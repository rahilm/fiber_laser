"""Synthetic Onshape parts as ``bodydetails`` / ``massproperties`` JSON (OpenAPI schemas
BTExportModelBodiesResponse-734 and BTMassPropertiesBulkInfo), built from 2-D outlines.

All input sizes are millimetres; the JSON is in metres, as Onshape sends it.  A plate lies
between h=0 and h=t along its normal N with in-plane axes a, b (a x b = N).
"""
from __future__ import annotations

import itertools
import math

M = 0.001
AXES = {
    "z": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),     # a, b, N
    "x": ((0, 1, 0), (0, 0, 1), (1, 0, 0)),     # a side plate: thickness along X
    "y": ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
}


def V(p):
    return {"btType": "BTVector3d-389", "x": p[0], "y": p[1], "z": p[2]}


class Builder:
    def __init__(self, axis="z", t=3.0, material="Aluminum - 6061"):
        self.a, self.b, self.N = AXES[axis]
        self.t = t
        self.ids = itertools.count(1)
        self.faces, self.edges, self.vertices = [], [], []
        self.material = material
        self.volume_mm3 = 0.0

    def p3(self, x, y, h):
        a, b, n = self.a, self.b, self.N
        return tuple((a[i] * x + b[i] * y + n[i] * h) * M for i in range(3))

    def vid(self, p):
        i = f"V{next(self.ids)}"
        self.vertices.append({"btType": "BTExportModelVertex-858", "id": i, "point": V(p)})
        return i

    # 2-D primitives: ("line", (x0,y0), (x1,y1)) ; ("arc", c, r, a0deg, a1deg, ccw) ;
    # ("circle", c, r) ; ("ellipse", c, ra, rb) ; ("spline", spline-dict-in-mm, (x0,y0), (x1,y1), mid)
    # ("other", pts) an edge only /tessellatededges can draw
    def edge(self, prim, h, sign=1):
        eid = f"E{next(self.ids)}"
        k = prim[0]
        g = {"btType": "BTExportModelEdgeGeometry-1125"}
        e = {"btType": "BTExportModelEdge-1782", "id": eid, "vertices": []}
        if k == "line":
            s, en = self.p3(*prim[1], h), self.p3(*prim[2], h)
            d = [en[i] - s[i] for i in range(3)]
            L = math.sqrt(sum(x * x for x in d))
            e["curve"] = {"btType": "BTLineDescription-1559", "type": "LINE", "origin": V(s),
                          "direction": V([x / L for x in d])}
            g.update(startPoint=V(s), endPoint=V(en), midPoint=V([(s[i] + en[i]) / 2 for i in range(3)]), length=L)
            e["vertices"] = [self.vid(s), self.vid(en)]
        elif k in ("arc", "circle"):
            c, r = prim[1], prim[2]
            e["curve"] = {"btType": "BTCircleDescription-1145", "type": "CIRCLE", "origin": V(self.p3(*c, h)),
                          "normal": V(self.N), "radius": r * M}
            if k == "arc":
                a0, a1, ccw = prim[3], prim[4], prim[5]
                sweep = (a1 - a0) % 360 if ccw else -((a0 - a1) % 360)
                am = a0 + sweep / 2
                pt = lambda a: self.p3(c[0] + r * math.cos(math.radians(a)), c[1] + r * math.sin(math.radians(a)), h)  # noqa: E731
                g.update(btType="BTExportModelArcEdgeGeometry-1257", startPoint=V(pt(a0)), endPoint=V(pt(a1)),
                         midPoint=V(pt(am)), arcSweep=math.radians(abs(sweep)), length=abs(math.radians(sweep)) * r * M)
                e["vertices"] = [self.vid(pt(a0)), self.vid(pt(a1))]
            else:
                g.update(length=2 * math.pi * r * M)
        elif k == "ellipse":
            c, ra, rb = prim[1], prim[2], prim[3]
            e["curve"] = {"btType": "BTEllipseDescription-866", "type": "ELLIPSE", "origin": V(self.p3(*c, h)),
                          "normal": V(self.N), "majorAxis": V(self.a), "majorRadius": ra * M, "minorRadius": rb * M}
        elif k == "spline":
            sp, s2, e2, m2 = prim[1], prim[2], prim[3], prim[4]
            cps = []
            stride = 4 if sp.get("isRational") else 3
            pts = sp["controlPoints"]
            for i in range(0, len(pts), stride):
                p = self.p3(pts[i], pts[i + 1], h)
                cps.extend(p)
                if stride == 4:
                    cps.append(pts[i + 3])
            e["curve"] = {"btType": "BTSplineDescription-2118", "type": "BCURVE", "degree": sp["degree"],
                          "knots": sp["knots"], "controlPoints": cps, "isRational": bool(sp.get("isRational")),
                          "isPeriodic": False}
            s, en = self.p3(*s2, h), self.p3(*e2, h)
            g.update(startPoint=V(s), endPoint=V(en), midPoint=V(self.p3(*m2, h)))
            e["vertices"] = [self.vid(s), self.vid(en)]
        elif k == "other":
            pts = prim[1]
            e["curve"] = {"btType": "BTCurveDescription-1583", "type": "OTHER"}
            s, en = self.p3(*pts[0], h), self.p3(*pts[-1], h)
            g.update(startPoint=V(s), endPoint=V(en))
            e["vertices"] = [self.vid(s), self.vid(en)]
            e["_tess"] = [self.p3(x, y, h) for x, y in pts]
        e["geometry"] = g
        self.edges.append(e)
        return eid

    def face(self, loops, h, outward, area_mm2, ftype="PLANE", reverse_flag=False):
        """loops: [(is_outer, [prim, ...])]; outward: +1 along N, -1 against."""
        fid = f"F{next(self.ids)}"
        n = self.N
        surf = {"btType": "BTPlaneDescription-692", "type": ftype, "origin": V(self.p3(0, 0, h)), "normal": V(n),
                "isOrientedWithFace": outward > 0,
                "directionOrientedWithFace": V([x * outward for x in n])}
        if reverse_flag:            # only normal + face orientation: the client must flip
            surf = {"btType": "BTPlaneDescription-692", "type": ftype, "origin": V(self.p3(0, 0, h)), "normal": V(n)}
        lps = []
        for outer, prims in loops:
            co = [{"btType": "BTExportModelCoedge-1342", "edgeId": self.edge(p, h), "orientation": True} for p in prims]
            lps.append({"btType": "BTExportModelLoop-1182", "isOuter": outer, "isInner": not outer, "coedges": co})
        f = {"btType": "BTExportModelFace-1363", "id": fid, "area": area_mm2 * M * M, "surface": surf,
             "loops": lps, "orientation": outward > 0 if reverse_flag else True}
        self.faces.append(f)
        return fid

    def side_face(self, normal, area_mm2, h=0.0):
        """A planar side wall (no loops needed for the tests)."""
        fid = f"F{next(self.ids)}"
        self.faces.append({"btType": "BTExportModelFace-1363", "id": fid, "area": area_mm2 * M * M,
                           "surface": {"btType": "BTPlaneDescription-692", "type": "PLANE",
                                       "origin": V(self.p3(0, 0, h)), "normal": V(normal),
                                       "isOrientedWithFace": True, "directionOrientedWithFace": V(normal)},
                           "loops": []})
        return fid

    def cyl_face(self, area_mm2):
        fid = f"F{next(self.ids)}"
        self.faces.append({"btType": "BTExportModelFace-1363", "id": fid, "area": area_mm2 * M * M,
                           "surface": {"btType": "BTCylinderDescription-686", "type": "CYLINDER",
                                       "origin": V((0, 0, 0)), "axis": V(self.N), "radius": 0.005}, "loops": []})
        return fid

    def response(self, pid):
        return {"btType": "BTExportModelBodiesResponse-734", "bodies": [{
            "btType": "BTExportModelBody-1272", "id": pid, "type": "SOLID",
            "properties": {"btType": "BTExportBodyProperties-3559", "name": pid,
                           "material": {"btType": "BTPartMaterial-1445", "id": self.material,
                                        "name": self.material, "libraryName": "Onshape Material Library"}},
            "faces": self.faces, "edges": [{k: v for k, v in e.items() if k != "_tess"} for e in self.edges],
            "vertices": self.vertices}]}

    def tessellation(self, pid, edge_ids=None):
        es = [{"btType": "BTExportTessellatedEdgesEdge-1364", "id": e["id"], "vertices": [V(p) for p in e["_tess"]]}
              for e in self.edges if "_tess" in e and (not edge_ids or e["id"] in edge_ids)]
        return {"btType": "BTExportTessellatedEdgesResponse-327",
                "bodies": [{"btType": "BTExportTessellatedEdgesBody-890", "id": pid, "edges": es}]}

    def mass(self, pid, volume_mm3=None):
        v = (self.volume_mm3 if volume_mm3 is None else volume_mm3) * M ** 3
        return {"microversionId": "mv", "bodies": {pid: {"volume": [v, v, v], "mass": [v * 2700] * 3,
                                                        "periphery": [0, 0, 0], "hasMass": True}}}


