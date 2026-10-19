"""PyInstaller hook for OR-Tools (packaging/mlaser.spec, ``hookspath``).

Windows: the wheel keeps its DLLs (ortools.dll, libprotobuf.dll, abseil_dll.dll, highs.dll,
libscip.dll ...) in ``ortools/.libs`` and ``ortools/__init__.py`` loads them from there by full
path.  The extension modules have no rpath, so PyInstaller's dependency scan cannot find
ortools.dll: collect the folder as it is (bundle_config.ortools_dlls).  Linux and macOS resolve
the same folder through the extensions' rpath, PyInstaller collects it there already.
"""
import os
import sys

from PyInstaller.utils.hooks import get_package_paths

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # packaging/
import bundle_config as cfg                            # noqa: E402

binaries = []
if sys.platform.startswith("win"):
    binaries = cfg.ortools_dlls(get_package_paths("ortools")[1])
    if not any(dest == "ortools/.libs" and src.lower().endswith("ortools.dll") for src, dest in binaries):
        raise SystemExit("hook-ortools: no ortools/.libs/ortools.dll in the installed wheel")

# every module of the routing package: pywrapcp imports routing_parameters_pb2 and the other
# *_pb2 modules lazily (DefaultRoutingSearchParameters); without them sort mode 20 failed in
# every bundle (found by the Review 2 bundle check)
from PyInstaller.utils.hooks import collect_submodules  # noqa: E402

hiddenimports = collect_submodules("ortools.constraint_solver")
