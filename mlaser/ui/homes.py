"""One home per value (SETTINGS_MAP.md, WORK_ITEMS X-1): the registry of where each
user-editable value is edited, the bindings that mark the one editor and the read-only mirrors,
and "Edit in …" navigation.

* **Value ids**: ``layer.GP.CutSpeed`` (a layer's GP value), ``recipe.GP.CutSpeed`` (a technology
  record's), ``layer.craft.kerf_width`` / ``recipe.craft.lead_in.length`` (craft defaults on a
  layer / a record), ``craft.lead_in.length`` (a part's or contour's own value, Properties),
  ``cfg:<ConfigService key>``, ``nest.<field>``, ``run.<field>`` ...
* :data:`VALUE_HOMES` maps an id (or an id prefix ending in ``.``/``:``) to its :class:`Home`
  (workspace, section, words for the link).  :func:`home_of` resolves an id.
* :func:`bind_editor` marks the one widget that edits a value (dynamic properties
  ``mlaser_value`` / ``mlaser_role = "editor"``); :func:`bind_mirror` a widget that only shows
  it (``"mirror"``; its tooltip gets "Edit in …"); :func:`bind_transient` an input that is a tool
  or run parameter, not a setting.  ``tests/ui/test_one_home.py`` walks the whole application and
  fails when a value has editors on two surfaces, a mirror has no home, or an implemented home
  has no editor.
* :func:`edit_in` switches to the home, opens the section and focuses the field; a page that
  needs a context first (the layer inspector selecting layer 2) registers a resolver with
  :func:`register_resolver`.  :func:`nav_command` makes a kept command id (ribbon, menu, key)
  navigate to the home instead of opening a second editor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QAbstractButton, QDockWidget, QLabel, QTabWidget, QWidget

PROP_VALUE = "mlaser_value"
PROP_ROLE = "mlaser_role"
EDITOR, MIRROR, TRANSIENT, SHORTCUT = "editor", "mirror", "transient", "shortcut"


@dataclass(frozen=True)
class Home:
    """Where a value is edited.  ``workspace``: shell workspace id (``"process"``, ``"machine"``
    ...) or ``"settings"`` (the Settings window); ``section``: the page / section inside it;
    ``title``: i18n key of the words for "Edit in …" (``"Process › Layers"``); ``implemented``:
    the home exists in this build (the guard test then requires its editor)."""

    workspace: str
    section: str
    title: str
    implemented: bool = False


# --------------------------------------------------------------------------- registry
H_SETUP = Home("machine", "setup", "home.machine_setup")
H_LIBRARY = Home("process", "library", "home.process_library", implemented=True)
H_LAYERS = Home("process", "layers", "home.process_layers", implemented=True)
H_PROPS = Home("process", "properties", "home.process_properties", implemented=True)
H_NEST = Home("nest", "job", "home.nest_job")
H_RUN = Home("run", "run", "home.run")
H_SETTINGS = Home("settings", "", "home.settings")

#: id or id prefix -> home (the longest matching prefix wins; see :func:`home_of`)
VALUE_HOMES: dict[str, Home] = {
    "recipe.": H_LIBRARY,           # recipe.GP.*, recipe.craft.*, recipe.rules.*, recipe.notes ...
    "layer.": H_LAYERS,             # layer.GP.*, layer.craft.*, layer.recipe
    "craft.": H_PROPS,              # part / contour CAM values (Properties)
    "part.": H_PROPS,               # part.layer, part transform
    "nest.": H_NEST,
    "run.": H_RUN,
    "cfg:": H_SETTINGS,             # ConfigService keys: Settings unless the schema says otherwise
    "cfg:bed.": H_SETUP,
    "cfg:safety.": H_SETUP,
    "cfg:machine.": H_SETUP,
}

_RESOLVERS: list[tuple[str, Callable[[Any, str, Any], Optional[QWidget]]]] = []


def home_of(vid: str) -> Optional[Home]:
    """The home of value id ``vid`` (exact entry, else the longest matching prefix)."""
    if vid in VALUE_HOMES:
        return VALUE_HOMES[vid]
    best = None
    for k, h in VALUE_HOMES.items():
        if (k.endswith((".", ":")) and vid.startswith(k)) and (best is None or len(k) > len(best[0])):
            best = (k, h)
    return best[1] if best else None


def register_home(vid_or_prefix: str, home: Home) -> None:
    """A track declares (or moves) a home; an exact id overrides its prefix's home."""
    VALUE_HOMES[vid_or_prefix] = home


