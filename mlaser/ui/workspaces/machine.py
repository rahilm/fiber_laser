"""The Machine workspace: connection, homing and jog, origins, guided procedures, monitors,
hardware settings and the alarm history (docs/user/machine.md).

Registered by ``ext_machine`` through ``ctx.add_workspace("machine", ...)``.  Until the Shell
track's workspace switcher lands, the command ``machine.workspace`` shows it as its own window
(or through a ``workspace.show`` provider when one is registered).  The device monitors of
``ext_diag`` open as panels here (provider ``machine.monitor_host``) instead of floating
dialogs.

User modes (Operator / Engineer / Service, docs/review/ux.md P1-10): every section and procedure
declares the lowest mode it needs (:data:`SECTIONS`, ``procedures.MIN_MODES``); the current mode
comes from the ``user_mode`` provider when the Shell registers one, else everything is open.
Hardware settings are read-only below Service.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QRadioButton,
                               QScrollArea, QSpinBox, QStyle, QTableWidget, QTableWidgetItem,
                               QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from mlaser.services.alarm_history import AlarmHistory, fmt_time
from mlaser.services import units as U
from mlaser.services.procedures import MIN_MODES, mode_allows

from .. import homes, icons
from ..responsive import (CollapsibleSection, ColumnFitter, ColumnsPanel, CurrentPageStack, FlowLayout, ResponsiveForm, flow,
                          mark_list)
from ..units_qt import QuantitySpinBox, kind_for, on_units_changed, spin_for_unit
from ..wizards.common import MIN_TARGET, JogPad, T

#: (id, title, icon, lowest mode)
SECTIONS = (("connection", "ws.machine.sec_connection", "plug-connected", "operator"),
            ("motion", "ws.machine.sec_motion", "arrows-move", "operator"),
            ("procedures", "ws.machine.sec_procedures", "wand", "operator"),
            ("monitors", "ws.machine.sec_monitors", "activity", "engineer"),
            ("settings", "ws.machine.sec_settings", "settings-cog", "engineer"),
            ("alarms", "ws.machine.sec_alarms", "alert-triangle", "operator"))
SETTINGS_EDIT_MODE = "service"
HW_SECTIONS = ("AxisParam", "MachineAxisConfig_0", "MachineAxisConfig", "HomeParam", "LaserParam",
               "GasParam", "ZFParam", "AFParam", "FCParam", "DAParam", "DIParam", "DOParam",
               "MachineControl")      # the machine-side parameters of the original's Options

#: the jog values (the jog pad is their home) and the coordinate system (Homing and jog)
JOG_KEYS = ("MC.IsStepMove", "MC.StepLength", "MC.IsFastMode")
MOTION_KEYS = JOG_KEYS + ("MSC.CoordSysType",)
#: runtime state, not settings (the selected mark point, recent files, test ids)
NOT_SETUP = frozenset({"MC.MarkPtIndex", "MSC.RecenFileIndex", "MSC.LatestFilePath", "MSC.TestCardID",
                       "MSC.TestPersonID"}) | frozenset(MOTION_KEYS)


def _pre(*prefixes: str):
    return lambda s: s.key.startswith(prefixes)


def _sec(*names: str):
    return lambda s: s.section in names


def _hard(s) -> bool:
    return s.file == "HardPara.xml"


#: Machine › Setup (WORK_ITEMS R-1): every machine setting in one editor, by topic.  (id, title
#: key, predicate over the ConfigService schema).  The card's HardPara rows that used to sit in
#: Options (precision compensation, crash protection, ...) are under Advanced; the machine
#: behaviour rows of Options (jog / rapid speeds, gas delays, coordinates, edge seek) have their
#: own sections; MachineAxisConfig_1..4 (no editor before) are the other axes.
SETUP_SECTIONS: tuple = (
    ("AxisParam", "ws.machine.hw_AxisParam", _sec("AxisParam")),
    ("MachineAxisConfig", "ws.machine.hw_MachineAxisConfig", _sec("MachineAxisConfig")),
    *((f"MachineAxisConfig_{i}", f"ws.machine.hw_MachineAxisConfig_{i}", _sec(f"MachineAxisConfig_{i}"))
      for i in range(5)),
    ("HomeParam", "ws.machine.hw_HomeParam", _sec("HomeParam")),
    # the machine-side rows of the original's Options (params.MACHINE_CONTROL: read by the
    # controller, the process sequencer and the job compiler), as the Settings track grouped them
    ("MachineControl", "ws.machine.hw_MachineControl", lambda s: s.key in _machine_control()),
    ("motion", "ws.machine.hw_motion", lambda s: s.section == "ManuParam" and not _hard(s)
     and s.key.startswith(("MC.", "MS.", "LC.", "LMP.", "ManuParam:LMP.", "MP."))),
    ("coords", "ws.machine.hw_coords", _pre("MSC.")),
    ("LaserParam", "ws.machine.hw_LaserParam", _sec("LaserParam")),
    ("GasParam", "ws.machine.hw_GasParam", lambda s: s.section == "GasParam" or s.key.startswith("GC.")),
    ("ZFParam", "ws.machine.hw_ZFParam", _sec("ZFParam")),
    ("AFParam", "ws.machine.hw_AFParam", lambda s: s.section == "AFParam"
     or (s.section == "ManuParam" and _hard(s) and s.key.startswith("AF."))),
    ("FCParam", "ws.machine.hw_FCParam", lambda s: s.section == "FCParam" or (s.section == "ManuParam"
                                                                               and s.key.startswith("FC."))),
    ("DAParam", "ws.machine.hw_DAParam", _sec("DAParam")),
    ("DIParam", "ws.machine.hw_DIParam", _sec("DIParam")),
    ("DOParam", "ws.machine.hw_DOParam", _sec("DOParam")),
    ("ethercat", "ws.machine.hw_ethercat", lambda s: s.section == "ECParam"
     or (s.section == "ManuParam" and _hard(s) and s.key.startswith("EC."))),
    ("edge", "ws.machine.hw_edge", lambda s: s.section == "GraphParam" and s.key.startswith("GRP.Edge")),
    ("advanced", "ws.machine.hw_advanced", lambda s: _hard(s) and (
        (s.section == "ManuParam" and s.key.startswith(("MP.", "MGP.")))
        or (s.section == "SoftParam" and s.key.startswith("SP.")))),
)


def _machine_control() -> frozenset:
    from ..params import MACHINE_CONTROL
    return frozenset(MACHINE_CONTROL)


def setup_keys(config: Any) -> dict[str, str]:
    """Schema key -> Setup section id, for every machine setting Machine › Setup edits (each
    key in exactly one section: the Options' machine rows in MachineControl first)."""
    out: dict[str, str] = {}
    order = sorted(SETUP_SECTIONS, key=lambda sec: sec[0] != "MachineControl")
    for s in config.schema.values():
        if s.key in NOT_SETUP or not s.label:
            continue
        for sid, _title, pred in order:
            if pred(s):
                out.setdefault(s.key, sid)
                break
    return out


H_SETUP = homes.Home("machine", "settings", "home.machine_setup", implemented=True)
H_JOG = homes.Home("machine", "motion", "home.machine_jog", implemented=True)


def register_homes(config: Any) -> dict[str, str]:
    """The Setup rows and the jog values have their home here (``cfg:<key>``)."""
    keys = setup_keys(config)
    for k in keys:
        homes.register_home(f"cfg:{k}", H_SETUP)
    for k in MOTION_KEYS:
        homes.register_home(f"cfg:{k}", H_JOG)
    homes.register_home("cfg:machine.setup", H_SETUP)        # the page (the menu bar's entry)
    return keys
MONITORS = (("controller", "ws.machine.mon_controller"), ("zf", "ws.machine.mon_follower"),
            ("af", "ws.machine.mon_autofocus"), ("ec", "ws.machine.mon_ethercat"))
#: (command, title, description, icon) of the procedure cards
PROCEDURES = (("wizard.edge_seek_cap4", "wizard.edge.title_cap4", "ws.machine.p_edge_cap", "radar-2", "edge_seek"),
              ("wizard.edge_seek_cap3", "wizard.edge.title_cap3", "ws.machine.p_edge_cap", "radar-2", "edge_seek"),
              ("wizard.edge_seek_disc", "wizard.edge.title_disc", "ws.machine.p_edge_disc", "circle-dashed", "edge_seek"),
              ("wizard.edge_seek_two_point", "wizard.edge.title_two_point", "ws.machine.p_edge_two", "line", "edge_seek"),
              ("wizard.follower_calibration", "wizard.follower.title", "ws.machine.p_follower", "wave-sine",
               "follower_calibration"),
              ("wizard.focus_home", "wizard.focus.title", "ws.machine.p_focus", "focus-2", "focus_home"),
              ("wizard.laser_test", "wizard.laser.title", "ws.machine.p_laser", "flare", "laser_test"),
              ("wizard.commissioning", "wizard.comm.title", "ws.machine.p_comm", "checks", "commissioning"),
              ("machine.import_install", "wizard.import.title", "ws.machine.p_import", "file-import",
               "settings_import"))


def current_mode(ctx: Any) -> Optional[str]:
    """The user mode from the Shell's ``user_mode`` provider (None: modes not configured)."""
    fn = ctx.host.hooks.provider("user_mode")
    if fn is None:
        return None
    try:
        return str(fn())
    except Exception:  # noqa: BLE001
        return None


def _big(label: QLabel, scale: float = 1.6, bold: bool = True) -> QLabel:
    f = label.font()
    f.setPointSizeF(f.pointSizeF() * scale)
    f.setBold(bold)
    label.setFont(f)
    return label


def _button(window: Any, key: str, fn: Callable, icon: str = "", color: Optional[str] = None,
            **fmt: Any) -> QPushButton:
    b = QPushButton(T(window, key, **fmt))
    if icon:
        b.setIcon(icons.icon(icon, color))
    b.setMinimumHeight(MIN_TARGET)
    b.clicked.connect(lambda _=False: fn())
    return b


class SettingsPanel(QWidget):
    """The hardware parameters through the ConfigService's typed schema: sections, search,
    typed editors with units and ranges; Apply validates (``ConfigService.set``) and reports
    the values the schema refuses.  Read-only below Service mode."""

    def __init__(self, window: Any, config: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.w, self.config = window, config
        self.editable = True
        self.errors: list[str] = []
        self._editors: dict[str, QWidget] = {}
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.section = QComboBox()
        self.section.setMinimumHeight(32)
        self.keys = register_homes(config)          # key -> section id
        homes.bind_transient(self.section, "machine.setup_section")
        self.section.setProperty("responsive_views", True)    # the layout harness visits each
        for sid, title, _pred in SETUP_SECTIONS:
            if sid in self.keys.values():
                self.section.addItem(T(window, title), sid)
        self.section.currentIndexChanged.connect(lambda _i: self.build())
        top.addWidget(QLabel(T(window, "ws.machine.hw_section")))
        top.addWidget(self.section, 1)
        self.search = QLineEdit()
        homes.bind_transient(self.search, "machine.setup_search")
        self.search.setPlaceholderText(T(window, "ws.machine.hw_search"))
        self.search.setClearButtonEnabled(True)
        self._all = False
        self.search.textChanged.connect(self._search)
        top.addWidget(self.search, 1)
        # the interlocks and the heartbeat have their own dialog (Engineer and Service mode)
        self.b_safety_io = _button(window, "safety.io.button", self._safety_io, "alert-triangle")
        self.b_safety_io.setObjectName("machine_safety_io")
        top.addWidget(self.b_safety_io)
        lay.addLayout(top)
        self.ro_note = QLabel(T(window, "ws.machine.hw_readonly"))
        self.ro_note.setWordWrap(True)
        lay.addWidget(self.ro_note)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([T(window, "ws.machine.hw_name"), T(window, "ws.machine.hw_value"),
                                   T(window, "ws.machine.hw_key")])
        hh = self.tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(1, QHeaderView.Fixed)     # as wide as the widest editor (build)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        # the name column stretches, the key column hides first in a narrow window
        self.columns = ColumnFitter(self.tree, {1: None, 2: 0}, stretch=0)
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.status = QLabel(T(window, "ws.machine.hw_note"))
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.b_revert = _button(window, "ws.machine.hw_revert", self.build)
        self.b_apply = _button(window, "ws.machine.hw_apply", self.apply)
        row.addWidget(self.b_revert)
        row.addWidget(self.b_apply)
        lay.addLayout(row)
        self.build()
        on_units_changed(self, self._relabel, now=False)

    def _safety_io(self) -> None:
        run = getattr(self.w, "run", None)
        if callable(run):
            run("machine.safety_io")

    def set_editable(self, on: bool) -> None:
        if on == self.editable:
            return
        self.editable = on
        self.ro_note.setVisible(not on)
        for e in self._editors.values():
            e.setEnabled(on)
        self.b_apply.setEnabled(on)

    def _editor(self, s) -> QWidget:
        v = self.config.get(s.key, s.default)
        if s.kind == "bool":
            w: QWidget = QCheckBox()
            w.setChecked(bool(v))
        elif s.kind == "enum" and s.options:
            w = QComboBox()
            tr = getattr(self.w, "tr_", None)
            w.addItems([tr.tr(o) if tr is not None and tr.has(o) else str(o) for o in s.options])
            try:
                w.setCurrentIndex(int(v))
            except (TypeError, ValueError):
                pass
        elif s.kind == "float":
            # mm, mm/s, mm/s² ...: a QuantitySpinBox (display units, value() stays in the
            # setting's own unit); other units (p/mm, %, ms) keep their suffix
            w = spin_for_unit(s.unit, s.min if s.min is not None else -1e9, s.max if s.max is not None else 1e9,
                              decimals=4, step=1.0)
            try:
                w.setValue(float(v))
            except (TypeError, ValueError):
                pass
        elif s.kind == "int":
            w = QSpinBox()
            w.setRange(int(max(s.min if s.min is not None else -2**31, -2**31)),
                       int(min(s.max if s.max is not None else 2**31 - 1, 2**31 - 1)))
            try:
                w.setValue(int(float(v)))
            except (TypeError, ValueError):
                pass
            if s.unit:
                w.setSuffix(f" {s.unit}")
        else:
            w = QLineEdit("" if v is None else str(v))
        w.setEnabled(self.editable)
        return w

    @staticmethod
    def _read(w: QWidget) -> Any:
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentIndex()
        if isinstance(w, (QSpinBox, QDoubleSpinBox, QuantitySpinBox)):
            return w.value()
        return w.text().strip()

    def build(self) -> None:
        self.tree.clear()
        self._editors = {}
        self._initial: dict[str, Any] = {}
        sec = self.section.currentData()
        if not sec:
            return
        tr = getattr(self.w, "tr_", None)
        groups: dict[str, QTreeWidgetItem] = {}
        # a search looks through every section of Setup, else the chosen section
        self._all = bool(self.search.text().strip())
        titles = {sid: T(self.w, title) for sid, title, _p in SETUP_SECTIONS}
        rows = [s for s in self.config.schema.values()
                if s.key in self.keys and (self._all or self.keys[s.key] == sec)]
        for s in rows:
            g, name = tr.tr_param(s.label) if tr is not None else ("", s.label)
            if self._all:
                g = f"{titles[self.keys[s.key]]} › {g}" if g else titles[self.keys[s.key]]
            top = groups.get(g)
            if top is None:
                top = QTreeWidgetItem([g or "–"])
                top.setFirstColumnSpanned(True)
                self.tree.addTopLevelItem(top)
                groups[g] = top
            it = QTreeWidgetItem([self._name(name, s), "", s.key])
            it.setData(0, Qt.UserRole, name)
            it.setToolTip(0, s.key)
            top.addChild(it)
            ed = self._editor(s)
            ed.setObjectName(f"setup_{s.key}")
            homes.bind_editor(ed, f"cfg:{s.key}")
            self.tree.setItemWidget(it, 1, ed)
            self._editors[s.key] = ed
            self._initial[s.key] = self._read(ed)
        self.tree.expandAll()
        if self._editors:
            self.tree.header().resizeSection(1, max(max(e.sizeHint().width(), e.minimumSizeHint().width())
                                                    for e in self._editors.values()))
        self._filter(self.search.text())
        self.columns.schedule()

    def _search(self, text: str) -> None:
        if bool(text.strip()) != self._all:
            self.build()                   # every section while searching, the chosen one after
        else:
            self._filter(text)

    def show_key(self, key: str) -> Optional[QWidget]:
        """Open the Setup section of ``key`` and return its editor (``homes.edit_in``)."""
        sid = self.keys.get(key)
        if sid is None:
            return None
        if self.search.text():
            self.search.clear()
        i = self.section.findData(sid)
        if i >= 0 and i != self.section.currentIndex():
            self.section.setCurrentIndex(i)
        ed = self._editors.get(key)
        if ed is not None:
            for j in range(self.tree.topLevelItemCount()):
                g = self.tree.topLevelItem(j)
                for k in range(g.childCount()):
                    if g.child(k).text(2) == key:
                        self.tree.scrollToItem(g.child(k))
        return ed

    @staticmethod
    def _name(name: str, s: Any) -> str:
        """The row title with the unit: the display unit for a converted number; a unit the
        registry puts on a switch or a choice ("mm" on GRP.IsShow*, ZF.ZFType) is dropped."""
        k = kind_for(s.unit) if s.unit else None
        if k is not None and s.kind == "float":
            return f"{name} ({U.current().suffix(k)})"
        if k is not None and s.kind in ("bool", "enum"):
            return name
        return name + (f" ({s.unit})" if s.unit else "")

    def _relabel(self, _u: Any = None) -> None:
        """Units switched: the row titles follow (the editors relabel themselves, edits kept)."""
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            for j in range(g.childCount()):
                c = g.child(j)
                s = self.config.setting(c.text(2))
                if s is not None and c.data(0, Qt.UserRole) is not None:
                    c.setText(0, self._name(c.data(0, Qt.UserRole), s))

    def _filter(self, text: str) -> None:
        t = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            any_vis = False
            for j in range(g.childCount()):
                c = g.child(j)
                vis = not t or t in c.text(0).lower() or t in c.text(2).lower()
                c.setHidden(not vis)
                any_vis |= vis
            g.setHidden(not any_vis)

    def changed(self) -> dict[str, Any]:
        return {k: self._read(w) for k, w in self._editors.items() if self._read(w) != self._initial.get(k)}

    def apply(self) -> int:
        if not self.editable:
            return 0
        self.errors = []
        n = 0
        for k, v in self.changed().items():
            try:
                self.config.set(k, v)
                n += 1
            except ValueError as e:
                self.errors.append(str(e))
        self.status.setText(T(self.w, "ws.machine.hw_applied", n=n)
                            + (("  ⚠ " + "; ".join(self.errors)) if self.errors else ""))
        self.build()
        return n


def _resolve(window: Any, vid: str, target: Any = None) -> Optional[QWidget]:
    """``homes.edit_in`` for the machine's values: open the section and return the editor."""
    key = vid[4:] if vid.startswith("cfg:") else vid
    if not isinstance(window, QWidget):
        return None
    ws = window.findChild(MachineWorkspace)
    if ws is None:
        return None
    if key == "machine.setup":                     # the page itself (menu bar: hw.options)
        ws.show_section("settings")
        return ws.settings_panel.section
    if key in MOTION_KEYS:
        ws.show_section("motion")
        return ws.coord if key == "MSC.CoordSysType" else (ws.jog.fast if key == "MC.IsFastMode"
                                                            else ws.jog.step_buttons[1])
    if key in ws.settings_panel.keys:
        ws.show_section("settings")
        return ws.settings_panel.show_key(key)
    return None


homes.register_resolver("cfg:", _resolve)


class MachineWorkspace(QWidget):
    def __init__(self, ctx: Any, ext: Any, history: Optional[AlarmHistory] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx, self.ext = ctx, ext
        self.w = ctx.window
        self.history = history or AlarmHistory()
        self.monitors: dict[str, QWidget] = {}
        self._mode: Optional[str] = "?"
        self.setObjectName("machineWorkspace")
        self.setWindowTitle(T(self.w, "ws.machine.title"))
        outer = QHBoxLayout(self)
        self.nav = QListWidget()
        self.nav.setObjectName("machine_nav")
        self.nav.setProperty("responsive_views", True)     # the layout harness visits each row
        self.nav.setIconSize(self.nav.iconSize() * 1.4)
        self.stack = CurrentPageStack()
        outer.addWidget(self.nav)
        outer.addWidget(self.stack, 1)
        self.pages: dict[str, QWidget] = {}
        builders = {"connection": self._connection, "motion": self._motion, "procedures": self._procedures,
                    "monitors": self._monitors, "settings": self._settings, "alarms": self._alarms}
        for sid, key, ic, _mode in SECTIONS:
            it = QListWidgetItem(icons.icon(ic), T(self.w, key))
            it.setData(Qt.UserRole, sid)
            it.setSizeHint(QSize(0, MIN_TARGET))
            self.nav.addItem(it)
            # no scroll area per section (the shell's page wrapper is the only one): the
            # section's lists and logs take the room that is left (docs/dev/RESPONSIVE.md)
            page = builders[sid]()
            self.pages[sid] = page
            self.stack.addWidget(page)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.history.listeners.append(lambda: QTimer.singleShot(0, self._fill_alarms))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._timer.start(200)
        self.tick()

    # ------------------------------------------------------------------ navigation
    def _fit_nav(self) -> None:
        """The navigation list is as wide as its longest entry (a content width, not a
        constant), so the page beside it gets the rest."""
        fm = self.nav.fontMetrics()
        text = max((fm.horizontalAdvance(self.nav.item(i).text()) for i in range(self.nav.count())), default=0)
        pad = self.nav.style().pixelMetric(QStyle.PM_FocusFrameHMargin, None, self.nav) + 1
        want = text + self.nav.iconSize().width() + 6 * pad + 3 * fm.horizontalAdvance("M") + 2 * self.nav.frameWidth()
        if self.nav.verticalScrollBar().isVisible():
            want += self.nav.verticalScrollBar().sizeHint().width()
        if want > 0 and self.nav.width() != want:
            self.nav.setFixedWidth(want)

    def showEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().showEvent(ev)
        self._fit_nav()

    def changeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().changeEvent(ev)
        if ev.type() in (QEvent.FontChange, QEvent.StyleChange, QEvent.LanguageChange):
            self._fit_nav()

    def show_section(self, sid: str) -> None:
        for i in range(self.nav.count()):
            if self.nav.item(i).data(Qt.UserRole) == sid:
                self.nav.setCurrentRow(i)

    def section(self) -> str:
        it = self.nav.currentItem()
        return it.data(Qt.UserRole) if it is not None else ""

    # ------------------------------------------------------------------ connection
    def _connection(self) -> QWidget:
        w = self.w
        # connect, status and the network self-test as sections: side by side when the page is
        # wide enough, folded from the self-test up when it is short (ui/responsive.py)
        page = ColumnsPanel(max_columns=2, compact=False)
        page.setObjectName("machine_connection")
        box = QWidget()
        g = QGridLayout(box)
        g.setContentsMargins(0, 0, 0, 0)
        self.r_sim = QRadioButton(T(w, "ws.machine.simulator"))
        self.r_real = QRadioButton(T(w, "ws.machine.real_card"))
        self.r_sim.setChecked(True)
        g.addWidget(self.r_sim, 0, 0)
        g.addWidget(self.r_real, 0, 1)
        ip = self.ctx.config.get("ip", None) or {}
        self.host = QLineEdit(ip.get("pd445", "10.1.1.168"))
        self.host.setMinimumHeight(32)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(502)
        self.port.setMinimumHeight(32)
        g.addWidget(QLabel(T(w, "ws.machine.host")), 1, 0)
        g.addWidget(self.host, 1, 1)
        g.addWidget(QLabel(T(w, "ws.machine.port")), 2, 0)
        g.addWidget(self.port, 2, 1)
        row = QHBoxLayout()
        row.addWidget(_button(w, "ws.machine.connect", self.connect, "plug-connected", icons.GREEN))
        row.addWidget(_button(w, "ws.machine.disconnect", self.disconnect, "plug-connected-x"))
        g.addLayout(row, 3, 0, 1, 2)
        page.add_section(CollapsibleSection(T(w, "ws.machine.connect_box"), box, collapsible=False))
        st = QWidget()
        f = QFormLayout(st)
        f.setContentsMargins(0, 0, 0, 0)
        self.lbl: dict[str, QLabel] = {}
        for k in ("conn", "kind", "safety", "endpoint", "link", "homed"):
            lb = QLabel("–")
            lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.lbl[k] = lb
            f.addRow(T(w, f"ws.machine.st_{k}"), lb)
        page.add_section(CollapsibleSection(T(w, "ws.machine.status_box"), st, priority=2))
        nc = QWidget()
        v = QVBoxLayout(nc)
        v.setContentsMargins(0, 0, 0, 0)
        self.selftest = QPlainTextEdit()
        self.selftest.setReadOnly(True)
        self.selftest.setMinimumHeight(self.selftest.fontMetrics().lineSpacing() * 5)
        v.addWidget(self.selftest)
        row = QHBoxLayout()
        self.probe = QCheckBox(T(w, "ws.machine.selftest_probe"))
        row.addWidget(self.probe)
        row.addStretch(1)
        row.addWidget(_button(w, "ws.machine.selftest_run", self.run_selftest, "network"))
        v.addLayout(row)
        page.add_section(CollapsibleSection(T(w, "ws.machine.selftest_box"), nc, priority=1))
        self._load_selftest()
        return page

    def _load_selftest(self) -> None:
        from mlaser import netcheck
        last = netcheck.last_result()
        self.selftest.setPlainText(last.text() if last is not None else T(self.w, "ws.machine.selftest_none"))

    def run_selftest(self) -> Any:
        from mlaser import netcheck
        ep = self.ctx.controller.endpoint()
        host = self.host.text().strip() if self.r_real.isChecked() or ep is None else ep[0]
        port = self.port.value() if self.r_real.isChecked() or ep is None else ep[1]
        res = netcheck.run(host, port, probe=self.probe.isChecked())
        try:
            netcheck.save_result(res)
        except OSError:
            pass
        self.selftest.setPlainText(res.text())
        return res

    def connect(self) -> bool:
        if self.r_sim.isChecked():
            return bool(self.ctx.commands.run("hw.connect_sim"))
        from mlaser.controller.ui_adapter import UiController
        ctl = self.ctx.controller
        if not isinstance(ctl, UiController) or ctl.is_simulator():
            ctl = UiController(machine=self.w.machine, cam_params=self.w.cam_params)
            self.w.set_controller(ctl)
        ok = bool(ctl.connect(self.host.text().strip(), self.port.value()))
        self.ctx.log("mf165" if ok else "mf166")
        return ok

    def disconnect(self) -> None:
        self.ctx.controller.disconnect()

    # ------------------------------------------------------------------ motion
    def _motion(self) -> QWidget:
        """Position, homing, jog and coordinates as sections in one or two columns (as the width
        allows), folded from the coordinates up when the page is short (ui/responsive.py)."""
        w = self.w
        page = ColumnsPanel(max_columns=2, compact=False)
        page.setObjectName("machine_motion")
        pos = QWidget()
        h = QHBoxLayout(pos)
        h.setContentsMargins(0, 0, 0, 0)
        self.pos_lbl = _big(QLabel("X –   Y –   Z –"), 1.8)
        self.pos_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        h.addWidget(self.pos_lbl, 1)
        self.homed_lbl = QLabel()
        h.addWidget(self.homed_lbl)
        page.add_section(CollapsibleSection(T(w, "ws.machine.position_box"), pos, collapsible=False))
        # homing: text buttons (the section title says what they do; no repeated home icon)
        homing = flow([_button(w, key, lambda c=cmd: self.ctx.commands.run(c))
                       for key, cmd in (("ws.machine.home_xy", "machine.home"), ("ws.machine.home_x", "machine.home_x"),
                                        ("ws.machine.home_y", "machine.home_y"),
                                        ("ws.machine.home_xyz", "machine.home_xyz"),
                                        ("ws.machine.home_focus", "wizard.focus_home"))], uniform=True)
        page.add_section(CollapsibleSection(T(w, "ws.machine.homing_box"), homing, priority=2))
        self.jog = JogPad(w, lambda: self.ctx.controller, z=True, home=True)     # the jog values' home
        self.jog.log.connect(self.ctx.log)
        page.add_section(CollapsibleSection(T(w, "ws.machine.jog_box"), self.jog, priority=3))
        cb = QWidget()
        cl = QVBoxLayout(cb)
        cl.setContentsMargins(0, 0, 0, 0)
        from mlaser.controller import coords as C
        self.coord = QComboBox()
        self.coord.setMinimumHeight(MIN_TARGET)
        tr = getattr(w, "tr_", None)
        for i in range(8):
            self.coord.addItem(tr.tr(C.NAMES[i]) if tr is not None else C.NAMES[i], i)
        self.coord.setCurrentIndex(self.ext.ops.coords.kind)
        self.coord.activated.connect(lambda i: self.ext.select_coord(int(self.coord.itemData(i))))
        homes.bind_editor(self.coord, "cfg:MSC.CoordSysType")
        form = ResponsiveForm()
        form.addRow(QLabel(T(w, "ws.machine.coord_system")), self.coord)
        cl.addWidget(form)
        cl.addWidget(flow([
            _button(w, "ws.machine.set_origin", lambda: self.ctx.commands.run("coord.set_origin"), "map-pin-plus"),
            _button(w, "ws.machine.return_origin", lambda: self.ctx.commands.run("coord.return_origin"),
                    "current-location"),
            _button(w, "ws.machine.edge_clear", lambda: self.ctx.commands.run("machine.edge_clear"), "eraser")],
            uniform=True))
        self.origins = QLabel()
        self.origins.setWordWrap(True)
        self.origins.setTextInteractionFlags(Qt.TextSelectableByMouse)
        cl.addWidget(self.origins)
        page.add_section(CollapsibleSection(T(w, "ws.machine.coords_box"), cb, priority=1))
        return page

    # ------------------------------------------------------------------ procedures
    def _procedures(self) -> QWidget:
        w = self.w
        cards = QWidget()
        g = FlowLayout(cards, uniform=True)         # as many card columns as the width allows
        self.proc_buttons: dict[str, QPushButton] = {}
        for i, (cmd, title, desc, ic, pid) in enumerate(PROCEDURES):
            card = QFrame()
            card.setFrameShape(QFrame.StyledPanel)
            v = QVBoxLayout(card)
            head = QHBoxLayout()
            pic = QLabel()
            pic.setPixmap(icons.icon(ic).pixmap(24, 24))     # the procedure's icon on its title
            head.addWidget(pic)
            t = _big(QLabel(T(w, title)), 1.15)
            t.setWordWrap(True)
            head.addWidget(t, 1)
            v.addLayout(head)
            d = QLabel(T(w, desc))
            d.setWordWrap(True)
            v.addWidget(d, 1)
            mode = MIN_MODES.get(pid, "operator")
            m = QLabel(T(w, "ws.machine.min_mode", mode=T(w, f"ws.machine.mode_{mode}")))
            m.setEnabled(False)
            v.addWidget(m)
            b = _button(w, "ws.machine.start_procedure", lambda c=cmd: self.ctx.commands.run(c))
            b.setProperty("min_mode", mode)
            v.addWidget(b)
            self.proc_buttons[cmd] = b
            g.addWidget(card)
        page = QScrollArea()                        # a list of cards: it may scroll (mark_list)
        page.setObjectName("machine_procedures")
        page.setFrameShape(QFrame.NoFrame)
        page.setWidgetResizable(True)
        page.setWidget(cards)
        mark_list(page)
        return page

    # ------------------------------------------------------------------ monitors
    def _monitors(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        self.mon_tabs = QTabWidget()
        self._mon_index: dict[str, int] = {}
        for key, title in MONITORS:
            holder = QWidget()
            QVBoxLayout(holder).setContentsMargins(0, 0, 0, 0)
            self._mon_index[key] = self.mon_tabs.addTab(holder, T(self.w, title))
        self.mon_tabs.currentChanged.connect(self._mon_tab)
        lay.addWidget(self.mon_tabs)
        return page

    def _mon_tab(self, i: int) -> None:
        if self.section() != "monitors":
            return
        key = MONITORS[i][0] if 0 <= i < len(MONITORS) else None
        if key is not None and key not in self.monitors:
            diag = self.w.extension("ext_diag")
            if diag is not None:
                diag.show_monitor(key)

    def embed_monitor(self, key: str, factory: Callable[[], QWidget]) -> QWidget:
        """``machine.monitor_host`` provider: the monitor ``key`` as a panel of this page."""
        mon = self.monitors.get(key)
        if mon is None:
            mon = factory()
            mon.setWindowFlags(Qt.Widget)
            holder = self.mon_tabs.widget(self._mon_index.get(key, 0))
            holder.layout().addWidget(mon)
            self.monitors[key] = mon
        mon.show()
        self.show_section("monitors")
        self.mon_tabs.setCurrentIndex(self._mon_index.get(key, 0))
        self.ext.show_workspace()
        return mon

    # ------------------------------------------------------------------ settings
    def _settings(self) -> QWidget:
        self.settings_panel = SettingsPanel(self.w, self.ctx.config)
        return self.settings_panel

    # ------------------------------------------------------------------ alarms
    def _alarms(self) -> QWidget:
        w = self.w
        page = QWidget()
        lay = QVBoxLayout(page)
        top = QHBoxLayout()
        self.active_lbl = _big(QLabel(), 1.2)
        top.addWidget(self.active_lbl, 1)
        top.addWidget(_button(w, "ws.machine.alarm_reset", lambda: self.ctx.commands.run("card.clear_alarms"),
                              "bell-off"))
        top.addWidget(_button(w, "ws.machine.alarm_clear_history", self._clear_history, "trash"))
        lay.addLayout(top)
        self.alarm_table = QTableWidget(0, 6)
        self.alarm_table.setHorizontalHeaderLabels([T(w, k) for k in (
            "ws.machine.a_raised", "ws.machine.a_cleared", "ws.machine.a_duration", "ws.machine.a_code",
            "ws.machine.a_text", "ws.machine.a_remedy")])
        self.alarm_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.alarm_table.horizontalHeader().setStretchLastSection(True)
        self.alarm_table.verticalHeader().hide()
        self.alarm_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.alarm_table.setWordWrap(True)
        lay.addWidget(self.alarm_table, 1)
        self._fill_alarms()
        return page

    def _clear_history(self) -> None:
        self.history.clear()

    def _fill_alarms(self) -> None:
        from ..safety_texts import remedy
        t = getattr(self, "alarm_table", None)
        if t is None:
            return
        rows = self.history.entries()
        t.setRowCount(len(rows))
        tr = getattr(self.w, "tr_", None)
        for i, r in enumerate(rows):
            dur = r.duration_s
            try:
                rem = remedy(r.text if r.code is None else _AlarmLike(r.code, r.text, r.alarm_kind), tr)
            except Exception:  # noqa: BLE001
                rem = ""
            cells = (fmt_time(r.raised), fmt_time(r.cleared) or T(self.w, "ws.machine.a_active"),
                     "" if dur is None else f"{dur:.1f} s",
                     T(self.w, "ws.machine.a_fault") if r.kind == "fault" else ("" if r.code is None else str(r.code)),
                     r.text, rem)
            for j, v in enumerate(cells):
                it = QTableWidgetItem(v)
                if j == 1 and r.active:
                    from PySide6.QtGui import QColor
                    it.setForeground(QColor(icons.RED))
                t.setItem(i, j, it)
        t.resizeRowsToContents()
        n = len(self.history.active())
        self.active_lbl.setText(T(self.w, "ws.machine.alarms_active", n=n) if n else T(self.w, "ws.machine.alarms_none"))

    # ------------------------------------------------------------------ periodic
    def tick(self) -> None:
        mode = current_mode(self.ctx)
        if mode != self._mode:
            self._mode = mode
            self.apply_mode(mode)
        if not self.isVisible():
            return
        ctl = self.ctx.controller
        try:
            st = ctl.state()
            saf = ctl.safety()
        except Exception:  # noqa: BLE001
            return
        y, n = T(self.w, "wizard.yes"), T(self.w, "wizard.no")
        self.lbl["conn"].setText(T(self.w, "ws.machine.connected") if st.connected else T(self.w, "ws.machine.offline"))
        self.lbl["kind"].setText(T(self.w, "ws.machine.simulator") if ctl.is_simulator() else
                                 (T(self.w, "ws.machine.real_card") if ctl.card() is not None else T(self.w, "ws.machine.demo")))
        self.lbl["safety"].setText(saf.state.value.upper() + (f" – {saf.fault}" if saf.fault else ""))
        ep = ctl.endpoint()
        self.lbl["endpoint"].setText(f"{ep[0]}:{ep[1]}" if ep else "–")
        self.lbl["link"].setText(y if st.link_ok else n)
        self.lbl["homed"].setText(y if st.homed else n)
        u = U.current()
        d = u.decimals("small_length")            # 0.001 mm / 0.0001 in
        self.pos_lbl.setText(f"X {u.number(st.x, 'length', d):>9}   Y {u.number(st.y, 'length', d):>9}   "
                             f"Z {u.number(st.z, 'length', d):>7}  {u.suffix('length')}")
        self.homed_lbl.setText(T(self.w, "ws.machine.homed") if st.homed else T(self.w, "ws.machine.not_homed"))
        c = self.ext.ops.coords
        if self.coord.currentIndex() != c.kind:
            self.coord.setCurrentIndex(c.kind)
        self.origins.setText(T(self.w, "ws.machine.origins", unit=u.suffix("length"), tools="  ".join(
            f"{i + 1}: ({u.number(x, 'length')}, {u.number(y_, 'length')})" for i, (x, y_) in enumerate(c.tools))))

    def apply_mode(self, mode: Optional[str]) -> None:
        for i, (sid, _k, _ic, need) in enumerate(SECTIONS):
            it = self.nav.item(i)
            ok = mode_allows(mode, need)
            it.setFlags((it.flags() | Qt.ItemIsEnabled) if ok else (it.flags() & ~Qt.ItemIsEnabled))
        for b in self.proc_buttons.values():
            b.setEnabled(mode_allows(mode, b.property("min_mode") or "operator"))
        self.settings_panel.set_editable(mode_allows(mode, SETTINGS_EDIT_MODE))
        if not mode_allows(mode, dict((s[0], s[3]) for s in SECTIONS).get(self.section(), "operator")):
            self.nav.setCurrentRow(0)

    def hideEvent(self, ev) -> None:
        self.jog.release_all("workspace hidden")
        super().hideEvent(ev)


class _AlarmLike:
    """Remedy lookup for a stored record (safety_texts.remedy reads ``code`` / ``key``)."""

    def __init__(self, code: int, text: str, kind: str = "card") -> None:
        self.code, self._text, self.key, self.kind = code, text, "", kind

    def text(self, tr=None) -> str:
        return self._text


__all__ = ["MachineWorkspace", "SettingsPanel", "SECTIONS", "PROCEDURES", "current_mode"]
