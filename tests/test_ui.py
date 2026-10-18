"""Offscreen smoke tests for mlaser.ui (QT_QPA_PLATFORM=offscreen)."""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mlaser.ui import docops, i18n  # noqa: E402
from mlaser.model import LayerParams  # noqa: E402
from mlaser.ui.facade import DemoController  # noqa: E402

# screenshot of the main window: MLASER_SHOT_DIR, else the dev box's work/ui folder when it
# exists, else the temp directory (CI)
_WORK = "/home/dev/scratch/mlaser/work"
SHOT = os.path.join(os.environ.get("MLASER_SHOT_DIR")
                    or (os.path.join(_WORK, "ui") if os.path.isdir(_WORK) else tempfile.gettempdir()),
                    "screenshot.png")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


ORIG_LANG = os.path.join(os.path.dirname(__file__), "data", "Lang")   # synthetic original-format files


def test_lang_catalogue(tmp_path):
    tr = i18n.Translator(language="en", import_dir=str(tmp_path))
    # original keys and readable ids resolve to the same English text
    assert tr.tr("mf15") == tr.tr("cmd.machine.home") == "Go Origin"
    assert tr.tr("mp54") == "Start"
    assert tr.tr_param("pd125") == ("Cut Basic", "Cut Speed")
    assert tr.tr("nset_UI_Result2MainView") == "Copy to Main Drawing"     # ux.md mistranslations fixed
    assert tr.tr("newLang1") == "Burn-in Test"
    # named placeholders, positional values in the original's order
    msg = tr.format("mp83", 1, 2.25, 3.0, 4.0, 5)
    assert msg.startswith("Estimated time: 1 min 2.2 s") and "pierces: 5" in msg
    assert tr.format("mv17", dx=1, dy=2, length=3).endswith("length 3.000 mm")
    assert [li.code for li in tr.available()] == ["en"]     # nothing imported
    tr.set_language("zh")
    assert tr.tr("mf15") == "Go Origin"                       # no Chinese yet: English
    assert tr.tr("no_such_key", "x") == "x"
    # import from an original install (here: the synthetic Lang folder)
    assert sorted(tr.import_from(ORIG_LANG)) == ["fr", "zh"]
    assert {li.code for li in tr.available()} == {"en", "zh", "fr"}
    assert tr.tr("mf15") == tr.tr("cmd.machine.home") == "回原点"
    assert tr.tr("mp54") == "开始"
    assert tr.format("mp83", 1, 2.25, 3.0, 4.0, 5).startswith("预计用时：1分2.2秒")   # printf text
    tr.set_language("fr")
    assert tr.tr("mf15") == "Prise d'origine"
    assert tr.tr("mp54") == "Start"                           # missing in French: English
    # a fresh translator sees the stored import; the direct (no import) mode reads the folder
    assert i18n.Translator(language="fr", import_dir=str(tmp_path)).tr("mf15") == "Prise d'origine"
    direct = i18n.Translator(lang_dir=ORIG_LANG, language="zh", import_dir=str(tmp_path / "none"))
    assert direct.tr("pd125") == "切割基本.切割速度"
    with pytest.raises(FileNotFoundError):
        tr.import_from(str(tmp_path / "missing"))


def test_alarm_text_placeholder(tmp_path):
    from mlaser.controller.alarms import Alarm
    a = Alarm(8100, "EtherCATAxisErrorInfo_0", "X hard positive limit alarm", axis=0, kind="limit")
    tr = i18n.Translator(language="en", import_dir=str(tmp_path))
    assert a.text(tr) == "X hard positive limit alarm"
    tr = i18n.Translator(lang_dir=ORIG_LANG, language="zh", import_dir=str(tmp_path))
    assert a.text(tr) == "X 硬正限位报警"


