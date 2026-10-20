"""Cutting strategy (review 2 P1-5 / P1-6; docs/user/cutting-strategy.md): lead-in collision
check and fix, automatic lead placement, tip analysis on the slat bed with the tip-avoiding
order, skeleton cut-up, remnant separation cut and the nest comparison figures.

The assertions are geometric and run on the toolpath as it is cut (``build_toolpath``)."""

import copy
import math

import pytest
from shapely.geometry import LineString, Point as SPoint, Polygon, box
from shapely.ops import unary_union

from mlaser.cam import collision, leadplace, tipping
from mlaser.cam.toolpath import CamParams, build_toolpath
from mlaser.model import Arc, Contour, Craft, Document, LeadParams, Line, Part
from mlaser.nest import NestJob, NestParams, NestPart, NestSheet, nest
from mlaser.nest.shapes import parts_geometry
from mlaser.toolpath.items import CutArc, CutLine, Marker, Rapid


def rect(x0, y0, w, h, lead=2.0, kind=1, layer=1):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    c = Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)
    return Part([c], Craft(layer=layer, lead_in=LeadParams(kind=kind, length=lead)))


def circle(cx, cy, r):
    a, b = (cx + r, cy), (cx - r, cy)
    return Contour([Arc(a, b, (cx, cy), True), Arc(b, a, (cx, cy), True)], True)


def lead_moves(items):
    """{(part, contour): [points of the lead-in, travel order]} of a toolpath."""
    out, cur = {}, None
    for m in items:
        if isinstance(m, Marker) and m.kind == "contour":
            cur = (m.part, m.contour)
        elif isinstance(m, (CutLine, CutArc)) and getattr(m, "lead", None) == "in":
            pts = out.setdefault(cur, [])
            if isinstance(m, CutArc):
                a = Arc(m.start, m.end, m.center, m.ccw)
                pts += [a.point_at(k / 16) for k in range(17)]
            else:
                pts += [m.start, m.end]
    return out


def outline(doc, key):
    pi, ci = key
    pts = list(doc.parts[pi].contours[ci].points(0.05))
    return LineString(pts + [pts[0]])


def assert_leads_clear(doc, kerf=0.2, clearance=0.5, sheet=None):
    """No lead-in reaches another contour (kerf / 2 + clearance), crosses its own contour away
    from its start, or pierces closer than 2 mm to the sheet edge."""
    for key, pts in lead_moves(build_toolpath(doc, CamParams())).items():
        lead = LineString(pts)
        for pi, part in enumerate(doc.parts):
            for ci, _c in enumerate(part.contours):
                if (pi, ci) == key:
                    continue
                assert lead.distance(outline(doc, (pi, ci))) >= kerf / 2 + clearance - 1e-6, (key, (pi, ci))
        rest = collision._trim_end(pts, 0.05)
        assert len(rest) < 2 or not LineString(rest).intersects(outline(doc, key)), key
        if sheet is not None:
            assert sheet.buffer(-2.0).contains(SPoint(pts[0])), key


# ============================================================================ lead collisions
def two_close_parts():
    """Two 50 x 30 plates 3 mm apart; the first part starts in the middle of its top edge, so its
    4 mm lead-in pierces inside the other part's kerf."""
    doc = Document(parts=[rect(0, 0, 50, 30, lead=4.0), rect(0, 33, 50, 30, lead=4.0)])
    doc.parts[0].craft.start_param = (50 + 30 + 25) / 160.0
    return doc


def test_lead_crossing_another_part_is_found_and_fixed():
    doc = two_close_parts()
    found = collision.analyse(doc)
    assert {(i.key, i.kind) for i in found} >= {((0, 0), "contour")}
    rep = collision.fix(doc)
    assert rep.found and not rep.remaining
    assert rep.count("moved") >= 1 and rep.count("flagged") == 0
    assert collision.analyse(doc) == []
    assert_leads_clear(doc)  # geometric check on the toolpath as cut


