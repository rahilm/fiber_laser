"""Offscreen smoke test of the ext_cam plugin (advanced CAM ribbon commands), loaded through the
application's real wiring (MainWindow + EXTENSIONS)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mlaser.model import Arc, Contour, Document, Line, Part  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


@pytest.fixture()
def win(app):
    from wiring import real_window
    w = real_window()
    assert w.extension("ext_cam").exec_dialogs is False
    return w


def test_register_rewires_ribbon(win):
    acts = win.ribbon.actions
    for cmd in ("tool.array", "opt.dedup", "opt.join", "opt.smooth", "tool.share_edge",
                "craft.chamfer_auto", "craft.unload", "camx.fill_circle", "camx.alpha"):
        assert cmd in acts and cmd in win.handlers
    assert "CAM+" in [win.ribbon.tabText(i) for i in range(win.ribbon.count())]


def test_array_and_fill_commands(win):
    win.set_document(Document(parts=[Part(contours=[rect(0, 0, 10, 10)])]))
    ext = win.extension("ext_cam")
    ext.array.rows, ext.array.cols = 2, 2
    win.run("tool.array")
    assert len(win.document.parts) == 4
    win.undo()
    assert len(win.document.parts) == 1
    win.set_document(Document(parts=[Part(contours=[rect(0, 0, 100, 50)])]))
    ext.fill.radius, ext.fill.stock, ext.fill.space = 3, 3, 3
    win.run("camx.fill_circle")
    circles = [p for p in win.document.parts if isinstance(p.contours[0].segments[0], Arc)]
    assert len(circles) > 10


def test_cleanup_and_corners_commands(win):
    segs = [Line((0, 0), (10, 0)), Line((10, 0), (10, 10)), Line((10, 10), (0, 10)), Line((0, 10), (0, 0)),
            Line((0, 0), (10, 0))]
    win.set_document(Document(parts=[Part(contours=[Contour([s], False)]) for s in segs]))
    win.run("opt.join")
    closed = [p for p in win.document.parts if p.contours[0].closed]
    assert len(closed) == 1
    win.run("opt.dedup")
    assert sum(len(p.contours) for p in win.document.parts) == 1
    n0 = len(win.document.parts[0].contours[0].segments)
    win.extension("ext_cam").fillet_p.radius = 1.0
    win.run("craft.chamfer_auto")
    c = win.document.parts[0].contours[0]
    assert len(c.segments) == n0 + 4 and sum(isinstance(s, Arc) for s in c.segments) == 4


def test_share_edge_command(win):
    win.set_document(Document(parts=[Part(contours=[rect(0, 0, 10, 10)]), Part(contours=[rect(10, 0, 10, 10)])]))
    win.run("tool.share_edge")
    total = sum(c.length() for p in win.document.parts for c in p.contours)
    assert abs(total - 70.0) < 1e-6


def test_flycut_command(win):
    def circ(cx, cy, r):
        p = (cx + r, cy)
        return Contour([Arc(p, p, (cx, cy), True)], True)
    win.set_document(Document(parts=[Part(contours=[circ(10 * i, 0, 2)]) for i in range(5)]))
    win.run("tool.scan_circle")
    scans = [p for p in win.document.parts if "scan_paths" in p.extra]
    assert len(scans) == 1
    assert win.toolpath                       # toolpath still builds