def register_resolver(prefix: str, fn: Callable[[Any, str, Any], Optional[QWidget]]) -> None:
    """``fn(window, vid, target)`` prepares the home for ``vid`` (selects the layer, part ...)
    and returns the editor widget, or None to let :func:`edit_in` search for it."""
    _RESOLVERS.append((prefix, fn))


# --------------------------------------------------------------------------- bindings
def bind_editor(widget: QWidget, vid: str) -> QWidget:
    widget.setProperty(PROP_VALUE, vid)
    widget.setProperty(PROP_ROLE, EDITOR)
    return widget


def bind_transient(widget: QWidget, what: str = "") -> QWidget:
    """An input that is a tool or run parameter (a value used once), not a setting."""
    widget.setProperty(PROP_VALUE, what or "transient")
    widget.setProperty(PROP_ROLE, TRANSIENT)
    return widget


def bind_mirror(widget: QWidget, vid: str, tr: Any = None) -> QWidget:
    """A read-only view of ``vid``; the tooltip says where it is edited."""
    widget.setProperty(PROP_VALUE, vid)
    widget.setProperty(PROP_ROLE, MIRROR)
    words = edit_in_text(vid, tr)
    tip = widget.toolTip()
    if words and words not in tip:
        widget.setToolTip((tip + "\n" if tip else "") + words)
    return widget


def edit_in_text(vid: str, tr: Any = None) -> str:
    h = home_of(vid)
    if h is None:
        return ""
    if tr is None:
        from .i18n import translator
        tr = translator()
    return tr.format("home.edit_in", home=tr.tr(h.title))