def test_shortening_follows_the_original_07_rule():
    """No start may move: the lead is shortened to the free distance (x 0.7 for an arc lead
    that hits another contour), then x 0.7 until clear."""
    doc = two_close_parts()
    doc.parts[0].craft.lead_in = LeadParams(kind=2, angle_deg=90.0, length=4.0)
    prm = collision.LeadCheckParams(move_start=False)
    first = [i for i in collision.analyse(doc, params=prm) if i.key == (0, 0)]
    d, kind = collision._first_conflict(
        (0, 0),
        doc.parts[0].craft,
        collision._lead_points(collision.prepared_nodes(doc)[0][2]),
        collision._Index(collision.prepared_nodes(doc)),
        None,
        prm,
    )
    assert first and kind == "contour"
    rep = collision.fix(doc, params=prm)
    fx = {f.key: f for f in rep.fixes}
    assert fx[(0, 0)].action == "shortened" and fx[(0, 0)].after <= 0.7 * d + 1e-9  # arc: x 0.7
    assert fx[(1, 0)].action == "shortened"
    assert collision.analyse(doc, params=prm) == []
    assert_leads_clear(doc)


def test_lead_in_a_small_hole_is_shortened_not_crossing_itself():
    """A 5 mm lead-in in a 4 mm hole crosses the hole: shortened (own contour, factor 1)."""
    p = rect(0, 0, 40, 40, lead=2.0)
    p.contours.append(circle(20, 20, 2.0))
    p.contours[1].craft = Craft(layer=1, lead_in=LeadParams(kind=1, length=5.0))
    doc = Document(parts=[p])
    assert any(i.kind == "own" for i in collision.analyse(doc))
    rep = collision.fix(doc)
    assert not rep.remaining
    assert doc.parts[0].contours[1].craft.lead_in.length < 4.0
    assert_leads_clear(doc)


def test_lead_with_no_room_is_flagged_red():
    """A hole too small for any lead (0.6 mm hole, minimum lead 0.5 mm): the error mark."""
    p = rect(0, 0, 40, 40, lead=2.0)
    p.contours.append(circle(20, 20, 0.3))
    p.contours[1].craft = Craft(layer=1, lead_in=LeadParams(kind=1, length=3.0))
    doc = Document(parts=[p])
    rep = collision.fix(doc)
    assert rep.count("flagged") == 1 and doc.parts[0].contours[1].craft.lead_error


def test_lead_at_the_sheet_edge():
    sheet = box(0, 0, 200, 100)
    doc = Document(parts=[rect(1, 10, 50, 30, lead=3.0)])  # starts 1 mm from the left edge
    found = collision.analyse(doc, sheet=sheet)
    assert [i.kind for i in found] == ["edge"]
    doc.extra["sheet_bounds"] = [0, 0, 200, 100]  # the nest sheet documents carry it
    assert [i.kind for i in collision.analyse(doc)] == ["edge"]
    assert not collision.fix(doc).remaining
    assert_leads_clear(doc, sheet=sheet)


def test_mark_and_preflight_warning():
    from mlaser.machine.preflight import cutting_issues

    doc = two_close_parts()
    issues = cutting_issues(doc)
    assert issues and {i.code for i in issues} == {"lead_collision"}
    assert all(i.severity == "warning" for i in issues)
    assert collision.mark(doc, collision.analyse(doc)) >= 1
    assert doc.parts[0].craft.lead_error
    collision.fix(doc)
    assert cutting_issues(doc) == []


