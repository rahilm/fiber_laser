"""Broad exception handlers say why (review 2026-11-09, arch A14).

* In the job services, the services layer and the run screens every ``noqa: BLE001`` states its
  reason after it (``# noqa: BLE001 - <why>``); a silent default that is the permissive answer
  logs.
* Elsewhere the reasonless ones may only become fewer (a ratchet: a new broad handler states its
  reason, an old one gets it when its code is touched).
* ``mlaser._swallow.swallow`` logs what it ignores, with the traceback, and lets
  ``BaseException`` through."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "mlaser"
#: every handler here states its reason
STRICT = ("services", "ui/safety_ui.py", "ui/run_rules.py", "ui/workspaces/run.py", "_swallow.py")
#: reasonless ``noqa: BLE001`` outside STRICT when this test was written (deep review 2026-11)
RATCHET = 183

_NOQA = re.compile(r"#\s*noqa:\s*((?:[A-Z]+\d+\s*,\s*)*BLE001(?:\s*,\s*[A-Z]+\d+)*)(.*)$")


def _reasonless(path: Path) -> list[str]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = _NOQA.search(line)
        if m and not m.group(2).strip(" -—:"):
            out.append(f"{path.relative_to(ROOT.parent)}:{n}: {line.strip()}")
    return out


def _files(rel: str) -> list[Path]:
    p = ROOT / rel
    return sorted(p.rglob("*.py")) if p.is_dir() else [p]


def test_strict_scope_states_every_reason():
    bad = [x for rel in STRICT for f in _files(rel) for x in _reasonless(f)]
    assert not bad, "a broad handler without its reason:\n" + "\n".join(bad)


def test_reasonless_handlers_only_become_fewer():
    strict = {f for rel in STRICT for f in _files(rel)}
    bad = [x for f in sorted(ROOT.rglob("*.py")) if f not in strict for x in _reasonless(f)]
    assert len(bad) <= RATCHET, (f"{len(bad)} reasonless noqa: BLE001 (at most {RATCHET}): give the new one its "
                                 "reason (# noqa: BLE001 - why)")


def test_swallow_logs_what_it_ignores(caplog):
    from mlaser._swallow import swallow
    log = logging.getLogger("mlaser.test_swallow")
    with caplog.at_level(logging.DEBUG, logger="mlaser.test_swallow"):
        with swallow(log, "the optional thing"):
            raise ValueError("broken")
    (rec,) = [r for r in caplog.records if r.name == "mlaser.test_swallow"]
    assert rec.getMessage() == "the optional thing failed (ignored)" and rec.exc_info is not None
    with pytest.raises(KeyboardInterrupt):
        with swallow(log, "x"):
            raise KeyboardInterrupt
