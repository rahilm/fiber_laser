"""Untrusted input limits (mlaser.io.limits, mlaser.project.format): zip bombs, JSON depth,
DXF block-expansion bombs, SVG <use> bombs, and Cancel.

The hostile files are the Review 2 probes (work/review2/probes: mlz_probe.py, import_probe.py),
generated here in tmp_path; nothing large is committed.  Before the limits: a 3.6 MB .mlz cost
7.2 GB RSS and 14 s, a 26 kB DXF of nested INSERTs and a 612 kB SVG <use> bomb ran for minutes
(TIMEOUT > 120 s, RSS growing past 1 GB / 5 GB), deep JSON escaped as RecursionError.
"""
from __future__ import annotations

import hashlib
import io
import json
import time
import tracemalloc
import zipfile

import pytest

from mlaser.io import limits as LIM
from mlaser.io.limits import ImportCancelled, ImportLimitError
from mlaser.model import Contour, Document, Line, Part
from mlaser.project import format as F
from mlaser.project.format import ProjectError, dumps_project, load_project, loads_project


# =========================================================================== .mlz
def _doc() -> Document:
    pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    return Document([Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)])])


def _members() -> dict[str, bytes]:
    z = zipfile.ZipFile(io.BytesIO(dumps_project(_doc())))
    return {i.filename: z.read(i) for i in z.infolist()}


def _zip(members: dict[str, bytes], *, unlisted=(), lie: dict | None = None) -> bytes:
    """Re-sign the manifest for ``members`` (``lie``: sizes to claim instead) and zip them;
    ``unlisted``: (name, data) written without a manifest entry."""
    man = json.loads(members["manifest.json"])
    man["files"] = {k: {"size": len(v), "sha256": hashlib.sha256(v).hexdigest()}
                    for k, v in members.items() if k not in ("manifest.json", "mimetype")}
    for k, size in (lie or {}).items():
        man["files"][k]["size"] = size
    members = dict(members, **{"manifest.json": json.dumps(man).encode()})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", members["mimetype"], compress_type=zipfile.ZIP_STORED)
        for k, v in members.items():
            if k != "mimetype":
                z.writestr(k, v)
        for k, v in unlisted:
            z.writestr(k, v)
    return buf.getvalue()


def _zeros_member(path, name: str, mb: int) -> None:
    """Append ``mb`` MiB of zeros as member ``name``, streamed (DEFLATE about 1000:1)."""
    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as z:
        zi = zipfile.ZipInfo(name)
        zi.compress_type = zipfile.ZIP_DEFLATED
        with z.open(zi, "w", force_zip64=True) as w:
            chunk = b"\0" * (1 << 20)
            for _ in range(mb):
                w.write(chunk)


def _peak(fn):
    tracemalloc.start()
    t0 = time.monotonic()
    try:
        out = fn()
    finally:
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return out, peak, time.monotonic() - t0


def test_unlisted_zip_bomb_is_never_read(tmp_path):
    """The probe's bomb_3.5g: a member the manifest does not list is not inflated at all."""
    p = tmp_path / "bomb.mlz"
    p.write_bytes(_zip(_members()))
    _zeros_member(p, "blobs/bomb.bin", 512)
    assert p.stat().st_size < 2 << 20
    pr, peak, dt = _peak(lambda: load_project(p))
    assert len(pr.document.parts) == 1
    assert peak < 32 << 20 and dt < 10


def test_listed_member_over_the_compression_ratio_is_refused(tmp_path):
    m = _members()
    m["sections/x.json"] = b"[" + b"0," * (24 << 20) + b"0]"      # 48 MiB, about 1000:1
    data = _zip(m)
    tracemalloc.start()
    try:
        with pytest.raises(ProjectError, match=r"expands more than 200:1"):
            loads_project(data, "x.mlz")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 64 << 20          # stopped at about RATIO_MIN, not at 48 MiB + copies
    # a raster blob may compress that well: only the size limits apply there
    m2 = _members()
    m2["blobs/r.bin"] = b"\0" * (24 << 20)
    assert loads_project(_zip(m2), "ok.mlz").document.parts


def test_member_larger_than_its_manifest_size_stops_early(tmp_path):
    m = _members()
    m["blobs/r.bin"] = b"\0" * (64 << 20)
    data = _zip(m, lie={"blobs/r.bin": 1024})
    tracemalloc.start()
    try:
        with pytest.raises(ProjectError, match=r"blobs/r\.bin is damaged"):
            loads_project(data, "liar.mlz")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 16 << 20             # one chunk read, not 64 MiB


