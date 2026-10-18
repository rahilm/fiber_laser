"""Nest project file round trip and the ext_nest UI registration (offscreen)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mlaser.cam import ordering as O
from mlaser.model import Arc, Contour, Line, Part
from mlaser.nest import NestJob, NestParams, NestPart, NestSheet, nest
from mlaser.nest.project import STD_SHEET_QTY, dumps_project, loads_project
from mlaser.nest.shapes import parts_geometry


def rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def circle(cx, cy, r):
    s = (cx + r, cy)
    return Contour([Arc(s, s, (cx, cy), True)], True)


def make_job():
    job = NestJob(params=NestParams(spacing=2, margin=5))
    job.add_part(NestPart([Part([rect(0, 0, 120, 80), circle(60, 40, 20)], name="plate")], name="plate", quantity=4))
    job.add_part(NestPart([Part([circle(0, 0, 15)], name="disc")], name="圆片", quantity=6))
    job.add_sheet(NestSheet(600, 400))
    job.add_sheet(NestSheet(outline=[Part([rect(0, 0, 300, 200), rect(100, 50, 50, 50)])], is_remnant=True,
                            name="残板1", quantity=2))
    return job


def test_project_roundtrip():
    job = make_job()
    nest(job)
    data = dumps_project(job)
    text = data.decode("gbk")
    assert text.startswith("scFlie\r\n9\r\n \r\n<Begin Parts>\r\n2\r\nPart_1\r\n")
    for tag in ("<End Parts>", "<Begin Sheets>", "Sheet_2", "<Begin NestResults>", "Result_1",
                "Result Graphs", "<End NestResults>"):
        assert tag in text
    assert text.endswith("eof\r\n")
    job2 = loads_project(data)
    assert [p.name for p in job2.parts] == ["plate", "圆片"]
    assert [p.quantity for p in job2.parts] == [4, 6]
    assert job2.sheets[0].width == 600 and job2.sheets[0].quantity == STD_SHEET_QTY
    rem = job2.sheets[1]
    assert rem.is_remnant and rem.name == "残板1" and rem.quantity == 2
    assert parts_geometry(rem.outline).area == pytest.approx(300 * 200 - 50 * 50, rel=1e-6)
    r1, r2 = job.result, job2.result
    assert len(r1.layouts) == len(r2.layouts)
    for a, b in zip(r1.layouts, r2.layouts):
        assert a.sheet_id == b.sheet_id
        for pa, pb in zip(a.placements, b.placements):
            assert (pa.part_id, pa.angle) == (pb.part_id, pb.angle)
            assert pa.x == pytest.approx(pb.x, abs=1e-3) and pa.y == pytest.approx(pb.y, abs=1e-3)
    assert r2.placed == r1.placed
    # saving the loaded project reproduces the bytes
    assert dumps_project(job2) == data


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_register_nest_and_sort(app, tmp_path):
    from mlaser.ui import docops
    from mlaser.ui.ext_nest import register
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import MainWindow

    from mlaser.ui.ext_nest import NestParamsDialog, SheetDialog
    win = MainWindow(DemoController())
    nx, sx = register(win)
    d = NestParamsDialog(win, nx.job.params, win)
    d.spacing.setValue(4.0)
    assert d.apply().spacing == 4.0
    assert SheetDialog(win).sheet().width == 3000.0
    win.set_document(docops.demo_document())
    assert nx.add_part_from_selection() == 4
    nx.job.parts[1].quantity = 3
    nx.job.add_sheet(NestSheet(1000, 500))
    res = nx.run(interactive=False)
    assert sum(res.placed.values()) == 6
    assert nx.panel.results.rowCount() == len(res.layouts)
    assert nx.to_main()
    assert len(win.document.parts) == len(res.layouts[0].placements)
    win.undo()
    assert len(win.document.parts) == 4
    path = str(tmp_path / "a.nestproj")
    assert nx.save_project(path=path)
    assert nx.open_project(path)
    assert len(nx.job.parts) == 4 and nx.job.result is not None
    out = nx.export_sheets(str(tmp_path))
    assert out and all(os.path.exists(p) for p in out)
    # sort commands
    win.run("sort.out_in")
    assert win.cam_params.sort_mode == O.MODE_RING_OUT
    win.run("sort.in_out")
    assert win.cam_params.sort_mode == O.MODE_RING_IN
    win.run("sort.shortest")
    assert win.cam_params.sort_mode == O.MODE_SHORTEST
    names = [p.name for p in win.document.parts]
    sx.start_manual()
    sx.pick(3)
    assert [p.name for p in win.document.parts][0] == names[3]
    assert win.cam_params.sort_mode == O.MODE_KEEP
    sx.finish_manual()
    win.canvas.select_parts([2])
    for p in win.document.parts:
        p.selected = False
    win.document.parts[2].selected = True
    lay = win.document.parts[2].craft.layer
    win.run("sort.layer_first")
    assert win.document.parts[0].craft.layer == lay
