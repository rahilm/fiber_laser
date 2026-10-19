"""Qt-free parts of the Wave 3 shell: user modes and PINs, command search, workspace slots,
command -> workspace mapping, key tables, colour contrast, the plugin API additions."""
from __future__ import annotations

import pytest

from mlaser.services import usermodes as um
from mlaser.services.command_search import Entry, RecentCommands, score, search
from mlaser.services.commands import CommandRegistry
from mlaser.services.config import ConfigService
from mlaser.services.workspaces import BUILTIN, SLOTS, command_workspace, slot_for


# --------------------------------------------------------------------------- user modes
def test_default_policy():
    assert um.default_min_mode("hw.upgrade") == um.SERVICE
    assert um.default_min_mode("card.params") == um.SERVICE
    assert um.default_min_mode("mon.controller") == um.SERVICE
    assert um.default_min_mode("craft.lead") == um.ENGINEER
    assert um.default_min_mode("draw.line") == um.ENGINEER
    assert um.default_min_mode("run.start") == um.OPERATOR
    assert um.default_min_mode("machine.home") == um.OPERATOR
    assert um.default_min_mode("file.open") == um.OPERATOR
    assert um.allowed("craft.lead", um.ENGINEER) and not um.allowed("craft.lead", um.OPERATOR)
    assert not um.allowed("hw.upgrade", um.ENGINEER) and um.allowed("hw.upgrade", um.SERVICE)


def test_declared_min_mode_wins():
    reg = CommandRegistry()
    spec = reg.register("x.fire", lambda: None, "p", min_mode="service", workspace="machine")
    assert um.min_mode_of("x.fire", spec) == um.SERVICE
    assert command_workspace("x.fire", spec) == "machine"
    with pytest.raises(ValueError):
        reg.register("x.bad", lambda: None, "p", min_mode="admin")


def test_pin_hash_and_verify():
    h = um.hash_pin("1234")
    assert h.startswith("pbkdf2-sha256$") and "1234" not in h
    assert um.verify_pin("1234", h) and not um.verify_pin("1235", h)
    assert not um.verify_pin("1234", "garbage")
    assert um.hash_pin("1234") != h                     # salted
    assert um.valid_pin("0042") and not um.valid_pin("12") and not um.valid_pin("12ab")


def test_mode_service_pins_and_startup(tmp_path):
    cfg = ConfigService(tmp_path / "c.json", load=False)
    s = um.UserModeService(cfg)
    assert s.mode == um.DEFAULT_MODE == um.ENGINEER
    with pytest.raises(PermissionError):
        s.set_pin(um.SERVICE, "1234")                   # only in Service mode
    assert s.set_mode(um.SERVICE)                       # no PIN yet: free
    s.set_pin(um.SERVICE, "4321")
    s.set_pin(um.ENGINEER, "1111")
    with pytest.raises(ValueError):
        s.set_pin(um.ENGINEER, "12")
    assert s.set_mode(um.OPERATOR)                      # down: never asks
    assert s.needs_pin(um.ENGINEER) and s.needs_pin(um.SERVICE)
    assert not s.set_mode(um.ENGINEER, "0000") and s.mode == um.OPERATOR
    assert s.set_mode(um.ENGINEER, "1111") and s.mode == um.ENGINEER
    assert not s.set_mode(um.SERVICE, "1111")           # the Service PIN is needed
    assert s.set_mode(um.SERVICE, "4321")
    cfg.save()
    # start-up: never above the first PIN-protected mode
    s2 = um.UserModeService(ConfigService(tmp_path / "c.json"))
    assert s2.mode == um.OPERATOR
    seen = []
    s2.subscribe(seen.append)
    assert s2.set_mode(um.ENGINEER, "1111") and seen == [um.ENGINEER]
    # only a Service PIN: start at most in Engineer
    cfg3 = ConfigService(tmp_path / "d.json", load=False)
    s3 = um.UserModeService(cfg3)
    s3.set_mode(um.SERVICE)
    s3.set_pin(um.SERVICE, "9999")
    assert um.UserModeService(cfg3).mode == um.ENGINEER
    s3.set_pin(um.SERVICE, "")                          # cleared
    assert um.UserModeService(cfg3).mode == um.SERVICE


# --------------------------------------------------------------------------- search
def test_fuzzy_score():
    assert score("mj", "Micro Joint")[0] > 0
    assert score("zz", "Micro Joint")[0] == 0
    s, pos = score("mj", "Micro Joint")
    assert pos == (0, 6)                                # word starts preferred
    assert score("kerf", "Kerf")[0] > score("kerf", "Clear kerf offset")[0]
    assert score("ecrou", "Écrou")[0] > 0               # accents folded


