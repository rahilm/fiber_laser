"""The part list follows a canvas selection at a cost that does not ask the model about every
selected row (mlaser/ui/part_list.py ``sync_selection``): finding the first selected row with
``selectedRows()`` called the model's ``flags`` for every column of every selected row, 21,600
Python calls for a select-all of 2,700 parts (1.4 s under the coverage tracer, CI)."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.ui

from test_part_list import app, grid, pump, win  # noqa: E402,F401,F811 - the fixtures


def _model_calls(fn) -> int:
    """Calls of the model's Python methods (flags, rowCount, columnCount, data) while ``fn``
    runs, counted by the profiler (Qt calls them from C++: a patched method is not seen)."""
    import cProfile
    import pstats
    pr = cProfile.Profile()
    pr.enable()
    try:
        fn()
    finally:
        pr.disable()
    st = pstats.Stats(pr).stats      # (file, line, name) -> (cc, nc, tt, ct, callers)
    return sum(v[1] for (f, _l, name), v in st.items()
               if f.replace("\\", "/").endswith("mlaser/ui/part_list.py")
               and name in ("flags", "rowCount", "columnCount", "data"))


def test_select_all_sync_does_not_query_every_row(win, app):  # noqa: F811
    n = 600
    win.set_document(grid(n))
    pump(app)
    pl = win.part_list
    for p in win.document.parts:
        p.selected = True
    sync_all = _model_calls(pl.sync_selection)
    rows = {ix.row() for ix in pl.view.selectionModel().selectedRows()}
    assert len(rows) == n
    # one selection range: the visible rows' worth of calls, not every selected row's columns
    # (16,000 before)
    assert sync_all < n, sync_all
