"""The Settings window's application preferences, applied to the main window and kept live.

:func:`install` (end of ``MainWindow.__init__``) takes the original's changed values into the
new keys once (:func:`mlaser.services.settings_catalog.migrate_legacy`), applies every preference
and subscribes to the ConfigService, so a change in the Settings window (or anywhere else)
reaches the running program at once:

* ``display.*``: the canvas view options (the ``show.*`` toggles of the View tab write them);
* ``cam.lead_*``: the lead dialog's starting values (``window.lead_in`` / ``lead_out``,
  closed-only, start mode) for the next lead applied;
* ``cam.sort_mode`` / ``cam.small_first``: the cutting order of the planned job (re-planned);
* ``cam.mj_*``: the micro-joint dialog's values (``window.mj_params``);
* ``shell.language``: the interface language;
* ``import.cleanup_on_open``: an opened drawing is cleaned up with the ``import.*`` values
  (the Optimize command's parameters, :func:`cleanup_params`); ``app.last_file`` records it.

``design.nudge_*`` is read by ext_draw, ``nest.*`` by the Nest workspace, ``shell.*`` by the
shell, each with its own listener.
"""
from __future__ import annotations

from typing import Any

from mlaser.model import LeadParams

#: canvas ViewOptions attribute -> preference
DISPLAY = {"show_bbox": "display.show_bbox", "unclosed_red": "display.unclosed_red",
           "show_index": "display.show_index", "show_start": "display.show_start",
           "show_direction": "display.show_direction", "show_move_path": "display.show_move_path"}
#: LeadParams field -> preference, per side
LEADS = {"in": {"kind": "cam.lead_in_type", "angle_deg": "cam.lead_in_angle", "length": "cam.lead_in_length",
                "radius": "cam.lead_in_radius"},
         "out": {"kind": "cam.lead_out_type", "angle_deg": "cam.lead_out_angle", "length": "cam.lead_out_length",
                 "radius": "cam.lead_out_radius"}}
#: MicroJointParams field -> preference
MICROJOINTS = {"kind": "cam.mj_kind", "count": "cam.mj_count", "distance": "cam.mj_distance",
               "length": "cam.mj_length", "min_size": "cam.mj_min_size"}
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


def lead_params(cfg: Any, side: str) -> LeadParams:
    k = LEADS[side]
    return LeadParams(kind=int(cfg.get(k["kind"])), angle_deg=float(cfg.get(k["angle_deg"])),
                      length=float(cfg.get(k["length"])), radius=float(cfg.get(k["radius"])))


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


def apply_leads(w: Any) -> None:
    cfg = w.config
    w.lead_in, w.lead_out = lead_params(cfg, "in"), lead_params(cfg, "out")
    w.lead_closed_only = bool(cfg.get("cam.lead_closed_only"))
    w.lead_start_mode = int(cfg.get("cam.lead_start_mode"))


def apply_order(w: Any, replan: bool = True) -> None:
    cfg, cp = w.config, w.cam_params
    mode, small = int(cfg.get("cam.sort_mode")), bool(cfg.get("cam.small_first"))
    if cp.sort_mode != mode or cp.small_first != small:
        cp.sort_mode, cp.small_first = mode, small
        if replan:
            w._update_toolpath()


def apply_microjoints(w: Any) -> None:
    cfg = w.config
    w.mj_params.update(**{f: (int if f in ("kind", "count") else float)(cfg.get(k)) for f, k in MICROJOINTS.items()})


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


def remember(w: Any, what: str) -> None:
    """A per-job dialog's values become its starting values next time (their one home is the
    dialog; the ConfigService only remembers them): ``"leads"``, ``"microjoints"`` or ``"order"``."""
    cfg = w.config
    vals: dict = {}
    if what == "leads":
        for side, lp in (("in", w.lead_in), ("out", w.lead_out)):
            vals.update({k: getattr(lp, f) for f, k in LEADS[side].items()})
        vals["cam.lead_closed_only"] = bool(w.lead_closed_only)
        vals["cam.lead_start_mode"] = int(w.lead_start_mode)
    elif what == "microjoints":
        vals = {k: getattr(w.mj_params, f) for f, k in MICROJOINTS.items()}
    elif what == "order":
        vals = {"cam.sort_mode": int(w.cam_params.sort_mode), "cam.small_first": bool(w.cam_params.small_first)}
    for k, v in vals.items():
        try:
            cfg.set(k, v)
        except ValueError:
            pass


def _changed(w: Any, key: str) -> None:
    if key in ("*", "shell.theme", "shell.classic_ribbon"):
        apply_shell(w)
    if key == "*" or key.startswith("display."):
        apply_display(w)
    if key == "*" or key.startswith("cam.lead_"):
        apply_leads(w)
    if key in ("*", "cam.sort_mode", "cam.small_first"):
        apply_order(w)
    if key == "*" or key.startswith("cam.mj_"):
        apply_microjoints(w)
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
    apply_leads(w)
    apply_order(w, replan=False)
    apply_microjoints(w)
    apply_shell(w)            # (the start-up language is chosen by mlaser.ui.__main__: --lang wins)
    cfg.subscribe(lambda key: _changed(w, key))
    from .settings_dialog import install_actions
    install_actions(w)          # the platform's name (Settings / Options / Preferences) and menu role
    w.hooks.add("document_imported", lambda path, doc: _imported(w, path, doc), "settings")