def test_member_total_and_claimed_size_limits(monkeypatch):
    m = _members()
    m["blobs/a.bin"] = bytes(range(256)) * 8192                       # 2 MiB, incompressible-ish
    m["blobs/b.bin"] = bytes(reversed(range(256))) * 8192
    data = _zip(m)
    monkeypatch.setattr(F, "MAX_MEMBER", 1 << 20)
    with pytest.raises(ProjectError, match=r"more than the limit of 1 MB per member"):
        loads_project(data, "big.mlz")
    monkeypatch.setattr(F, "MAX_MEMBER", 256 << 20)
    monkeypatch.setattr(F, "MAX_TOTAL", 3 << 20)
    with pytest.raises(ProjectError, match=r"lists 4 MB of content"):
        loads_project(data, "big.mlz")
    # the claim is not trusted: a manifest that under-states sizes is caught by the running total
    small = _zip(m, lie={"blobs/a.bin": 4 << 20 >> 2, "blobs/b.bin": 4 << 20 >> 2})
    monkeypatch.setattr(F, "MAX_TOTAL", (2 << 20) + (512 << 10))
    with pytest.raises(ProjectError):
        loads_project(small, "big.mlz")


def test_too_many_members():
    buf = io.BytesIO(_zip(_members()))
    with zipfile.ZipFile(buf, "a") as z:
        for i in range(F.MAX_MEMBERS + 1):
            z.writestr(f"x/{i}", b"")
    with pytest.raises(ProjectError, match=r"entries \(more than 65,536\)"):
        loads_project(buf.getvalue(), "many.mlz")


@pytest.mark.parametrize("member", ["document.json", "sections/x.json", "manifest.json"])
def test_deep_json_is_a_project_error(member):
    """deep_document / deep_section / deep_manifest of the probe (RecursionError escaped)."""
    m = _members()
    deep = b"[" * 200_000 + b"]" * 200_000
    if member == "document.json":
        d = json.loads(m[member])
        m[member] = json.dumps(d).replace('"parts": [', '"deep": ' + deep.decode() + ', "parts": [', 1).encode()
    else:
        m[member] = deep
    if member == "manifest.json":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for k, v in m.items():
                z.writestr(k, v)
        data = buf.getvalue()
    else:
        data = _zip(m)
    with pytest.raises(ProjectError):
        loads_project(data, "deep.mlz")


def test_json_depth_limit_and_strings():
    ok = ("{\"a\":" * F.MAX_JSON_DEPTH + "1" + "}" * F.MAX_JSON_DEPTH).encode()
    assert F._json_depth_ok(ok)
    assert not F._json_depth_ok(b"[" + ok + b"]")
    assert F._json_depth_ok(json.dumps({"s": "[" * 500 + '\\"{'}).encode())    # brackets in strings
    m = _members()
    m["sections/x.json"] = ("{\"a\":" * 101 + "1" + "}" * 101).encode()
    with pytest.raises(ProjectError, match=r"nested more than 100 levels"):
        loads_project(_zip(m), "deep.mlz")


def test_bigint_and_memory_error_are_project_errors(monkeypatch):
    m = _members()
    m["sections/x.json"] = b"[" + b"9" * 5000 + b"]"
    with pytest.raises(ProjectError, match=r"integer string"):
        loads_project(_zip(m), "bigint.mlz")
    good = _zip(_members())

    def boom(*a, **k):
        raise MemoryError
    monkeypatch.setattr(F, "validate", boom)
    with pytest.raises(ProjectError, match=r"not enough memory"):
        loads_project(good, "mem.mlz")


def test_mlz_open_cancel(tmp_path):
    p = tmp_path / "ok.mlz"
    p.write_bytes(_zip(_members()))
    with LIM.context() as ctx:
        ctx.cancel()
        with pytest.raises(ImportCancelled):
            load_project(p)
    assert load_project(p).document.parts      # outside a context: unchanged


# =========================================================================== DXF
def _dxf(tmp_path, build, name="t.dxf"):
    ezdxf = pytest.importorskip("ezdxf")
    d = ezdxf.new()
    build(d)
    p = tmp_path / name
    d.saveas(p)
    return str(p)


def _nested(d, levels=9, fan=10):
    b = d.blocks.new("B0")
    b.add_line((0, 0), (1, 0))
    b.add_line((1, 0), (1, 1))
    for k in range(1, levels):
        b = d.blocks.new(f"B{k}")
        for i in range(fan):
            b.add_blockref(f"B{k - 1}", (i * 2.0 ** k, 0))
    d.modelspace().add_blockref(f"B{levels - 1}", (0, 0))


def _minsert(d):
    d.blocks.new("M").add_line((0, 0), (1, 0))
    ins = d.modelspace().add_blockref("M", (0, 0))
    ins.dxf.row_count = 30000
    ins.dxf.column_count = 30000
    ins.dxf.row_spacing = 2
    ins.dxf.column_spacing = 2


def _self_ref(d):
    b = d.blocks.new("S")
    b.add_line((0, 0), (1, 0))
    b.add_blockref("S", (1, 1))
    d.modelspace().add_blockref("S", (0, 0))