# ============================================================================ lead placement
def test_lead_placement_rules():
    c = rect(0, 0, 100, 20).contours[0]
    cands = leadplace.candidates(c, leadplace.LeadRules())  # auto: corners on a sharp part
    assert cands[0].kind == "corner"
    st = leadplace.candidates(c, leadplace.LeadRules(prefer="straight"))[0]
    assert st.kind == "straight" and st.point in ((50.0, 0.0), (50.0, 20.0))
    side = leadplace.candidates(c, leadplace.LeadRules(prefer="straight", prefer_side="top"))[0]
    assert side.point == (50.0, 20.0)
    c.extra["finished_edges"] = [0, 2]  # both long sides are visible
    rest = leadplace.candidates(c, leadplace.LeadRules(prefer="straight"))
    assert all(
        abs(cd.point[1] - 0.0) > 1e-9 and abs(cd.point[1] - 20.0) > 1e-9
        for cd in rest
        if cd.kind != "fallback"
    )
    assert rest[0].kind == "straight" and rest[0].point[0] in (0.0, 100.0)
    # small features: a notch of 2 mm segments carries no start
    small = leadplace.candidates(rect(0, 0, 4, 4).contours[0], leadplace.LeadRules(min_feature=5.0))
    assert all(cd.kind == "fallback" for cd in small)
    r = leadplace.LeadRules.from_dict(
        {"prefer": "corner", "min_feature_mm": 3, "corner_max_angle_deg": 120, "enabled": True, "unknown": 1}
    )
    assert (r.prefer, r.min_feature, r.corner_max_angle) == ("corner", 3, 120)


def test_place_leads_sets_the_start_and_the_toolpath_follows():
    doc = Document(parts=[rect(10, 10, 100, 20)])
    n = leadplace.place_leads(doc, leadplace.LeadRules(prefer="straight"))
    assert n == 1
    f = doc.parts[0].craft.start_param
    assert f == pytest.approx(0.5 * 100 / 240)
    leads = lead_moves(build_toolpath(doc, CamParams()))
    assert math.dist(leads[(0, 0)][-1], (60.0, 10.0)) < 1e-6  # the lead ends mid bottom edge


def test_technology_lead_rules_apply():
    from mlaser.services.technology import TechKey, Technology, apply_craft

    tech = Technology(
        TechKey("S235", 3.0, "O2", "1.5"),
        craft={"lead_in": {"kind": 1, "length": 3.0}},
        rules={"lead_place": {"enabled": True, "prefer": "straight"}},
    )
    doc = Document(parts=[rect(0, 0, 100, 20)])
    apply_craft(doc, tech, leads=True, kerf=False, joints=False)
    assert doc.parts[0].craft.start_param == pytest.approx(0.5 * 100 / 240)
    s, w = tipping.strategy_from_technology(
        Technology(TechKey("S235", 1.0, "N2", "1.5"), rules={"tip": {"strategy": "last"}})
    )
    assert (s, w) == ("last", 0.5)


# ============================================================================ tipping
BED = tipping.BedModel(pitch=100.0, direction="y", offset=None, width=3.0)


def test_tip_states():
    doc = Document(parts=[rect(0, 0, 300, 300), rect(310, 0, 60, 40), rect(400, 0, 5, 5)])
    st = {p.key: p.state for p in tipping.analyse(doc, BED)}
    assert st == {(0, 0): "stable", (1, 0): "tips", (2, 0): "small"}
    # known slat position: the 60 mm plate between two slats falls through, over one it tips
    between = tipping.BedModel(pitch=100.0, offset=300.0)  # slats at x = 300, 400
    over = tipping.BedModel(pitch=100.0, offset=320.0)  # one slat under it
    assert {p.key: p.state for p in tipping.analyse(doc, between)}[(1, 0)] == "drops"
    assert {p.key: p.state for p in tipping.analyse(doc, over)}[(1, 0)] == "tips"


def test_l_shape_over_two_slats_tips():
    pts = [(0, 0), (250, 0), (250, 20), (20, 20), (20, 400), (0, 400)]  # heavy leg along Y
    c = Contour([Line(pts[i], pts[(i + 1) % 6]) for i in range(6)], True)
    doc = Document(parts=[Part([c], Craft(layer=1))])
    bed = tipping.BedModel(pitch=100.0, offset=10.0, direction="x")  # slats along X at y = 10, 110..
    assert tipping.analyse(doc, bed)[0].state == "stable"
    bed_y = tipping.BedModel(pitch=200.0, offset=150.0, direction="y")  # one slat under the foot only
    assert tipping.analyse(doc, bed_y)[0].state == "tips"


