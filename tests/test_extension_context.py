"""The built-in extensions reach the main window only through their typed context (review
2026-11-09, arch.md A12 item 2): :class:`mlaser.ui.extension_context.ExtensionContext`.

* no ``mlaser/ui/ext_*.py`` module uses ``ctx.window`` / ``ctx.host``, keeps the window in an
  attribute (``self.w``, ``self.window``), probes it with ``getattr`` / ``hasattr``, or reads an
  attribute of ``ctx.parent`` (the window only as a Qt parent);
* in the real window every built-in gets an :class:`ExtensionContext`, and every accessor of it
  resolves (a misspelt window attribute is an error here, not a silent ``None`` in a feature);
* a built-in whose ``register`` trips over a missing part of the window is reported like any
  failing extension, and an interlock extension that fails still blocks Start (fail closed).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXT_FILES = sorted((ROOT / "mlaser" / "ui").glob("ext_*.py"))
WINDOW_NAMES = {"w", "window", "win", "window_"}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else ""
    return ""


def _is_ctx(expr: str) -> bool:
    return expr == "ctx" or expr.endswith(".ctx")


def _is_window(expr: str) -> bool:
    last = expr.rsplit(".", 1)[-1]
    return last in WINDOW_NAMES or (_is_ctx(expr.rsplit(".", 1)[0]) and last == "parent" and "." in expr)


def _violations(path: Path) -> list[str]:
    out = []
    tree = ast.parse(path.read_text("utf-8"))
    for node in ast.walk(tree):
        where = f"{path.name}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Attribute):
            base = _dotted(node.value)
            if _is_ctx(base) and node.attr in ("window", "host"):
                out.append(f"{where}: {base}.{node.attr}")
            elif base and _is_ctx(base.rsplit(".", 1)[0]) and base.endswith(".parent") and "." in base:
                out.append(f"{where}: {base}.{node.attr} (ctx.parent is only a Qt parent)")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                node.func.id in ("getattr", "hasattr", "setattr") and node.args:
            target = _dotted(node.args[0])
            if target and (_is_window(target) or _is_ctx(target)):
                out.append(f"{where}: {node.func.id}({target}, ...)")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for el in (t.elts if isinstance(t, ast.Tuple) else [t]):
                    if isinstance(el, ast.Attribute) and _dotted(el.value) == "self" and el.attr in ("w", "window", "window_"):
                        out.append(f"{where}: self.{el.attr} = ... (keep the context, not the window)")
    return out


def test_the_scan_sees_the_old_patterns(tmp_path):
    src = ("def register(ctx):\n    w = ctx.window\n    getattr(w, 'tr_', None)\n"
           "    ctx.parent.statusBar()\n    hasattr(self.ctx, 'x')\n"
           "class E:\n    def __init__(self, ctx):\n        self.w = ctx.host\n")
    p = tmp_path / "ext_probe.py"
    p.write_text(src, "utf-8")
    found = _violations(p)
    assert len(found) == 6, found


def test_builtin_extensions_do_not_reach_into_the_window():
    assert EXT_FILES, "no ext_*.py found"
    hits = [v for p in EXT_FILES for v in _violations(p)]
    assert hits == []


# --------------------------------------------------------------------------- the real window
@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _window(tmp_path, monkeypatch, patch=None):
    from mlaser.services.config import ConfigService
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow

    warned: list = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a) or QMessageBox.Ok))
    if patch is not None:
        patch()
    cfg = ConfigService()
    cfg.set("app_dir", str(tmp_path))
    return MainWindow(DemoController(), extensions=EXTENSIONS, config=cfg), warned


def _close(qapp, w):
    w._test_no_dialogs = True
    w.close()
    w.deleteLater()
    qapp.processEvents()


def test_every_builtin_gets_the_typed_context_and_every_accessor_resolves(qapp, tmp_path, monkeypatch):
    from mlaser.services.plugins import PluginContext
    from mlaser.ui.extension_context import ExtensionContext
    w, _warned = _window(tmp_path, monkeypatch)
    try:
        assert w.failed_extensions == {}
        builtins = {n: c for n, c in w.plugin_contexts.items() if c.origin == "builtin"}
        assert builtins and all(type(c) is ExtensionContext for c in builtins.values())
        ctx = builtins["ext_card"]
        own = [n for n, v in vars(ExtensionContext).items()
               if isinstance(v, property) and n not in vars(PluginContext)]
        assert {"canvas", "safety_ui", "log_panel", "shell", "parent", "path", "machine"} <= set(own)
        for name in own:
            getattr(ctx, name)                  # AttributeError: a misspelt window attribute
        assert ctx.controller is w.ctl and ctx.safety_ui is w.safety_ui and ctx.parent is w
        assert ctx.text("mf123", "Alarm") == w.tr_.tr("mf123")
    finally:
        _close(qapp, w)


def test_a_builtin_that_reads_a_part_too_early_is_reported_and_blocks_start(qapp, tmp_path, monkeypatch):
    """``ctx.safety_ui`` is installed after the extensions register: reading it in ``register``
    used to be a silent ``None`` (getattr); now it raises, the extension is reported (log, the
    Plugin manager's failure list) and, for an interlock extension, Start is refused."""
    import mlaser.ui.ext_diag as Dg
    real = Dg.register

    def early(ctx):
        ctx.safety_ui                            # not installed yet: AttributeError
        return real(ctx)

    w, warned = _window(tmp_path, monkeypatch, lambda: monkeypatch.setattr(Dg, "register", early))
    try:
        assert isinstance(w.failed_extensions.get("ext_diag"), AttributeError)
        assert "ext_diag" in w.extension_failures
        assert w.extension("ext_diag") is None
        ok, why = w.jobs.check_start("process")
        assert not ok and "Start is blocked" in why
        qapp.processEvents()
        assert warned
    finally:
        _close(qapp, w)
