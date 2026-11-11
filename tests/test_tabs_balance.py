"""TabsWhenNarrow(balance=(i, j)): two panes share their width so that the taller one needs
the least height (UI review 2026-11-09 finding 3, at a 12 pt font: the Nest page's parts
column took the width its one-row fields wanted and the sheet column's parameters wrapped one
per row); a width the user chose with the splitter is kept.  The log under the page gives way
down to its tab bar (tests/test_layout_fit.py checks the whole window at 10 and 12 pt)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _pane(n: int, width: int) -> QWidget:
    """A pane of ``n`` buttons ``width`` px wide that wrap (taller when narrower)."""
    from mlaser.ui.responsive import FlowLayout
    p = QWidget()
    v = QVBoxLayout(p)
    v.setContentsMargins(0, 0, 0, 0)
    flow = FlowLayout(justify=False)
    for i in range(n):
        b = QPushButton(f"b{i}")
        b.setFixedSize(width, 40)
        flow.addWidget(b)
    v.addLayout(flow)
    return p


def _split(app, balance):
    from mlaser.ui.responsive import TabsWhenNarrow
    top = QWidget()
    split = TabsWhenNarrow(balance=balance, parent=top)
    left, mid, right = _pane(6, 100), _pane(6, 100), _pane(1, 100)
    for p in (left, mid, right):
        split.addWidget(p, "pane")
    # the left pane wants most of the width: the middle one wraps into six rows
    split.set_sizes([640, 110, 150])
    top.resize(920, 600)
    split.resize(920, 600)
    top.show()
    for _ in range(5):
        app.processEvents()
    return top, split


def _tallest(split) -> int:
    return max(split._height_at(p, w) for (p, _t), w in zip(split.panes[:2], split.splitter.sizes()[:2]))


def test_balanced_panes_need_the_least_height(app):
    top, split = _split(app, None)
    try:
        unbalanced = _tallest(split)
    finally:
        top.close()
        top.deleteLater()
    top, split = _split(app, (0, 1))
    try:
        sizes = split.splitter.sizes()
        assert not split.tabbed
        assert _tallest(split) < unbalanced, (sizes, unbalanced)
        # two rows of three buttons each, side by side (6 rows before)
        assert _tallest(split) <= 2 * 40 + 20, sizes
        assert sizes[2] == 150 or abs(sizes[2] - 150) <= 2       # the other pane keeps its width
    finally:
        top.close()
        top.deleteLater()
        app.processEvents()


def test_a_giving_pane_lends_width_only_while_it_lowers_the_height(app):
    """``give``: a pane without rows that wrap lends the balanced pair width, down to its own
    minimum, when one of them would wrap a row more (a font with wider text: macOS, the Nest
    page at 12 pt), and takes it back in a wider window."""
    from mlaser.ui.responsive import TabsWhenNarrow
    top = QWidget()
    split = TabsWhenNarrow(balance=(0, 1), give=2, parent=top)
    left, mid, right = _pane(3, 100), _pane(3, 100), _pane(1, 100)
    for p in (left, mid, right):
        split.addWidget(p, "pane")
    split.set_sizes([310, 310, 290])
    top.resize(920, 600)
    split.resize(920, 600)
    top.show()
    try:
        for _ in range(5):
            app.processEvents()
        row = left.layout().minimumHeightForWidth(1000)            # one row of three buttons
        sizes = split.splitter.sizes()
        assert _tallest(split) == row, sizes                         # neither wraps
        assert 100 <= sizes[2] < 290, sizes                          # lent from the giving pane
        top.resize(1400, 600)
        split.resize(1400, 600)
        for _ in range(5):
            app.processEvents()
        split.balance_panes()
        assert split.splitter.sizes()[2] >= 290, split.splitter.sizes()   # taken back
        assert _tallest(split) == row
    finally:
        top.close()
        top.deleteLater()
        app.processEvents()


def test_a_width_the_user_chose_is_kept(app):
    top, split = _split(app, (0, 1))
    try:
        split._user_moved()
        split.splitter.setSizes([500, 250, split.splitter.sizes()[2]])
        sizes = split.splitter.sizes()
        split._balanced_for = None
        split.balance_panes()
        assert split.splitter.sizes() == sizes
    finally:
        top.close()
        top.deleteLater()
        app.processEvents()


def test_the_log_gives_way_down_to_its_tab_bar(app):
    from mlaser.ui.i18n import Translator
    from mlaser.ui.panels import LogPanel
    p = LogPanel(Translator())
    try:
        assert p.minimumSizeHint().height() < p.tabBar().sizeHint().height() + p.text.fontMetrics().lineSpacing()
        p.resize(600, p.minimumSizeHint().height())
        p.show()
        app.processEvents()
        from PySide6.QtCore import Qt
        assert p.text.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff   # none squeezed in
        p.resize(600, 200)
        app.processEvents()
        assert p.text.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded
    finally:
        p.close()
        p.deleteLater()
        app.processEvents()