def test_tip_risk_part_gets_a_micro_joint_on_its_heavy_side():
    doc = Document(parts=[rect(0, 0, 300, 300), rect(310, 0, 60, 40)])
    rep = tipping.apply(doc, BED, "joint")
    assert [k for k, _pos in rep.joints] == [(1, 0)]
    joints = doc.parts[1].craft.joint_list()
    assert len(joints) == 1
    assert tipping.analyse(doc, BED)[1].state == "held"
    items = build_toolpath(doc, CamParams())
    gaps = [m for m in items if isinstance(m, (CutLine, CutArc)) and not m.laser]
    assert gaps  # the joint is a beam-off span
    from mlaser.geom.contour import point_at

    jp = point_at(doc.parts[1].contours[0], joints[0][0])
    cg = (340.0, 20.0)
    worst = tipping.analyse(Document(parts=[rect(310, 0, 60, 40)]), BED)[0]
    v = (cg[0] - worst.support[0], cg[1] - worst.support[1])
    assert (jp[0] - cg[0]) * v[0] + (jp[1] - cg[1]) * v[1] > 0  # on the centre-of-gravity side


def _order(items):
    return [m.part for m in items if isinstance(m, Marker) and m.kind == "contour"]


def test_tip_risk_part_is_cut_last_and_rapids_go_around_it():
    parts = [rect(0, 0, 60, 40), rect(100, 0, 300, 300), rect(0, 60, 60, 40), rect(450, 0, 300, 300)]
    doc = Document(parts=parts)
    base = _order(build_toolpath(doc, CamParams()))
    d2 = copy.deepcopy(doc)
    rep = tipping.apply(d2, BED, "last")
    risky = set(rep.last)
    assert risky == {0, 2}
    order = _order(build_toolpath(d2, CamParams()))
    assert sorted(order) == sorted(base)
    first_risky = min(order.index(i) for i in risky)
    assert all(order.index(i) < first_risky for i in set(order) - risky)  # every other part first
    # rapids after a tip-risk part is cut keep out of it
    items = build_toolpath(d2, CamParams())
    cut, done = [], set()
    for m in items:
        if isinstance(m, Marker) and m.kind == "contour":
            if done:
                cut = [box(*d2.parts[i].bbox()) for i in done]
            if m.part in risky:
                done.add(m.part)
        elif isinstance(m, Rapid) and cut:
            seg = LineString([m.start, m.end])
            assert all(seg.intersection(g.buffer(-0.5)).length < 1e-6 for g in cut)


def test_tip_strategy_avoid_marks_slugs():
    p = rect(0, 0, 400, 400)
    p.contours.append(rect(100, 100, 70, 50).contours[0])  # a 70 x 50 slug can tip
    doc = Document(parts=[p])
    rep = tipping.apply(doc, BED, "avoid")
    assert rep.avoided == [(0, 1)] and doc.parts[0].contours[1].extra.get("tip_risk")
    assert build_toolpath(doc, CamParams())  # the toolpath builds with the tags


# ============================================================================ nest strategy
def nested(**kw):
    job = NestJob(params=NestParams(spacing=5, margin=5, rotation_step=90, time_limit_s=10, **kw))
    job.add_part(NestPart([rect(0, 0, 200, 120)], quantity=8))
    job.add_sheet(NestSheet(1500, 1000, 1))
    job.result = nest(job)
    return job


def scrap_pieces(doc, sheet, remnant=None, kerf=0.2):
    lines = [c for p in doc.parts if p.extra.get("strategy") for c in p.contours]
    parts = [p for p in doc.parts if not p.extra.get("strategy")]
    used = parts_geometry(parts)
    cut = unary_union([LineString([c.segments[0].start, c.segments[-1].end]).buffer(kerf / 2) for c in lines])
    filled = unary_union([Polygon(g.exterior) for g in getattr(used, "geoms", [used])])
    scrap = sheet.difference(filled.buffer(kerf / 2)).difference(cut)
    if remnant is not None:
        scrap = scrap.difference(remnant.buffer(kerf))
    return [g for g in getattr(scrap, "geoms", [scrap]) if g.area > 1.0], used