def test_en_catalogue_covers_used_keys():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "i18n_keys", os.path.join(os.path.dirname(__file__), "..", "tools", "i18n_keys.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from mlaser.i18n import catalogue
    en = catalogue("en")
    missing = [k for k in mod.used_keys() if en.resolve(k) is None]
    assert not missing
    # every placeholder declared in args appears in the text, and no printf placeholders remain
    from mlaser.i18n import BRACE_RE, PRINTF_RE
    for sid, text in en.strings.items():
        assert not PRINTF_RE.search(text), sid
        assert set(BRACE_RE.findall(text)) == set(en.args.get(sid, [])), sid


def test_svg_icons(app):
    from mlaser.ui import icons, ribbon_spec
    for name in icons.used_icon_names():
        assert icons.has_icon(name), name
    ic = icons.command_icon("file.open")
    pm = ic.pixmap(64, 64)
    assert not pm.isNull() and pm.width() == 64
    assert icons.command_icon("no.such.command").isNull()
    # every vendored SVG is referenced (only the used ones are shipped)
    shipped = {f[:-4] for f in os.listdir(icons.ICON_DIR) if f.endswith(".svg")}
    assert shipped == icons.used_icon_names()
    assert os.path.exists(os.path.join(icons.ICON_DIR, "LICENSE"))
    assert ribbon_spec.all_commands()


def test_demo_controller_motion():
    t = [0.0]
    c = DemoController(clock=lambda: t[0])
    c.jog_start("X", 1)
    assert c.state().x == 0.0          # not connected -> ignored
    c.connect()
    c.jog_start("X", 1, fast=False)
    t[0] += 1.0
    assert c.state().x == pytest.approx(DemoController.JOG_SLOW)
    c.jog_stop()
    doc = docops.demo_document()
    c.start(doc)                       # refused: not armed, layers without parameters
    assert c.state().mode == "ready"
    assert {"not_armed", "layer_undefined"} <= c.last_preflight.codes()
    for li in {p.craft.layer for p in doc.parts}:
        doc.layers[li] = LayerParams(li)
    assert c.arm()
    c.start(doc)
    assert c.state().mode == "process"
    c.pause()
    assert c.state().mode == "pause"
    c.resume()
    t[0] += 1e4
    assert c.state().mode == "ready"
    c.frame(doc)
    assert c.state().mode == "frame"
    c.stop()
    assert c.state().mode == "ready"


def test_docops_roundtrip():
    doc = docops.demo_document()
    b0 = doc.bbox()
    docops.mirror(doc, True)
    docops.mirror(doc, True)
    assert doc.bbox() == pytest.approx(b0)
    docops.rotate(doc, 90)
    docops.rotate(doc, -90)
    assert doc.bbox() == pytest.approx(b0, abs=1e-6)
    n = len(doc.parts)
    doc.parts[0].selected = True
    docops.array(doc, 2, 3, 5, 5)
    assert len(doc.parts) == n + 5


def test_main_window_screenshot(app, tmp_path):
    from mlaser.ui.mainwindow import MainWindow

    win = MainWindow(DemoController(), i18n.Translator(lang_dir=ORIG_LANG, import_dir=str(tmp_path)))
    win.resize(1400, 860)
    doc = docops.demo_document()
    win.set_document(doc)
    win.ctl.connect()
    win.show()
    app.processEvents()
    # selection + layer assignment through the UI paths
    win.canvas.select_parts([1])
    win.assign_layer(5)
    assert win.document.parts[1].craft.layer == 5
    win.undo()
    assert win.document.parts[1].craft.layer == 2
    # copy / paste / nudge / delete (view key handler FUN_004d0480)
    n = len(win.document.parts)
    win.canvas.select_parts([0])
    win.run("edit.copy"); win.run("edit.paste")
    assert len(win.document.parts) == n + 1 and win.document.parts[-1].selected
    b = win.document.parts[-1].bbox()
    win.nudge(1, 0)
    assert win.document.parts[-1].bbox()[0] == pytest.approx(b[0] + 1.0)
    win.delete_selected()
    assert len(win.document.parts) == n
    # every ribbon command is wired to something callable
    assert "file.open" in win.ribbon.actions and "craft.lead" in win.ribbon.actions
    win.run("show.direction")
    win.run("show.move_path")
    # simulation runs to completion
    assert win.canvas.start_simulation(interval_ms=1)
    for _ in range(2000):
        if not win.canvas.simulation_running():
            break
        win.canvas._sim_step()
    assert not win.canvas.simulation_running()
    win.canvas.stop_simulation(clear=True)   # mv11 "clear track line"
    # language switch re-labels the ribbon
    win.set_language("zh")
    assert win.ribbon.tabText(0) == "开始"
    win.set_language("en")
    assert win.ribbon.tabText(0) == "Start"
    win.canvas.select_parts([2])
    win.canvas.zoom_fit()
    win._poll()
    app.processEvents()
    os.makedirs(os.path.dirname(SHOT), exist_ok=True)
    assert win.grab().save(SHOT)
    assert os.path.getsize(SHOT) > 10_000
    win.close()


def test_dialogs_build(app):
    from mlaser.model import LayerParams, LeadParams
    from mlaser.ui import dialogs
    from mlaser.ui.params import OPTION_SECTIONS, registry

    tr = i18n.Translator(language="en")
    assert len(registry()) > 500
    lp = LayerParams(index=1)
    d = dialogs.LayerParamDialog(tr, lp)
    d.values["GP.CutSpeed"] = 55.0
    d._store()
    assert lp.speed == 55.0
    li, lo = LeadParams(kind=1), LeadParams()
    ld = dialogs.LeadDialog(tr, li, lo)
    ld._w["in"][3].setValue(4.5)
    ld._store()
    assert li.length == 4.5
    assert dialogs.KerfDialog(tr, 0, 0.2).result_values() == (0, 0.2)
    for which, secs in OPTION_SECTIONS.items():
        dialogs.ParamGridDialog(tr, "mf137", secs, {})
    assert dialogs.IPDialog(tr).result_values()["pd445"] == "10.1.1.168"
    for cls in (dialogs.MicroJointDialog, dialogs.ArrayDialog, dialogs.PasswordDialog, dialogs.AboutDialog):
        cls(tr)