def rect(x0, y0, x1, y1):
    return [("line", (x0, y0), (x1, y0)), ("line", (x1, y0), (x1, y1)),
            ("line", (x1, y1), (x0, y1)), ("line", (x0, y1), (x0, y0))]


def slab(b: Builder, outer, holes, outer_area, hole_area, perimeter, both_flags=False):
    """Top (+N, h=t) and bottom (-N, h=0) faces with the same loops, side walls, volume."""
    t = b.t
    net = outer_area - hole_area
    loops = [(True, outer)] + [(False, h) for h in holes]
    top = b.face(loops, t, +1, net)
    bot = b.face(loops, 0.0, -1, net, reverse_flag=both_flags)
    b.side_face(b.a, perimeter * t / 4, 0)
    b.side_face(tuple(-x for x in b.a), perimeter * t / 4, 0)
    b.volume_mm3 = net * t
    return top, bot


# ------------------------------------------------------------------ the test parts
def plate_with_holes(t=3.0, w=100.0, h=60.0, r=4.0, material="Aluminum - 6061"):
    b = Builder("z", t, material)
    holes = [[("circle", (20, 30), r)], [("circle", (80, 30), r)], [("circle", (50, 15), r / 2)]]
    ha = 2 * math.pi * r * r + math.pi * (r / 2) ** 2
    top, bot = slab(b, rect(0, 0, w, h), holes, w * h, ha, 2 * (w + h))
    for _ in holes:
        b.cyl_face(2 * math.pi * r * t)
    return b, top, bot


