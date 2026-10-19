"""packaging/: bundle selection rules, licence notices, the `python -m mlaser` launcher.

The PyInstaller build itself is not run here (about 1 minute; CI job "package" and
``python packaging/build.py --smoke`` do that)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

import bundle_config as cfg  # noqa: E402
import gen_licenses  # noqa: E402


def test_qt_module_names():
    assert cfg.qt_module_of("PySide6/Qt/lib/libQt6Charts.so.6") == "Charts"
    assert cfg.qt_module_of("PySide6\\Qt6Core.dll") == "Core"
    assert cfg.qt_module_of("PySide6/Qt/lib/QtGui.framework") == "Gui"
    assert cfg.qt_module_of("PySide6/QtCore.abi3.so") == ""          # the binding, not Qt
    assert cfg.qt_module_of("numpy/core.so") == ""


def test_gpl_only_and_drop_rules():
    assert cfg.is_gpl_only("PySide6/Qt/lib/libQt6Charts.so.6")
    assert cfg.is_gpl_only("PySide6/Qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so")
    assert not cfg.is_gpl_only("PySide6/Qt/lib/libQt6Widgets.so.6")
    for keep in ("PySide6/Qt/lib/libQt6Core.so.6", "PySide6/Qt/lib/libQt6Svg.so.6",
                 "PySide6/Qt/plugins/platforms/libqxcb.so", "PySide6/Qt/plugins/imageformats/libqsvg.so",
                 "PySide6/Qt/plugins/platforms/libqoffscreen.so", "PySide6/Qt6PrintSupport.dll"):
        assert not cfg.drop_binary(keep), keep
    for drop in ("PySide6/Qt/lib/libQt6WebEngineCore.so.6", "PySide6/Qt/lib/libQt6Quick.so.6",
                 "PySide6/Qt/plugins/platformthemes/libqgtk3.so", "PySide6/Qt/plugins/tls/libqopensslbackend.so",
                 "PySide6/Qt/plugins/imageformats/libqpdf.so", "PySide6/Qt/lib/libQt6Pdf.so.6"):
        assert cfg.drop_binary(drop), drop
    assert cfg.drop_data("PySide6/Qt/qml/QtQuick/qmldir")


def test_app_qt_modules_not_excluded():
    """Every PySide6 module the app imports must stay in the bundle."""
    import re
    used = set()
    for p in (ROOT / "mlaser").rglob("*.py"):
        used |= set(re.findall(r"PySide6\.(Qt[A-Za-z0-9]+)", p.read_text(encoding="utf-8")))
    excluded = {m.split(".", 1)[1] for m in cfg.excludes() if m.startswith("PySide6.")}
    assert used and not (used & excluded), used & excluded


def test_parity_tooling_excluded_but_ports_kept():
    assert not cfg.keep_mlaser_module("mlaser.parity")
    assert not cfg.keep_mlaser_module("mlaser.parity.capture")
    assert cfg.keep_mlaser_module("mlaser.parity_ports.profile")
    assert cfg.keep_mlaser_module("mlaser.ui.mainwindow")


def test_prune_binaries_removes_only_orphans():
    deps = {"/s/plug_gtk.so": ["libgtk-3.so.0"], "/s/libgtk-3.so.0": ["libcairo.so.2", "libz.so.1"],
            "/s/libcairo.so.2": [], "/s/libz.so.1": [], "/s/core.so": ["libz.so.1"],
            "/s/libdl_only.so": []}
    toc = [("PySide6/Qt/plugins/platformthemes/libqgtk3.so", "/s/plug_gtk.so", "BINARY"),
           ("libgtk-3.so.0", "/s/libgtk-3.so.0", "BINARY"), ("libcairo.so.2", "/s/libcairo.so.2", "BINARY"),
           ("libz.so.1", "/s/libz.so.1", "BINARY"), ("core.so", "/s/core.so", "EXTENSION"),
           ("libdl_only.so", "/s/libdl_only.so", "BINARY"),
           ("cairo_link.so.2", "libcairo.so.2", "SYMLINK")]
    kept, removed = cfg.prune_binaries(toc, lambda src: deps[src])
    names = {k[0] for k in kept}
    assert names == {"libz.so.1", "core.so", "libdl_only.so"}   # dlopen-only lib stays
    assert {r[0] for r in removed} >= {"libgtk-3.so.0", "libcairo.so.2", "cairo_link.so.2"}


def test_help_documents_bundled():
    """The bundle carries the help documents where helpdocs looks for them (mlaser/help/docs)."""
    from mlaser.diag import helpdocs
    datas = cfg.help_datas(ROOT)
    dests = {Path(src).name: dest for src, dest in datas}
    assert dests["getting-started.md"] == "mlaser/help/docs/user"
    assert dests["SAFETY.md"] == "mlaser/help/docs" and dests["protocol.md"] == "mlaser/help/docs/re"
    assert all(Path(src).exists() for src, _ in datas)
    assert not any("review" in dest or "history" in dest for _, dest in datas)
    assert helpdocs.PKG_DOCS.replace(os.sep, "/").endswith("mlaser/help/docs")
    spec = (ROOT / "packaging" / "mlaser.spec").read_text(encoding="utf-8")
    assert "cfg.help_datas(ROOT)" in spec


def test_spec_is_onedir():
    spec = (ROOT / "packaging" / "mlaser.spec").read_text(encoding="utf-8")
    assert "exclude_binaries=True" in spec and "COLLECT(" in spec
    assert "upx=False" in spec


def test_licence_notices(tmp_path):
    out = tmp_path / "LICENSES"
    names = gen_licenses.generate(out)
    assert (out / "mlaser-LICENSE.txt").read_text().startswith("MIT License")
    lgpl = (out / "LGPL-3.0.txt").read_text()
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in lgpl and "Version 3" in lgpl
    assert "GNU GENERAL PUBLIC LICENSE" in (out / "GPL-3.0.txt").read_text()
    notice = (out / "Qt-PySide6-NOTICE.txt").read_text()
    assert "LGPL-3.0" in notice and "download.qt.io" in notice and "Replacing the Qt libraries" in notice
    tp = (out / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    lower = {n.lower() for n in names}
    for dist in ("ezdxf", "shapely", "svgelements", "pyclipper", "uharfbuzz", "fonttools", "numpy"):
        assert dist in lower, dist
        assert dist in tp.lower()
    assert "mlaser" not in lower and "pyinstaller" not in lower
    assert "Tabler Icons" in tp and "CPython" in tp


def test_bundled_distributions_by_files(tmp_path):
    # a fake bundle with svgelements' module and shapely's GEOS library dir
    (tmp_path / "shapely.libs").mkdir()
    import importlib.metadata as md
    geos = [f for f in md.distribution("shapely").files if f.parts[0] == "shapely.libs"]
    if not geos:
        pytest.skip("shapely wheel without a .libs folder on this platform")
    (tmp_path / "/".join(geos[0].parts)).write_bytes(b"")
    dists = gen_licenses.bundled_distributions([tmp_path], modules={"svgelements.svgelements"})
    assert {d.metadata["Name"].lower() for d in dists} == {"shapely", "svgelements"}


# ------------------------------------------------------------------ python -m mlaser

def _env(tmp_path):
    return dict(os.environ, QT_QPA_PLATFORM="offscreen", MLASER_CONFIG_DIR=str(tmp_path / "cfg"))


def test_launcher_selftest(tmp_path):
    p = subprocess.run([sys.executable, "-m", "mlaser", "--selftest", "--host", "127.0.0.1"],
                       env=_env(tmp_path), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert "network self-test" in p.stdout and "PySide6" not in p.stderr
    assert (tmp_path / "cfg" / "netcheck.json").is_file()


def test_launcher_bundle_check(tmp_path):
    dxf = ROOT / "tests" / "data" / "dxf" / "gear.dxf"
    p = subprocess.run([sys.executable, "-m", "mlaser", "--bundle-check", str(dxf)],
                       env=_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "import gear.dxf" in p.stdout and "result: OK" in p.stdout
    assert "[ok     ] help documents" in p.stdout


@pytest.mark.ui
def test_launcher_smoke_test_and_first_run(tmp_path):
    p = subprocess.run([sys.executable, "-m", "mlaser", "--fake", "--smoke-test", "1"],
                       env=_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "smoke test: GUI ran for 1 s, exit code 0" in p.stderr
    assert "network self-test" in p.stderr                      # first run printed the check
    assert (tmp_path / "cfg" / "netcheck.json").is_file()
    p2 = subprocess.run([sys.executable, "-m", "mlaser", "--fake", "--smoke-test", "0.5"],
                        env=_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert p2.returncode == 0 and "network self-test" not in p2.stderr   # only once


def test_launcher_hands_multiprocessing_children_to_the_app_entry():
    """freeze_support() lives in mlaser.ui.__main__.main (first statement); the launcher sends
    a frozen spawn child there before parsing anything."""
    src = (ROOT / "mlaser" / "__main__.py").read_text(encoding="utf-8")
    body = src[src.index("def main("):]
    assert body.index("_is_multiprocessing_child") < body.index("argparse")
    ui = (ROOT / "mlaser" / "ui" / "__main__.py").read_text(encoding="utf-8")
    ub = ui[ui.index("def main("):]
    assert ub.index("multiprocessing.freeze_support()") < ub.index("argparse")
    import mlaser.__main__ as L
    assert L._is_multiprocessing_child(["--multiprocessing-fork", "pipe_handle=3"])
    assert L._is_multiprocessing_child(["-B", "-c", "from multiprocessing.spawn import spawn_main"])
    assert not L._is_multiprocessing_child(["--demo"])