def test_search_ranks_and_recents():
    es = [Entry("file.save", "Save"), Entry("file.save_as", "Save As"), Entry("select.all", "Select All"),
          Entry("craft.microjoint", "Micro Joint", "Process", "J"), Entry("craft.lead", "Lead Line"),
          Entry("hw.upgrade", "Upgrade", enabled=False)]
    ids = [m.entry.id for m in search(es, "micro")]
    assert ids[0] == "craft.microjoint"
    assert [m.entry.id for m in search(es, "sa", recent=["select.all"])][0] == "select.all"
    empty = [m.entry.id for m in search(es, "", recent=["craft.lead", "file.save"])]
    assert empty[:2] == ["craft.lead", "file.save"]
    assert "hw.upgrade" not in [m.entry.id for m in search(es, "up")]
    assert "hw.upgrade" in [m.entry.id for m in search(es, "up", include_disabled=True)]
    assert [m.entry.id for m in search(es, "process")][0] == "craft.microjoint"   # by workspace
    r = RecentCommands(size=3)
    for c in ("a", "b", "c", "a", "d"):
        r.add(c)
    assert r.ids == ["d", "a", "c"]


# --------------------------------------------------------------------------- workspaces
def test_slots_and_command_workspaces():
    assert SLOTS == ("design", "process", "nest", "run", "report", "machine")
    assert [BUILTIN[s].shortcut for s in SLOTS] == [f"Ctrl+{i}" for i in range(1, 7)]
    assert BUILTIN["design"].canvas and not BUILTIN["design"].placeholder
    assert slot_for("ext_process.process") == "process"
    assert slot_for("ext_x.operator", "Run") == "run"
    assert slot_for("ext_x.thing", "", "nest") == "nest"
    assert slot_for("ext_x.tech_db", "Technology") == ""
    assert command_workspace("craft.lead") == "process"
    assert command_workspace("nest.run") == "nest"
    assert command_workspace("hw.upgrade") == "machine"
    assert command_workspace("draw.line") == "design"
    assert command_workspace("app.about") == ""


# --------------------------------------------------------------------------- contrast
def test_contrast_helpers():
    from mlaser.ui.shell.contrast import adjust_for_background, adjust_palette, contrast, distinct
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#777777") == pytest.approx(1.0)
    yellow = adjust_for_background("#fcfc00", "#fbfbfa", 3.0)
    assert contrast(yellow, "#fbfbfa") >= 3.0
    import colorsys
    h0 = colorsys.rgb_to_hls(*(int("fcfc00"[i:i + 2], 16) / 255 for i in (0, 2, 4)))[0]
    h1 = colorsys.rgb_to_hls(*(int(yellow[1:][i:i + 2], 16) / 255 for i in (0, 2, 4)))[0]
    assert abs(h0 - h1) < 0.02                           # the hue is kept
    assert adjust_for_background("#00fc00", "#000000") == "#00fc00"   # already fine: unchanged
    greys = adjust_palette(["#999999", "#e6e6e6"], "#ffffff")
    assert distinct(greys) >= 12.0


def test_keymap_has_no_conflicts():
    from mlaser.ui.shell import keymap
    assert keymap.conflicts() == []
    assert keymap.sequences("Ctrl+K|F1") == ["Ctrl+K", "F1"]
    assert keymap.key_for("shell.palette") == "Ctrl+K" and keymap.key_for("app.help") == "F1"
    md = keymap.markdown([("Design", "Undo", "Ctrl+Z", "main window")])
    assert "| Design | Undo | `Ctrl+Z` | main window |" in md


# --------------------------------------------------------------------------- plugin API
class _OldHost:
    """A Wave 2 host: only host_add_workspace(name, widget, title)."""

    def __init__(self):
        from mlaser.services.document import DocumentService
        from mlaser.services.plugins import Hooks
        self.commands, self.hooks, self.docs = CommandRegistry(), Hooks(), DocumentService()
        self.config, self.jobs, self.ctl, self.tr_ = ConfigService(load=False), None, None, None
        self.added = []

    def log(self, msg):
        pass

    def host_add_dock(self, widget, title, area, key):
        return widget

    def host_add_ribbon_tab(self, title, commands):
        return title

    def host_add_workspace(self, name, widget, title):
        self.added.append((name, widget, title))
        return widget


def test_add_workspace_backward_compatible():
    from mlaser.services.plugins import HOOKS, PluginContext
    host = _OldHost()
    ctx = PluginContext(host, "ext_run")
    w = object()
    assert ctx.add_workspace("run", w, "Run", icon="player-play", canvas=True, order=40) is w
    assert host.added == [("ext_run.run", w, "Run")]      # options dropped for an old host
    with pytest.raises(TypeError):
        ctx.add_workspace("x", w, "X", colour="red")
    assert ctx.user_mode == "engineer" and ctx.show_workspace("run") is False
    assert "workspace_changed" in HOOKS and "user_mode_changed" in HOOKS
    seen = []
    ctx.on_user_mode_changed(seen.append)
    ctx.on_workspace_changed(seen.append)
    host.hooks.emit("user_mode_changed", "operator")
    host.hooks.emit("workspace_changed", "run")
    assert seen == ["operator", "run"]
    spec = ctx.register_command("ext_run.big_start", lambda: None, "Start", min_mode="operator", workspace="run")
    assert spec.min_mode == "operator" and spec.workspace == "run"