@pytest.mark.parametrize("mode", ["grid", "gaps"])
def test_skeleton_cut_up_pieces_are_under_the_max_size(mode):
    from mlaser.services.nesting import sheet_documents

    job = nested(scrap_cut=mode, scrap_pitch=300.0, remnant_cut=True)
    ((k, doc, info),) = sheet_documents(job)
    sheet = box(*info["sheet_bounds"])
    from mlaser.services.remnants import polygon_of

    remnant = polygon_of(info["remnant_rings"])
    pieces, used = scrap_pieces(doc, sheet, remnant)
    assert pieces and info["strategy"]["scrap_lines"] > 0
    for g in pieces:
        x0, y0, x1, y1 = g.bounds
        assert max(x1 - x0, y1 - y0) <= 300.0 + 0.5, g.bounds
    for p in doc.parts:  # the lines never touch a part
        if p.extra.get("strategy"):
            for c in p.contours:
                ln = LineString([c.segments[0].start, c.segments[-1].end])
                assert ln.distance(used) >= 0.1 - 1e-6


def test_remnant_is_separated_and_cut_after_the_parts():
    from mlaser.services.nesting import sheet_documents

    job = nested(remnant_cut=True)
    lay = job.result.layouts[0]
    assert lay.remnant is not None
    ((k, doc, info),) = sheet_documents(job)
    from mlaser.services.remnants import polygon_of

    rem = polygon_of(info["remnant_rings"])
    assert rem.area == pytest.approx((1500 - lay.remnant[0] - 0.1) * 1000, rel=1e-3)
    cut = [p for p in doc.parts if p.extra.get("strategy") == "remnant_cut"]
    assert len(cut) == 1
    ln = unary_union([LineString([c.segments[0].start, c.segments[-1].end]) for c in cut[0].contours])
    assert ln.length == pytest.approx(1000.0)  # edge to edge across the sheet
    parts = parts_geometry([p for p in doc.parts if not p.extra.get("strategy")])
    assert rem.intersection(parts).area < 1e-6  # the remnant holds no part
    pieces = box(*info["sheet_bounds"]).difference(ln.buffer(0.1))
    assert len(getattr(pieces, "geoms", [pieces])) == 2  # the line really separates it
    order = _order(build_toolpath(doc, CamParams()))
    ci = doc.parts.index(cut[0])
    assert order.index(ci) > max(
        order.index(i) for i, p in enumerate(doc.parts) if not p.extra.get("strategy")
    )
    for c in cut[0].contours:  # pierced inside the sheet, not off it
        assert box(*info["sheet_bounds"]).buffer(-1).contains(SPoint(c.segments[0].start))


def test_strategy_order_parts_then_separation_then_cut_up():
    from mlaser.services.nesting import sheet_documents

    job = nested(scrap_cut="grid", scrap_pitch=300.0, remnant_cut=True)
    ((_k, doc, _info),) = sheet_documents(job)
    order = _order(build_toolpath(doc, CamParams()))
    phase = [int(doc.parts[i].extra.get("cut_phase", 0)) for i in order]
    assert phase == sorted(phase) and set(phase) == {0, 2, 3}


def test_strategy_off_by_default():
    from mlaser.services.nesting import has_strategy, sheet_documents

    job = nested()
    ((_k, doc, info),) = sheet_documents(job)
    assert not has_strategy(doc) and "remnant_rings" not in info
    assert len(doc.parts) == 8
    p = NestParams()
    assert (p.scrap_cut, p.remnant_cut, p.lead_fix, p.tip_check, p.grain) == ("off", False, False, False, "")


def test_queue_writes_mlz_with_the_strategy(tmp_path):
    from mlaser.project.format import load_project
    from mlaser.services.nesting import send_to_queue
    from mlaser.services.queue import JobQueue

    job = nested(scrap_cut="grid", scrap_pitch=300.0, remnant_cut=True)
    q = JobQueue(str(tmp_path / "queue.json"))
    entries = send_to_queue(job, q, str(tmp_path / "sheets"), "t")
    assert len(entries) == 1 and entries[0].path.endswith(".mlz")
    assert entries[0].extra["remnant_rings"]
    doc = load_project(entries[0].path).document
    assert {p.extra.get("cut_phase", 0) for p in doc.parts} == {0, 2, 3}
    plain = send_to_queue(nested(), q, str(tmp_path / "sheets2"), "u")
    assert plain[0].path.endswith(".chf")


