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


def test_review2_trims():
    """Review 2 P2-8: the unused Qt plugins, translations, opengl32sw, the second OpenSSL and
    Pillow's AVIF codec stay out; what the app needs stays in."""
    for drop in ("PySide6/opengl32sw.dll", "libcrypto-3-x64.dll", "libssl-3-x64.dll",
                 "PIL/_avif.cpython-312-x86_64-linux-gnu.so", "PIL\\_avif.cp312-win_amd64.pyd",
                 "PySide6/plugins/platforms/qdirect2d.dll",
                 "PySide6/Qt/plugins/platforms/libqvnc.so", "PySide6/Qt/plugins/platforms/libqeglfs.so",
                 "PySide6/Qt/plugins/platforms/libqlinuxfb.so",
                 "PySide6/Qt/plugins/platforms/libqvkkhrdisplay.so",
                 "PySide6/Qt/plugins/platforms/libqminimalegl.so",
                 "PySide6/Qt/plugins/egldeviceintegrations/libqeglfs-x11-integration.so",
                 "PySide6/Qt/plugins/generic/libqevdevmouseplugin.so",
                 "PySide6/Qt/plugins/generic/libqtuiotouchplugin.so",
                 "PySide6/plugins/generic/qtuiotouchplugin.dll",
                 "PySide6/Qt/plugins/imageformats/libqtiff.so", "PySide6/plugins/imageformats/qwebp.dll",
                 "PySide6/Qt/plugins/imageformats/libqtga.so", "PySide6/Qt/plugins/imageformats/libqwbmp.so",
                 "PySide6/Qt/lib/libQt6Network.so.6", "PySide6/Qt6Network.dll"):
        assert cfg.drop_binary(drop), drop
    for keep in ("PySide6/Qt/plugins/platforms/libqxcb.so", "PySide6/Qt/plugins/platforms/libqwayland.so",
                 "PySide6/Qt/plugins/platforms/libqminimal.so", "PySide6/plugins/platforms/qwindows.dll",
                 "PySide6/Qt/plugins/platforms/libqcocoa.dylib",
                 "PySide6/Qt/plugins/printsupport/libcupsprintersupport.so",
                 "PySide6/Qt/plugins/imageformats/libqjpeg.so", "PySide6/Qt/plugins/imageformats/libqsvg.so",
                 "PySide6/Qt/plugins/imageformats/libqico.so", "PySide6/Qt/plugins/imageformats/libqgif.so",
                 "PySide6/Qt/plugins/iconengines/libqsvgicon.so",
                 "PySide6/Qt/plugins/xcbglintegrations/libqxcb-glx-integration.so",
                 "PySide6/Qt/lib/libQt6DBus.so.6", "PySide6/Qt/lib/libQt6OpenGL.so.6",
                 "PIL/_imaging.cpython-312-x86_64-linux-gnu.so", "libcrypto-3.dll", "libssl-3.dll"):
        assert not cfg.drop_binary(keep), keep
    assert cfg.drop_data("PySide6/Qt/translations/qtbase_de.qm")
    assert cfg.drop_data("PySide6/translations/qt_help_de.qm")
    assert not cfg.drop_data("mlaser/i18n/qm/mlaser_de.qm")          # the app's own catalogues
    excluded = cfg.excludes()
    assert "PySide6.QtNetwork" in excluded and "PySide6.QtDBus" in excluded


def test_linux_host_system_libraries_dropped():
    """Review 2 P2-8: the Linux bundle carried the build host's CUPS, gnutls, krb5, OpenSSL ...
    Everything resolved from a system directory stays out, except the X11 helper libraries."""
    for lib in ("libcups.so.2", "libgnutls.so.30", "libp11-kit.so.0", "libcrypto.so.3", "libssl.so.3",
                "libkrb5.so.3", "libavahi-client.so.3", "libfontconfig.so.1", "libfreetype.so.6",
                "libdbus-1.so.3", "libsystemd.so.0", "libglib-2.0.so.0", "libstdc++.so.6",
                "libgcc_s.so.1", "libX11.so.6", "libxkbcommon.so.0", "libxcb-randr.so.0"):
        for d in ("/lib/x86_64-linux-gnu/", "/usr/lib/x86_64-linux-gnu/", "/usr/lib64/"):
            assert cfg.is_host_system_lib(d + lib, "linux"), d + lib
            assert cfg.drop_entry((lib, d + lib, "BINARY"), "linux")
    for lib in ("libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0", "libxcb-keysyms.so.1",
                "libxcb-render-util.so.0", "libxcb-util.so.1", "libxkbcommon-x11.so.0"):
        assert not cfg.is_host_system_lib("/usr/lib/x86_64-linux-gnu/" + lib, "linux"), lib
    # wheels' own libraries and the Python runtime stay; other OSes are not affected
    for src in ("/home/u/.venv/lib/python3.12/site-packages/numpy.libs/libscipy_openblas64_.so",
                "/opt/hostedtoolcache/Python/3.12/x64/lib/libpython3.12.so.1.0",
                "/home/u/.local/share/uv/python/cpython-3.12/lib/libpython3.12.so.1.0"):
        assert not cfg.is_host_system_lib(src, "linux"), src
    assert not cfg.is_host_system_lib("/usr/lib/libcups.2.dylib", "darwin")
    assert not cfg.is_host_system_lib("C:/Windows/System32/x.dll", "win32")
    # the CUPS plugin's whole chain goes when its libcups goes (prune_binaries)
    deps = {"/w/cups.so": ["libcups.so.2"], "/lib/libcups.so.2": ["libgnutls.so.30"],
            "/lib/libgnutls.so.30": []}
    toc = [("PySide6/Qt/plugins/printsupport/libcupsprintersupport.so", "/w/cups.so", "BINARY"),
           ("libcups.so.2", "/lib/libcups.so.2", "BINARY"), ("libgnutls.so.30", "/lib/libgnutls.so.30", "BINARY")]
    kept, removed = cfg.prune_binaries(toc, lambda src: deps[src], drop=lambda e: cfg.drop_entry(e, "linux"))
    assert [k[0] for k in kept] == ["PySide6/Qt/plugins/printsupport/libcupsprintersupport.so"]