class EditInLink(QLabel):
    """A link "Edit in Process › Layers" that navigates to the home of ``vid``."""

    def __init__(self, window: Any, vid: str, target: Any = None, tr: Any = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.window_, self.vid, self.target = window, vid, target
        self.setText(f"<a href='#'>{edit_in_text(vid, tr)}</a>")
        self.setTextFormat(Qt.RichText)
        self.linkActivated.connect(lambda _h: edit_in(self.window_, self.vid, self.target))
        bind_mirror(self, vid, tr)


# --------------------------------------------------------------------------- navigation
def editors(root: QWidget, vid: Optional[str] = None, role: str = EDITOR) -> list[QWidget]:
    """Widgets under ``root`` bound to ``vid`` (every bound one when None) with ``role``."""
    out = []
    for w in [root] + root.findChildren(QWidget):
        v = w.property(PROP_VALUE)
        if v is None or w.property(PROP_ROLE) != role:
            continue
        if vid is None or v == vid:
            out.append(w)
    return out


def reveal(widget: QWidget) -> None:
    """Make ``widget`` visible where it lives: open the collapsed sections, select the tab page
    and raise the dock that hold it."""
    from .techform import Section

    w: Optional[QWidget] = widget
    while w is not None:
        p = w.parentWidget()
        if isinstance(w, Section) and not w.expanded():
            w.set_expanded(True)
        if isinstance(p, QWidget) and p.objectName() == "qt_tabwidget_stackedwidget":
            tabs = p.parentWidget()
            if isinstance(tabs, QTabWidget):
                tabs.setCurrentWidget(w)
        if isinstance(w, QDockWidget):
            w.show()
            w.raise_()
        w = p
    # a form's filters (search, "show all", a switch that is off) may hide the row
    form = widget
    while form is not None and form.objectName() != "techform":
        form = form.parentWidget()
    if form is not None and hasattr(form, "reveal_widget"):
        form.reveal_widget(widget)


def edit_in(window: Any, vid: str, target: Any = None) -> Optional[QWidget]:
    """Go to the home of ``vid``: show its workspace, let its resolver select the context,
    reveal the field and focus it.  Returns the editor, or None (no home / not built)."""
    h = home_of(vid)
    if h is None:
        return None
    if h.workspace == "settings":
        fn = getattr(window, "settings_dialog", None)
        if callable(fn):
            fn(vid[4:] if vid.startswith("cfg:") else vid)
        return None
    show = getattr(window, "show_workspace", None)
    if callable(show):
        if not show(h.workspace):
            for name in getattr(window, "workspaces", {}) or {}:
                if name.split(".", 1)[-1] == h.workspace and show(name):
                    break
    w = None
    for prefix, fn in sorted(_RESOLVERS, key=lambda r: -len(r[0])):
        if vid.startswith(prefix):
            w = fn(window, vid, target)
            if w is not None:
                break
    if w is None:
        found = editors(window, vid) if isinstance(window, QWidget) else []
        w = found[0] if found else None
    if w is None:
        return None
    reveal(w)
    w.setFocus()
    if hasattr(w, "selectAll"):
        try:
            w.selectAll()
        except TypeError:
            pass
    _flash(w)
    return w


def _flash(w: QWidget) -> None:
    """Show the user which field: a short focus highlight (palette, no colour literals)."""
    old = w.autoFillBackground()
    pal = w.palette()
    hi = w.palette()
    hi.setColor(w.backgroundRole(), hi.highlight().color())
    w.setAutoFillBackground(True)
    w.setPalette(hi)

    def back():
        try:
            w.setPalette(pal)
            w.setAutoFillBackground(old)
        except RuntimeError:
            pass
    QTimer.singleShot(600, back)


def nav_command(window: Any, cmd_id: str, vid: str, need_selection: bool = False,
                hint_key: str = "home.select_parts") -> Callable[[], Optional[QWidget]]:
    """A handler for a kept command id that navigates to the home of ``vid`` (instead of
    opening a second editor).  ``need_selection``: part values; with nothing selected the
    status bar says to select parts (and where the layer defaults are)."""
    def run() -> Optional[QWidget]:
        if need_selection and not any(p.selected for p in window.document.parts):
            msg = window.tr_.tr(hint_key)
            sb = getattr(window, "statusBar", None)
            if callable(sb):
                sb().showMessage(msg, 8000)
            lp = getattr(window, "log_panel", None)
            if lp is not None:
                lp.log(msg)
        return edit_in(window, vid)
    run.__name__ = f"nav_{cmd_id.replace('.', '_')}"
    return run


def is_input(w: QWidget) -> bool:
    """Whether ``w`` is a value input (for the "unbound input" check)."""
    from PySide6.QtWidgets import QAbstractSpinBox, QComboBox, QLineEdit

    if isinstance(w, (QAbstractSpinBox, QComboBox)):
        return True
    if isinstance(w, QLineEdit):
        return not isinstance(w.parentWidget(), (QAbstractSpinBox, QComboBox))
    from PySide6.QtWidgets import QCheckBox
    return isinstance(w, QCheckBox) and isinstance(w, QAbstractButton)


__all__ = ["Home", "VALUE_HOMES", "home_of", "register_home", "register_resolver", "bind_editor",
           "bind_mirror", "bind_transient", "edit_in", "nav_command", "editors", "reveal", "EditInLink",
           "edit_in_text", "is_input", "PROP_VALUE", "PROP_ROLE", "EDITOR", "MIRROR", "TRANSIENT", "SHORTCUT"]
