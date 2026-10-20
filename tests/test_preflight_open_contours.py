"""Pre-flight: open contours are one warning with the count, not one line per nested copy
(review 2 §2.1 item 3: "44 open contours"), and they never block."""
from __future__ import annotations

from mlaser.machine.preflight import check
from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part


def opened(x):
    return Part([Contour([Line((x, 0), (x + 10, 0)), Line((x + 10, 0), (x + 10, 10))], False)], Craft(layer=1))


def closed(x):
    pts = [(x, 0), (x + 10, 0), (x + 10, 10)]
    return Part([Contour([Line(pts[i], pts[(i + 1) % 3]) for i in range(3)], True)], Craft(layer=1))


def doc(*parts):
    return Document(parts=list(parts), layers={1: LayerParams(1)})


def test_one_line_for_many_open_contours():
    d = doc(closed(0), *[opened(20 * k) for k in range(1, 45)])
    d.parts[3].contours.append(Contour([Line((0, 50), (5, 50))], False))
    rep = check("start", document=d, homed=True, armed=True, emits=True)
    ws = [i for i in rep.issues if i.code == "open_contour"]
    assert len(ws) == 1 and rep.ok
    assert ws[0].message.startswith("45 open contours in 44 parts (first: part 2 contour 1)")
    assert "Repair" in ws[0].message and ws[0].where == (1, 0)


def test_single_open_contour_keeps_its_place():
    rep = check("start", document=doc(closed(0), opened(30)), homed=True, armed=True, emits=True)
    (w,) = [i for i in rep.issues if i.code == "open_contour"]
    assert w.message.startswith("part 2 contour 1 is open") and w.severity == "warning"


def test_closed_drawing_has_none():
    rep = check("start", document=doc(closed(0)), homed=True, armed=True, emits=True)
    assert "open_contour" not in rep.codes()