def test_ortools_windows_dlls(tmp_path):
    """Review 2 P2-8: the Windows bundle had no ortools.dll (sort mode 20 fell back silently).
    The hook collects ortools/.libs as it is, and top-level copies resolved along PATH go."""
    libs = tmp_path / "ortools" / ".libs"
    libs.mkdir(parents=True)
    for n in ("ortools.dll", "libprotobuf.dll", "abseil_dll.dll", "notes.txt"):
        (libs / n).write_bytes(b"")
    got = cfg.ortools_dlls(str(tmp_path / "ortools"))
    assert sorted(Path(s).name for s, _ in got) == ["abseil_dll.dll", "libprotobuf.dll", "ortools.dll"]
    assert {d for _, d in got} == {"ortools/.libs"}
    toc = [("ortools/.libs/libprotobuf.dll", "C:/sp/ortools/.libs/libprotobuf.dll", "BINARY"),
           ("libprotobuf.dll", "C:/Strawberry/c/bin/libprotobuf.dll", "BINARY"),
           ("abseil_dll.dll", "C:/x/abseil_dll.dll", "BINARY"),
           ("python312.dll", "C:/py/python312.dll", "BINARY")]
    kept, removed = cfg.dedupe_package_libs(toc)
    assert [r[0] for r in removed] == ["libprotobuf.dll"]
    assert "ortools/.libs/libprotobuf.dll" in [k[0] for k in kept]
    hook = (ROOT / "packaging" / "hooks" / "hook-ortools.py").read_text(encoding="utf-8")
    assert "ortools_dlls" in hook and 'collect_submodules("ortools.constraint_solver")' in hook
    spec = (ROOT / "packaging" / "mlaser.spec").read_text(encoding="utf-8")
    assert '"hooks"' in spec and "dedupe_package_libs" in spec and "missing_qt_imports" in spec


def test_ortools_hook_collects_the_routing_modules():
    """The routing parameters module pywrapcp imports lazily (DefaultRoutingSearchParameters)
    was missing from every bundle; the hook adds the whole routing package."""
    pytest.importorskip("ortools")
    pytest.importorskip("PyInstaller")
    import runpy
    ns = runpy.run_path(str(ROOT / "packaging" / "hooks" / "hook-ortools.py"))
    assert "ortools.constraint_solver.routing_parameters_pb2" in ns["hiddenimports"]
    if sys.platform.startswith("win"):
        assert any(s.lower().endswith("ortools.dll") for s, _ in ns["binaries"])
    else:
        assert ns["binaries"] == []


def test_missing_qt_imports():
    toc = [("PySide6/Qt/lib/libQt6Gui.so.6", "/s/gui", "BINARY"),
           ("PySide6/Qt/lib/libQt6Core.so.6", "/s/core", "BINARY"),
           ("PySide6/Qt/plugins/generic/libqtuiotouchplugin.so", "/s/tuio", "BINARY")]
    deps = {"/s/gui": ["libQt6Core.so.6", "libfontconfig.so.1"], "/s/core": [],
            "/s/tuio": ["libQt6Network.so.6", "libQt6Gui.so.6"]}
    assert cfg.missing_qt_imports(toc, lambda s: deps[s]) == [
        ("PySide6/Qt/plugins/generic/libqtuiotouchplugin.so", "libqt6network.so.6")]
    mac = [("PySide6/Qt/lib/QtCore.framework/Versions/A/QtCore", "/m/core", "BINARY"),
           ("PySide6/QtCore.abi3.so", "/m/b", "EXTENSION")]
    assert cfg.missing_qt_imports(mac, lambda s: ["@rpath/QtCore.framework/Versions/A/QtCore"]) == []