def test_queue_sheets_get_common_line_cutting():
    from mlaser.services.nesting import run_figures, sheet_documents

    job = NestJob(
        params=NestParams(margin=0, common_line=True, rotation_step=0, share_edge_min_len=20, time_limit_s=10)
    )
    job.add_part(NestPart([rect(0, 0, 100, 50)], quantity=4))
    job.add_sheet(NestSheet(500, 300, 1))
    job.result = nest(job)
    ((_k, doc, _i),) = sheet_documents(job)
    shared = run_figures(job, job.result)
    job2 = copy.deepcopy(job)
    job2.params.common_line = False
    alone = run_figures(job2, job2.result)
    assert shared["cut_length_mm"] < alone["cut_length_mm"] - 100  # the shared edges are cut once
    assert alone["pierces"] == 4 and alone["cut_length_mm"] == pytest.approx(4 * 300.0)


def test_nest_lead_fix_and_tip_check_on_the_queued_sheet():
    from mlaser.services.nesting import sheet_documents

    job = NestJob(
        params=NestParams(
            spacing=2, margin=5, rotation_step=0, time_limit_s=10, lead_fix=True, tip_check=True
        )
    )
    np_ = job.add_part(NestPart([rect(0, 0, 60, 40, lead=4.0)], quantity=6))
    np_.parts[0].craft.start_param = (60 + 40 + 30) / 200.0  # lead in the 2 mm gap above
    job.add_sheet(NestSheet(400, 200, 1))
    job.result = nest(job)
    ((_k, doc, info),) = sheet_documents(job, bed=BED)
    assert info["strategy"]["leads_fixed"] > 0 and info["strategy"]["tip_risk"] == 6
    assert collision.analyse(doc) == []
    assert all(p.craft.micro_joints for p in doc.parts)


def test_grain_and_part_rotation():
    from mlaser.nest.model import GRAIN_Y

    job = NestJob(params=NestParams(spacing=2, margin=0, rotation_step=90, grain=GRAIN_Y, time_limit_s=10))
    a = job.add_part(NestPart([rect(0, 0, 80, 20)], quantity=3))
    a.extra["grain"] = True
    b = job.add_part(NestPart([rect(0, 0, 30, 30)], quantity=2, angles=[0.0]))
    assert job.params.part_angles(a) == [90.0, 270.0] and job.params.part_angles(b) == [0.0]
    res = nest(job)
    for lay in res.layouts:
        for pl in lay.placements:
            allowed = {90.0, 270.0} if pl.part_id == a.id else {0.0}
            assert round(pl.angle % 360.0, 6) in allowed


def test_nest_run_compares_cut_length_and_pierces():
    from mlaser.services.nesting import NestService

    svc = NestService(threaded=False)
    job = nested()
    job.result = None
    svc.start(job)
    run = svc.runs[-1]
    assert run.pierces == 8 and run.cut_length_mm == pytest.approx(8 * 640.0)
    s = run.summary()
    assert s["pierces"] == 8 and "cut_length_mm" in s


def test_untagged_documents_keep_the_default_toolpath():
    """The strategy hook of build_toolpath is a no-op without its tags (the goldens pin the rest):
    phase-0 / false tags give the same items as no tags."""
    parts = [rect(0, 0, 60, 40), rect(100, 0, 300, 300), rect(0, 60, 60, 40)]
    parts[1].contours.append(rect(150, 100, 70, 50).contours[0])
    doc = Document(parts=parts)
    ref = build_toolpath(doc, CamParams())
    tagged = copy.deepcopy(doc)
    for p in tagged.parts:
        p.extra["cut_phase"] = 0
        p.extra["tip_risk"] = False
    assert build_toolpath(tagged, CamParams()) == ref
    assert build_toolpath(copy.deepcopy(doc), CamParams(avoid_cut=True)) == build_toolpath(
        doc, CamParams(avoid_cut=True)
    )
