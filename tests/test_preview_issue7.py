"""Issue #7: "Preview in process can make UI unresponsive".

* "Previous contour" while the preview plays kept returning to the start of the same contour
  (the time is already a little past the start it was sent to): within a short grace after a
  contour's start it now goes to the contour before it, as a media player.
* Every 33 ms frame rebuilt the whole drawn path (up to 60 000 points in Python), so with a
  real job the frames alone kept the event loop busy and the buttons stopped answering while
  the preview went on.  The path is now joined from cached chunks, and a slow frame stretches
  the next interval."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QGraphicsScene, QGraphicsView  # noqa: E402

from mlaser.ui import i18n  # noqa: E402
from mlaser.ui.preview_scrubber import PreviewOverlay, ScrubberWidget  # noqa: E402

from test_preview_cost import _plan  # noqa: E402
from mlaser.services.preview import build_timeline  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def tl():
    _js, plan = _plan()
    return build_timeline(plan)


def _starts(tl):
    return sorted({m[0] for m in tl.markers})


def test_previous_contour_has_a_grace_after_a_start(tl):
    ts = _starts(tl)
    assert len(ts) >= 4
    # exactly at a start, or a moment after it: the contour before
    assert tl.next_contour_time(ts[2], -1) == ts[1]
    assert tl.next_contour_time(ts[2] + 0.05, -1, grace=0.6) == ts[1]
    # well inside a contour: its own start
    mid = ts[2] + (ts[3] - ts[2]) / 2
    if mid - ts[2] > 0.6:
        assert tl.next_contour_time(mid, -1, grace=0.6) == ts[2]
    # the first contour never goes further back
    assert tl.next_contour_time(ts[0] + 0.01, -1, grace=0.6) == ts[0]
    assert tl.next_contour_time(ts[1], 1) == ts[2]


def test_previous_while_playing_walks_back_through_the_contours(app, tl):
    tr = i18n.Translator(language="en")
    sc = ScrubberWidget(tr)
    sc.set_timeline(tl)
    ts = _starts(tl)
    sc.seek(ts[4] + 0.02)
    sc.play()
    try:
        assert sc.playing
        seen = []
        for _ in range(3):                    # three quick presses while it plays
            sc.step_contour(-1)
            seen.append(sc.t)
            sc.seek(sc.t + 0.02)               # the playback moved on a little
        assert seen == [ts[3], ts[2], ts[1]]
    finally:
        sc.pause()


def _elements(path):
    """The drawn line segments ((x0, y0), (x1, y1)) in order: a chunk boundary adds a move to the
    point the previous chunk ended at, which draws nothing."""
    from PySide6.QtGui import QPainterPath
    out, cur = [], None
    for k in range(path.elementCount()):
        e = path.elementAt(k)
        pt = (round(e.x, 9), round(e.y, 9))
        if e.type == QPainterPath.ElementType.LineToElement and cur is not None:
            out.append((cur, pt))
        cur = pt
    return out


def _reference(tl, t):
    """The previous full rebuild (Timeline.path_until), for comparison."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPainterPath
    xs, ys, las = tl.path_until(t, max_points=10 ** 9)
    cut, rap = QPainterPath(), QPainterPath()
    for k in range(1, len(xs)):
        a, b = QPointF(xs[k - 1], ys[k - 1]), QPointF(xs[k], ys[k])
        path = cut if las[k] else rap
        if path.currentPosition() != a or path.elementCount() == 0:
            path.moveTo(a)
        path.lineTo(b)
    return cut, rap


def test_chunked_path_draws_what_the_full_rebuild_drew(app, tl):
    scene = QGraphicsScene()                  # kept: the view does not own it
    view = QGraphicsView(scene)
    ov = PreviewOverlay(view)
    ov.CHUNK = 37                             # many chunk boundaries on the demo job
    ov.MAX_POINTS = 10 ** 9                   # no thinning: compare point for point
    ov.set_timeline(tl)
    for frac in (0.0, 0.003, 0.17, 0.5, 0.731, 0.999, 1.0):
        t = tl.total_s * frac
        ov.set_time(t)
        cut, rap = _reference(tl, t)
        assert _elements(ov._cut.path()) == _elements(cut), frac
        assert _elements(ov._rapid.path()) == _elements(rap), frac
    # backwards (a seek to an earlier contour) after forwards: still the same picture
    ov.set_time(tl.total_s * 0.2)
    cut, rap = _reference(tl, tl.total_s * 0.2)
    assert _elements(ov._cut.path()) == _elements(cut)


def test_a_frame_costs_a_fraction_of_the_full_rebuild(app, tl):
    scene = QGraphicsScene()                  # kept: the view does not own it
    view = QGraphicsView(scene)
    ov = PreviewOverlay(view)
    ov.CHUNK = 200
    ov.set_timeline(tl)
    ov.set_time(tl.total_s)                   # warm the chunk cache once
    t0 = time.perf_counter()
    for k in range(5):
        ov.set_time(tl.total_s * (0.99 + k * 0.002))
    frame = (time.perf_counter() - t0) / 5
    t0 = time.perf_counter()
    for _ in range(5):
        _reference(tl, tl.total_s)
    full = (time.perf_counter() - t0) / 5
    assert frame < 0.6 * full, (frame, full)


def test_a_slow_frame_stretches_the_next_interval(app, tl, monkeypatch):
    tr = i18n.Translator(language="en")
    sc = ScrubberWidget(tr)
    sc.set_timeline(tl)
    real_seek = sc.seek

    def slow_seek(t, from_slider=False):
        time.sleep(0.08)
        real_seek(t, from_slider)
    monkeypatch.setattr(sc, "seek", slow_seek)
    sc.play()
    try:
        sc._tick()
        assert sc._timer.interval() >= 150      # twice the frame's 80 ms: idle time for input
    finally:
        sc.pause()