@pytest.mark.parametrize("mode", ["faithful", "modern"])
def test_dxf_nested_insert_bomb_is_refused_before_expanding(tmp_path, mode):
    from mlaser.io.dxf_import import import_dxf
    p = _dxf(tmp_path, _nested)
    t0 = time.monotonic()
    with pytest.raises(ImportLimitError, match=r"more than 2,000,000 entities"):
        import_dxf(p, mode=mode)
    assert time.monotonic() - t0 < 10


def test_dxf_minsert_cap(tmp_path):
    from mlaser.io.dxf_import import import_dxf
    p = _dxf(tmp_path, _minsert)
    with pytest.raises(ImportLimitError, match=r"MINSERT block array of 900,000,000 copies"):
        import_dxf(p, mode="modern")
    assert len(import_dxf(p, mode="faithful").parts) == 1      # DxfParse draws one copy


@pytest.mark.parametrize("mode,parts", [("faithful", 17), ("modern", 33)])
def test_dxf_self_insert_stays_bounded_by_the_depth_limit(tmp_path, mode, parts):
    from mlaser.io.dxf_import import import_dxf
    assert len(import_dxf(_dxf(tmp_path, _self_ref), mode=mode).parts) == parts


def test_dxf_expansion_count_is_exact_for_a_normal_file(tmp_path):
    import ezdxf

    from mlaser.io.dxf_import import check_expansion
    p = _dxf(tmp_path, lambda d: _nested(d, levels=3, fan=4))
    d = ezdxf.readfile(p)
    # modelspace: 1 INSERT of B2; B2: 4 INSERTs of B1; B1: 4 INSERTs of B0; B0: 2 lines
    b1 = 4 * (1 + 2)
    b2 = 4 * (1 + b1)
    assert check_expansion([d.modelspace()]) == 1 + b2 == 53
    with pytest.raises(ImportLimitError):
        check_expansion([d.modelspace()], limit=20)


def test_dxf_modern_walk_counts_at_runtime(tmp_path):
    """The modern traversal's own budget (virtual entities, DIMENSION blocks...) and that the
    per-entity ``except Exception`` does not swallow the limit."""
    import ezdxf

    from mlaser.io import dxf_modern
    p = _dxf(tmp_path, lambda d: _nested(d, levels=3, fan=4))
    d = ezdxf.readfile(p)
    with pytest.raises(ImportLimitError):
        list(dxf_modern._walk(d.modelspace(), budget=LIM.entity_budget(15)))
    assert len(list(dxf_modern._walk(d.modelspace()))) == 32


def test_dxf_cancel(tmp_path):
    from mlaser.io.dxf_import import import_dxf
    p = _dxf(tmp_path, lambda d: _nested(d, levels=3, fan=4))
    with LIM.context() as ctx:
        ctx.cancel()
        with pytest.raises(ImportCancelled):
            import_dxf(p)


# =========================================================================== SVG
def test_svg_use_bomb_is_refused(tmp_path):
    """svg_use_bomb_20k_x_200: one symbol of 200 paths used 20000 times (4 M elements)."""
    from mlaser.io.svg_import import import_svg
    paths = "".join(f'<path d="M{i} 0 L{i} 10"/>' for i in range(200))
    uses = "".join(f'<use href="#s" x="{i % 100}" y="{i // 100}"/>' for i in range(20000))
    p = tmp_path / "usebomb.svg"
    p.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1000"><defs>'
                 f'<symbol id="s">{paths}</symbol></defs>{uses}</svg>')
    t0 = time.monotonic()
    with pytest.raises(ImportLimitError, match=r"more than 500,000 elements"):
        import_svg(str(p))
    assert time.monotonic() - t0 < 20


def test_svg_deep_nesting_and_nested_uses(tmp_path):
    from mlaser.io.svg_import import import_svg
    p = tmp_path / "deep.svg"
    p.write_text('<svg xmlns="http://www.w3.org/2000/svg">' + "<g>" * 100000 + '<path d="M0 0 L1 1"/>'
                 + "</g>" * 100000 + "</svg>")
    with pytest.raises(ImportLimitError, match=r"nested too deeply"):
        import_svg(str(p))
    syms = "".join(f'<symbol id="s{k}">' + "".join(f'<use href="#s{k + 1}"/>' for _ in range(10))
                   + "</symbol>" for k in range(8)) + '<symbol id="s8"><path d="M0 0 L1 1"/></symbol>'
    p2 = tmp_path / "nest.svg"
    p2.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><defs>{syms}</defs>'
                  '<use href="#s0"/></svg>')
    import_svg(str(p2))                 # 10^8 nominal: bounded by the 20000-expansion guard


def test_svg_small_uses_still_import(tmp_path):
    from mlaser.io.svg_import import import_svg
    p = tmp_path / "ok.svg"
    p.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" viewBox="0 0 100 100">'
                 '<defs><symbol id="s"><rect width="5" height="5"/></symbol></defs>'
                 + "".join(f'<use href="#s" x="{10 * i}" y="0"/>' for i in range(5)) + "</svg>")
    assert len(import_svg(str(p)).parts) == 5
