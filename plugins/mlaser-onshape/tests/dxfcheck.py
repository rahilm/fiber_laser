"""Read a DXF back with ezdxf and describe it (entities, bounding box, closed chains)."""
import math

import ezdxf


def read(path):
    doc = ezdxf.readfile(str(path))
    assert doc.dxfversion == "AC1009", doc.dxfversion          # R12
    return doc, list(doc.modelspace())


def endpoints(e):
    t = e.dxftype()
    if t == "LINE":
        return [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
    if t == "ARC":
        c, r = e.dxf.center, e.dxf.radius
        a0, a1 = math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle)
        return [(c.x + r * math.cos(a0), c.y + r * math.sin(a0)), (c.x + r * math.cos(a1), c.y + r * math.sin(a1))]
    if t == "POLYLINE":
        pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
        return [pts[0], pts[-1]]
    return []


def points(e):
    t = e.dxftype()
    if t == "POLYLINE":
        return [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
    if t == "CIRCLE":
        c, r = e.dxf.center, e.dxf.radius
        return [(c.x + r * math.cos(a), c.y + r * math.sin(a)) for a in [i * math.pi / 32 for i in range(64)]]
    if t == "ARC":
        c, r = e.dxf.center, e.dxf.radius
        a0, a1 = e.dxf.start_angle, e.dxf.end_angle
        sweep = (a1 - a0) % 360 or 360
        return [(c.x + r * math.cos(math.radians(a0 + sweep * i / 32)), c.y + r * math.sin(math.radians(a0 + sweep * i / 32)))
                for i in range(33)]
    return endpoints(e)


def bbox(ents):
    pts = [p for e in ents for p in points(e)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def chains(ents, tol=1e-3):
    """Closed chains (lists of entities); asserts every open entity joins up."""
    closed = [[e] for e in ents if e.dxftype() == "CIRCLE"]
    rest = [e for e in ents if e.dxftype() != "CIRCLE"]
    while rest:
        ch = [rest.pop(0)]
        a, b = endpoints(ch[0])
        start, end = a, b
        while math.dist(start, end) > tol:
            for i, e in enumerate(rest):
                p, q = endpoints(e)
                if math.dist(p, end) <= tol:
                    end = q
                elif math.dist(q, end) <= tol:
                    end = p
                else:
                    continue
                ch.append(rest.pop(i))
                break
            else:
                raise AssertionError(f"open chain ending at {end}")
        closed.append(ch)
    return closed
