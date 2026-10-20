"""The Settings window's application preferences, applied to the main window and kept live.

:func:`install` (end of ``MainWindow.__init__``) takes the original's changed values into the
new keys once (:func:`mlaser.services.settings_catalog.migrate_legacy`), applies every preference
and subscribes to the ConfigService, so a change in the Settings window (or anywhere else)
reaches the running program at once:

* ``display.*``: the canvas view options (the ``show.*`` toggles of the View tab write them);
* ``shell.language``: the interface language;
* ``import.cleanup_on_open``: an opened drawing is cleaned up with the ``import.*`` values
  (the Optimize command's parameters, :func:`cleanup_params`); ``app.last_file`` records it.

``design.nudge_*`` is read by ext_draw, ``nest.*`` by the Nest workspace, ``shell.*`` by the
shell, each with its own listener.
"""
from __future__ import annotations

from typing import Any


#: canvas ViewOptions attribute -> preference
DISPLAY = {"show_bbox": "display.show_bbox", "unclosed_red": "display.unclosed_red",
           "show_index": "display.show_index", "show_start": "display.show_start",
           "show_direction": "display.show_direction", "show_move_path": "display.show_move_path"}
SHOW_COMMANDS = {"show.bbox": "show_bbox", "show.unclosed_red": "unclosed_red", "show.index": "show_index",
                 "show.start": "show_start", "show.direction": "show_direction",
                 "show.move_path": "show_move_path"}
_CLEANUP = {"remove_small": "import.remove_small", "small_gate": "import.small_gate",
            "remove_overlap": "import.remove_overlap", "overlap_gate": "import.overlap_gate",
            "merge_type": "import.merge_type", "connect_gate": "import.connect_gate",
            "smooth": "import.smooth", "smooth_accuracy": "import.smooth_accuracy",
            "close_gap": "import.close_gap"}


def cleanup_params(cfg: Any):
    """The ``import.*`` values as :class:`mlaser.cam.cleanup.CleanupParams`."""
    from mlaser.cam.cleanup import CleanupParams

    return CleanupParams(**{attr: cfg.get(key) for attr, key in _CLEANUP.items()})


def apply_display(w: Any) -> None:
    cfg, opts = w.config, w.canvas.options
    changed = False
    for attr, key in DISPLAY.items():
        v = bool(cfg.get(key))
        if getattr(opts, attr) != v:
            setattr(opts, attr, v)
            changed = True
    for cmd, attr in SHOW_COMMANDS.items():
        act = w.ribbon.actions.get(cmd)
        if act is not None and act.isChecked() != getattr(opts, attr):
            act.blockSignals(True)
            act.setChecked(getattr(opts, attr))
            act.blockSignals(False)
    if changed:
        w.canvas.rebuild()


def resolve_language(tr: Any, code: str) -> str:
    """``shell.language`` -> a language with texts: the chosen one, or for "" the system's
    language (QLocale), else English."""
    have = {li.code for li in tr.available()}
    if code:
        return code if code in have else "en"
    from PySide6.QtCore import QLocale
    sys_code = QLocale.system().name().split("_")[0].lower()
    return sys_code if sys_code in have else "en"


def apply_language(w: Any) -> None:
    code = resolve_language(w.tr_, str(w.config.get("shell.language") or ""))
    if code != w.tr_.language:
        w.set_language(code, persist=False)


def apply_shell(w: Any) -> None:
    """``shell.theme`` / ``shell.classic_ribbon`` changed outside the shell's own commands."""
    shell = getattr(w, "shell", None)
    if shell is None:
        return
    name = str(w.config.get("shell.theme", shell.theme))
    if name != shell.theme:
        shell.set_theme(name, persist=False)
    on = bool(w.config.get("shell.classic_ribbon", False))
    if on != getattr(shell, "classic", on):
        shell.set_classic(on, persist=False)


def _changed(w: Any, key: str) -> None:
    if key in ("*", "shell.theme", "shell.classic_ribbon"):
        apply_shell(w)
    if key == "*" or key.startswith("display."):
        apply_display(w)
    if key in ("*", "shell.language"):
        apply_language(w)


def _imported(w: Any, path: str, doc: Any) -> None:
    cfg = w.config
    if path and cfg.get("app.last_file") != path:
        cfg.set("app.last_file", str(path))
    if bool(cfg.get("import.cleanup_on_open")) and doc is not None and doc.parts:
        from mlaser.ops import cam as OC
        res = w._edit(OC.cleanup, cleanup_params(cfg), label="optimize", scope="all")
        if isinstance(res, dict):
            w.log(f"mf470: {res.get('before')} -> {res.get('after')} contours (Settings > Import: clean up on open)")


def register_homes(cfg: Any) -> None:
    """The value ids ``cfg:<key>`` of keys whose home is not the Settings window point at their
    home (mlaser.ui.homes); the Settings window's own keys keep the ``cfg:`` default."""
    from . import homes
    where = {"machine": homes.H_SETUP, "nest": homes.H_NEST, "run": homes.H_RUN}
    for key, s in cfg.schema.items():
        h = where.get(s.home)
        if s.home == "process":
            h = homes.H_LAYERS if s.source == "registry" else homes.H_PROPS
        if h is not None and homes.home_of(f"cfg:{key}") is homes.H_SETTINGS:
            homes.register_home(f"cfg:{key}", h)


def install(w: Any) -> None:
    cfg = w.config
    cfg.migrate_legacy()
    register_homes(cfg)
    apply_display(w)
    apply_shell(w)            # (the start-up language is chosen by mlaser.ui.__main__: --lang wins)
    cfg.subscribe(lambda key: _changed(w, key))
    # the menu bar (shell/menubar.py, Native track) gives app.settings the platform's name
    # (Settings… / Options… / Preferences…) and the PreferencesRole
    w.hooks.add("document_imported", lambda path, doc: _imported(w, path, doc), "settings")
