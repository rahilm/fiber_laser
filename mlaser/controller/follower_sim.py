"""Card simulator with the on-board height follower (moved from ``mlaser.devices.follower``).

:func:`make_follower_card_simulator` extends :class:`~mlaser.controller.simulator.CardSimulator`
with the follower blocks 10000.. / 11000.., the ``[118, ...]`` commands and function 0x26
downloads, driving a :class:`mlaser.devices.follower.FollowerModel`.
"""
from __future__ import annotations

from ..devices.follower import (FollowerModel, CMD_CALIBRATE, CMD_CANCEL, COMMIT_FACTORY, N_PROPS, N_STATUS, REG_COMMIT, REG_CURVE, REG_CURVE_COUNT, REG_MAILBOX, REG_PROPS, REG_STATUS, SUB_CALIBRATE, SUB_FOLLOW)
from ..protocol import frames as F
from .simulator import EXC_ILLEGAL_FUNCTION, CardSimulator  # noqa: F401


def make_follower_card_simulator(**kw):
    """A :class:`~mlaser.controller.simulator.CardSimulator` subclass instance that also
    serves the follower blocks 10000.. and 11000.., applies ``[118, ...]`` commands to a
    :class:`FollowerModel` and accepts function 0x26 downloads."""
    class FollowerCardSimulator(CardSimulator):
        def __init__(self, **k):
            k.setdefault("onboard_zf", True)
            super().__init__(**k)
            self.zf = FollowerModel()
            self.downloads: list[tuple[int, bytes]] = []
            self.props: dict[int, int] = {}
            self.commits: list[int] = []

        def _read(self, addr: int) -> int:
            if REG_STATUS <= addr < REG_STATUS + N_STATUS:
                i = addr - REG_STATUS
                if i == 2:
                    return 0x80000000 | (0x10 if self.zf.calibrated else 0)
                if i == 3:
                    return CMD_CALIBRATE if self.zf.calibrating else 0
                if i == 4:      # FUN_100394b0 tests bit 15 here (see module doc)
                    return 0x8000 if self.zf.following else 0
                if i in (5, 6):
                    return self.zf.height_um & 0xFFFFFFFF
                if i == 11:
                    return self.zf.raw()
                return 0
            if REG_PROPS <= addr < REG_PROPS + 2 * N_PROPS:
                return self.props.get(addr - REG_PROPS, 0)
            if addr == REG_CURVE_COUNT:
                return len(self.zf.table.points)
            if REG_CURVE <= addr < REG_CURVE + 2 * len(self.zf.table.points) + 2:
                flat = [self.zf.table.reference, self.zf.table.reference_b]
                for h, r in self.zf.table.points:
                    flat += [h, r]
                return flat[addr - REG_CURVE] & 0xFFFFFFFF
            return super()._read(addr)

        def _write(self, addr: int, vals: list[int]) -> None:
            if addr == REG_MAILBOX and vals and vals[0] in (CMD_CALIBRATE, CMD_CANCEL):
                self.state.commands.append(list(vals))
                self.zf.calibrating = vals[0] == CMD_CALIBRATE
                return
            if REG_PROPS <= addr < REG_PROPS + 2 * N_PROPS:
                for i, v in enumerate(vals):
                    self.props[addr - REG_PROPS + i] = v
                return
            if addr == REG_COMMIT:
                self.commits.append(vals[0])
                if vals[0] == COMMIT_FACTORY:
                    self.props.clear()
                return
            super()._write(addr, vals)

        def _mailbox(self, w: list[int]) -> None:
            if w and w[0] == F.OP_FOLLOW and len(w) >= 2:
                if w[1] == SUB_FOLLOW and len(w) >= 4:
                    self.zf.following = bool(w[2])
                    self.zf.target = w[3]
                    if self.zf.following:
                        self.zf.height_um = w[3]
                elif w[1] == SUB_CALIBRATE:
                    self.zf.calibrating = True
                return
            super()._mailbox(w)

        def handle(self, data: bytes):
            try:
                req = F.decode_request(data)
            except F.FrameError:
                return None
            if req.func == F.FUNC_WRITE_BYTES_AT:
                self.requests += 1
                self.downloads.append((req.addr, bytes(req.values)))
                return F.encode_response(F.Response(req.func, req.tid, req.addr, req.count))
            return super().handle(data)

    return FollowerCardSimulator(**kw)