def plate_with_slot(t=3.0, mirror_flags=False):
    """120 x 50 plate, a 40 x 10 slot (two half circles r=5), offset origin (10, 5)."""
    b = Builder("z", t, "Aluminum 5052 1/8\"")
    ox, oy = 10, 5
    outer = rect(ox, oy, ox + 120, oy + 50)
    cx0, cx1, cy = ox + 40, ox + 80, oy + 25
    slot = [("line", (cx0, cy - 5), (cx1, cy - 5)), ("arc", (cx1, cy), 5, -90, 90, True),
            ("line", (cx1, cy + 5), (cx0, cy + 5)), ("arc", (cx0, cy), 5, 90, 270, True)]
    sa = 40 * 10 + math.pi * 25
    top, bot = slab(b, outer, [slot], 120 * 50, sa, 340, both_flags=mirror_flags)
    return b, top, bot


def pocketed_plate(t=6.0, depth=3.0, pocket_side=-1):
    """80 x 80 plate; a 40 x 40 pocket on the -Z side (the side the router works from)."""
    b = Builder("z", t, "6061-T6 1/4\"")
    outer = rect(0, 0, 80, 80)
    pocket = rect(20, 20, 60, 60)
    hole = [("circle", (10, 10), 3)]
    ha = math.pi * 9
    if pocket_side < 0:
        bot = b.face([(True, outer), (False, pocket), (False, hole)], 0.0, -1, 6400 - 1600 - ha)
        floor = b.face([(True, pocket)], depth, -1, 1600)
        top = b.face([(True, outer), (False, hole)], t, +1, 6400 - ha)
    else:
        top = b.face([(True, outer), (False, pocket), (False, hole)], t, +1, 6400 - 1600 - ha)
        floor = b.face([(True, pocket)], t - depth, +1, 1600)
        bot = b.face([(True, outer), (False, hole)], 0.0, -1, 6400 - ha)
    for nrm in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0)):
        b.side_face(nrm, 80 * t)
        b.side_face(nrm, 40 * depth)
    b.volume_mm3 = (6400 - ha) * t - 1600 * depth
    return b, top, bot, floor