def test_bundle_manifest():
    m = cfg.manifest({"ortools": True, "native_kernel": True}, "1.2.3")
    assert m["shipped"]["ortools"] and not m["shipped"]["spyrrow"] and m["version"] == "1.2.3"
    assert set(m["shipped"]) == set(cfg.OPTIONAL_BACKENDS)
    with pytest.raises(ValueError):
        cfg.manifest({"bogus": True}, "1")
    import mlaser.__main__ as L
    assert set(L._BACKEND_CHECKS) | {"native_kernel"} == set(cfg.OPTIONAL_BACKENDS)
    spec = (ROOT / "packaging" / "mlaser.spec").read_text(encoding="utf-8")
    assert "cfg.MANIFEST_NAME" in spec and '"mlaser")]' in spec


def test_bundle_check_requires_the_backends_a_bundle_ships(monkeypatch, capsys):
    """A backend the bundle ships but cannot run fails --bundle-check loudly (it was a silent
    "missing" line: the Windows bundle shipped OR-Tools without ortools.dll); outside a bundle
    the same backend is only reported."""
    import mlaser.__main__ as L

    def broken():
        raise ImportError("DLL load failed while importing _pywrapcp")
    monkeypatch.setitem(L._BACKEND_CHECKS, "ortools", ("OR-Tools (sort mode 20)", broken))
    monkeypatch.setattr(L, "_bundle_manifest", lambda: None)
    assert L.bundle_check([]) == 0
    assert "[missing] OR-Tools (sort mode 20): ImportError" in capsys.readouterr().out
    monkeypatch.setattr(L, "_bundle_manifest", lambda: cfg.manifest({"ortools": True}, "0"))
    assert L.bundle_check([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL   ] OR-Tools (sort mode 20): ImportError" in out and "1 required item(s) failed" in out
    assert "bundle manifest: ships ortools" in out


def test_bundle_check_backend_probes_run_here():
    """The working checks themselves (each installed backend solves its small problem)."""
    import mlaser.__main__ as L
    assert "2 shapes parsed" in L._check_svgelements()
    assert "NotoSansSC" in L._check_fonts() and "NotoSansArabic" in L._check_fonts()
    for key, mod in (("ortools", "ortools"), ("spyrrow", "spyrrow"), ("ruckig", "ruckig")):
        try:
            __import__(mod)
        except ImportError:
            continue
        assert L._BACKEND_CHECKS[key][1]()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="ELF")
def test_symbol_floor_reads_version_references(tmp_path):
    import shutil
    if not shutil.which("objdump"):
        pytest.skip("no objdump")
    import importlib.util
    spec = importlib.util.spec_from_file_location("mlaser_packaging_build", ROOT / "packaging" / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    exe = Path(os.path.realpath(sys.executable))
    shutil.copy(exe, tmp_path / exe.name)
    floor = build.symbol_floor(tmp_path)
    assert floor["GLIBC"]["version"].startswith("2.") and floor["GLIBC"]["file"] == exe.name
    assert build._vkey("2.35") > build._vkey("2.9")


def test_linux_console_executable_is_a_symlink():
    spec = (ROOT / "packaging" / "mlaser.spec").read_text(encoding="utf-8")
    assert "cli = None if IS_LINUX" in spec and "symlink_to(APP)" in spec


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
    assert "Emitter kernel: " in p.stdout                     # C kernel or Python port
    assert (tmp_path / "cfg" / "netcheck.json").is_file()


def test_launcher_selftest_kernel_required(tmp_path):
    """MLASER_NATIVE=1: --selftest fails when the C kernel cannot be used, passes when it can."""
    from mlaser import _native
    p = subprocess.run([sys.executable, "-m", "mlaser", "--selftest", "--host", "127.0.0.1"],
                       env=dict(_env(tmp_path), MLASER_NATIVE="1"), capture_output=True, text=True,
                       timeout=60)
    if _native._emitter is not None:
        assert p.returncode == 0 and "Emitter kernel: native C kernel" in p.stdout, p.stdout
    else:
        assert p.returncode == 1 and "UNAVAILABLE" in p.stdout


def test_launcher_bundle_check(tmp_path):
    dxf = ROOT / "tests" / "data" / "dxf" / "gear.dxf"
    p = subprocess.run([sys.executable, "-m", "mlaser", "--bundle-check", str(dxf)],
                       env=_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "import gear.dxf" in p.stdout and "result: OK" in p.stdout
    assert "[ok     ] help documents" in p.stdout
    assert "emitter kernel (mlaser._native)" in p.stdout


@pytest.mark.ui
def test_launcher_smoke_test_and_first_run(tmp_path):
    # the first-run check is what this test is about: CI sets MLASER_NO_FIRSTRUN=1 for the rest
    env = {k: v for k, v in _env(tmp_path).items() if k != "MLASER_NO_FIRSTRUN"}
    p = subprocess.run([sys.executable, "-m", "mlaser", "--fake", "--smoke-test", "1"],
                       env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "smoke test: GUI ran for 1 s, exit code 0" in p.stderr
    assert "network self-test" in p.stderr                      # first run printed the check
    assert (tmp_path / "cfg" / "netcheck.json").is_file()
    p2 = subprocess.run([sys.executable, "-m", "mlaser", "--fake", "--smoke-test", "0.5"],
                        env=env, capture_output=True, text=True, timeout=120)
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