def side_plate(t=4.0):
    """A plate standing in the YZ plane (thickness along X): 90 (along Y) x 40 (along Z), one hole."""
    b = Builder("x", t, "Steel 4mm")
    top, bot = slab(b, rect(0, 0, 90, 40), [[("circle", (45, 20), 5)]], 3600, math.pi * 25, 260)
    return b, top, bot


def spline_plate(t=2.0, r=20.0):
    """A quarter disc whose arc is a rational quadratic B-spline (exactly a circle of radius r)."""
    b = Builder("z", t, "Aluminum 2mm")
    w = math.sqrt(0.5)
    sp = {"degree": 2, "knots": [0, 0, 0, 1, 1, 1], "isRational": True,
          "controlPoints": [r, 0, 0, 1.0, r, r, 0, w, 0, r, 0, 1.0]}
    mid = (r * math.cos(math.pi / 4), r * math.sin(math.pi / 4))
    outer = [("line", (0, 0), (r, 0)), ("spline", sp, (r, 0), (0, r), mid), ("line", (0, r), (0, 0))]
    area = math.pi * r * r / 4
    top, bot = slab(b, outer, [], area, 0.0, 2 * r + math.pi * r / 2)
    return b, top, bot


def ellipse_and_other_plate(t=3.0):
    """A plate with an elliptic hole and one edge of type OTHER (only tessellation draws it)."""
    b = Builder("z", t, "Mild steel")
    other = [(60, 40), (40, 45), (20, 40)]                 # the top edge, as a polyline
    outer = [("line", (0, 0), (60, 0)), ("line", (60, 0), (60, 40)), ("other", other), ("line", (20, 40), (0, 40)),
             ("line", (0, 40), (0, 0))]
    hole = [("ellipse", (30, 20), 10, 5)]
    top, bot = slab(b, outer, [hole], 60 * 40 + 150, math.pi * 50, 200)
    return b, top, bot


def configured_plate(length):
    b = Builder("z", 3.0, "Aluminum - 6061")
    slab(b, rect(0, 0, length, 30), [[("circle", (10, 15), 2.5)], [("circle", (length - 10, 15), 2.5)]],
         length * 30, 2 * math.pi * 6.25, 2 * (length + 30))
    return b


def round_bar():
    """A shaft with no flat face (ends left out on purpose: a revolved, domed body)."""
    b = Builder("z", 10.0, "Steel")
    b.cyl_face(3000.0)
    b.faces.append({"btType": "BTExportModelFace-1363", "id": "Fsph", "area": 300e-6,
                    "surface": {"btType": "BTSphereDescription-1263", "type": "SPHERE", "origin": V((0, 0, 0))},
                    "loops": []})
    b.volume_mm3 = 5000
    return b
