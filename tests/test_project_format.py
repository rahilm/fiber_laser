"""Native project format .mlz (mlaser.project, docs/dev/project-format.md)."""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

from mlaser.formats.chf import dumps_chf, load_chf
from mlaser.model import Arc, Contour, Craft, Document, Glyph, LayerParams, LeadParams, Line, MicroJoint, Part
from mlaser.project import (Autosave, Project, ProjectError, dumps_project, export_chf, load_project, loads_project,
                            open_document, save_project)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "data"
sys.path.insert(0, str(ROOT / "tools"))
import make_goldens as G  # noqa: E402  (the golden generators: samples and seeded random documents)


def _roundtrip(doc_or_project):
    return loads_project(dumps_project(doc_or_project))


def _same_doc(a: Document, b: Document) -> None:
    assert len(a.parts) == len(b.parts)
    for i, (p, q) in enumerate(zip(a.parts, b.parts)):
        assert p == q, f"part {i}"
    assert a.layers == b.layers
    assert a.version == b.version
    assert dict(a.extra) == dict(b.extra) or set(a.extra) == set(b.extra)


# --------------------------------------------------------------------------- samples / random
@pytest.mark.parametrize("name, doc, host", G.sample_docs(), ids=lambda v: v if isinstance(v, str) else "")
def test_sample_documents_roundtrip(name, doc, host):
    back = _roundtrip(doc).document
    _same_doc(doc, back)
    assert dumps_chf(back, version=back.version) == dumps_chf(doc, version=doc.version)


@pytest.mark.parametrize("path", sorted((DATA / "chf").glob("*.chf")), ids=lambda p: p.name)
def test_chf_to_mlz_to_chf_is_byte_exact(path, tmp_path):
    src = path.read_bytes()
    save_project(tmp_path / "p.mlz", load_chf(str(path)))
    back = load_project(tmp_path / "p.mlz").document
    assert export_chf(back) == src


def test_300_random_documents_roundtrip():
    from mlaser.cam.toolpath import CamParams, build_toolpath

    for seed in range(G.N_RANDOM):
        doc, *_ = G.random_doc(seed)
        back = _roundtrip(doc).document
        _same_doc(doc, back)
        assert dumps_chf(back) == dumps_chf(doc), seed
        if seed % 25 == 0:
            a = build_toolpath(doc, CamParams())
            b = build_toolpath(back, CamParams())
            assert G.items_hash(a) == G.items_hash(b), seed


def test_edited_chf_exports_the_same_after_a_project_roundtrip():
    from mlaser.cam.manual import contour_craft, set_start_at

    doc = load_chf(str(DATA / "chf" / "sample_v9.chf"))
    part = doc.parts[0]
    c = part.contours[3]
    part.contours[3] = Contour([Line((s.start[0] + 1, s.start[1]), (s.end[0] + 1, s.end[1]))
                                if isinstance(s, Line) else Arc((s.start[0] + 1, s.start[1]), (s.end[0] + 1, s.end[1]),
                                                                (s.center[0] + 1, s.center[1]), s.ccw)
                                for s in c.segments], c.closed, c.extra.materialize())
    cr = contour_craft(part, 5, create=True)
    cr.lead_in = LeadParams(kind=2, length=4.0, radius=1.5)
    set_start_at(part, 5, 3.0)
    before = export_chf(doc)
    assert before != (DATA / "chf" / "sample_v9.chf").read_bytes()
    back = _roundtrip(doc).document
    assert back.parts[0].contours[5].craft.lead_in.length == 4.0
    assert export_chf(back) == before


# --------------------------------------------------------------------------- typed data survives
def _rich_project() -> Project:
    from mlaser.cam.raster_engraving import RasterEngraving, make_raster_part

    rect = G._rect(0, 0, 40, 20)
    hole = G._circle(20, 10, 4)
    hole.craft = Craft(layer=3, lead_in=LeadParams(kind=1, length=3.0))
    hole.set_source_glyphs([Glyph(4, {"center": (20.0, 10.0), "radius": 4.0})])
    stale = G._rect(50, 0, 5, 5)
    stale.set_source_glyphs([Glyph(6, {"flag": 1, "verts": [((50.0, 0.0), 0.0)]})])
    stale.segments = stale.segments[:3]
    cr = Craft(layer=2, kerf_comp=1, kerf_width=0.12, micro_joints=[(5.0, 0.8), 12.0],
               cool_points=[7.5, (40.0, 20.0)], start_param=0.25, overcut=-0.6, lead_error=True)
    cr.joint_width = 0.4
    cr.extra["pwm_mode"] = 1
    p = Part([rect, hole, stale], cr, name="plate", selected=True)
    p.extra.update({"array_cell": (1, 2), "nest": {"part_id": 0, "x": 1.5}, "group": True})
    p.contours[0].extra["alpha_source"] = G._rect(0, 0, 40, 20)
    r = RasterEngraving.from_segments([0.0, 1.0, 0.0], [5.0, 4.0, 6.0], [0.0, 0.0, 0.1],
                                      power=[100, 200, 300], rho=10, params={"mode": "floyd"})
    rp = make_raster_part(r, Craft(layer=4), name="photo")
    doc = Document([p, rp], {2: LayerParams(2, "#ff0000", speed=55.5, pierce={"time": 0.2}, extra={"gp": {"a": 1}}),
                             4: LayerParams(4)}, version=9, extra={"dock_point": (3.0, 4.0), "order": [(0, 1), (0, 0)]})
    from mlaser.nest.apply import apply_result
    from mlaser.nest.engine import Nester
    from mlaser.nest.model import NestJob, NestParams, NestPart, NestSheet

    job = NestJob(params=NestParams(time_limit_s=2.0, rotation_step=90.0))
    job.add_part(NestPart([Part([G._rect(0, 0, 30, 20)], Craft(layer=2))], quantity=3))
    job.add_sheet(NestSheet(200.0, 100.0))
    job.result = Nester(job).run()
    assert apply_result(job).parts
    return Project(document=doc, technology={"2": {"material": "S235", "thickness_mm": 2.0, "gas": "O2"}},
                   queue=[{"name": "plate", "count": 3, "state": "waiting"}], nest=job,
                   sections={"cam": {"sort_mode": 20, "origin": (0.0, 0.0)}},
                   fonts={"Custom.ttf": b"\0\1\0\0fake"})


def test_typed_fields_raster_nest_queue_and_sections_survive(tmp_path):
    pr = _rich_project()
    save_project(tmp_path / "rich.mlz", pr)
    back = load_project(tmp_path / "rich.mlz")
    d0, d1 = pr.document, back.document
    p0, p1 = d0.parts[0], d1.parts[0]
    assert p1 == p0
    assert p1.craft.micro_joints == [MicroJoint(5.0, 0.8), MicroJoint(12.0, 0.4)]
    p1.craft.joint_width = 0.9                       # an auto-width joint follows the default
    assert p1.craft.micro_joints[1] == MicroJoint(12.0, 0.9)
    assert p1.craft.cool_points == [7.5, (40.0, 20.0)] and p1.craft.cool_point == (40.0, 20.0)
    assert (p1.craft.start_param, p1.craft.overcut, p1.craft.lead_error) == (0.25, -0.6, True)
    assert p1.contours[1].craft.lead_in.length == 3.0
    assert p1.contours[1].source_glyphs()[0].data["center"] == (20.0, 10.0)
    assert p1.contours[2].glyphs and p1.contours[2].source_glyphs() is None      # stays stale
    assert p1.extra["array_cell"] == (1, 2) and isinstance(p1.contours[0].extra["alpha_source"], Contour)
    r0, r1 = d0.parts[1].raster, d1.parts[1].raster
    for k in ("x0", "x1", "row", "row_y", "power"):
        np.testing.assert_array_equal(getattr(r0, k), getattr(r1, k))
        assert getattr(r1, k).dtype == getattr(r0, k).dtype
    assert (r1.rho, r1.params, r1.frame) == (10, {"mode": "floyd"}, r0.frame)
    assert d1.layers == d0.layers and d1.extra == d0.extra
    assert back.technology == pr.technology and back.queue == pr.queue
    assert back.sections == {"cam": {"sort_mode": 20, "origin": (0.0, 0.0)}}
    assert back.fonts == {"Custom.ttf": b"\0\1\0\0fake"}
    j0, j1 = pr.nest, back.nest
    assert j1.params == j0.params and len(j1.parts) == 1 and j1.parts[0].parts == j0.parts[0].parts
    assert j1.result.layouts == j0.result.layouts and j1.result.placed == j0.result.placed


def test_craft_survives_save_load_and_nesting(tmp_path):
    """architecture.md P2-4: per-contour craft survives save / load and nesting."""
    from mlaser.nest.model import Placement
    from mlaser.nest.shapes import transform_part

    pr = _rich_project()
    back = _roundtrip(pr).document
    moved = transform_part(back.parts[0], Placement(0, 100.0, 50.0, 90.0))
    assert moved.contours[1].craft == pr.document.parts[0].contours[1].craft
    assert moved.craft.micro_joints == back.parts[0].craft.micro_joints
    assert moved.craft.cool_points[0] == 7.5 and moved.craft.cool_points[1] != (40.0, 20.0)
    again = _roundtrip(Document([moved])).document
    assert again.parts[0] == moved


def test_unencodable_extra_values_are_reported_not_fatal():
    doc = Document([Part([G._rect(0, 0, 1, 1)], Craft())])
    doc.parts[0].extra["callback"] = lambda: None
    pr = Project(document=doc)
    back = loads_project(dumps_project(pr))
    assert "callback" not in back.document.parts[0].extra
    assert any("callback" in n for n in pr.notes)
    assert any("callback" in n for n in back.meta["dropped"])


def test_project_layout_and_manifest():
    b = dumps_project(_rich_project())
    z = zipfile.ZipFile(io.BytesIO(b))
    names = z.namelist()
    assert names[0] == "mimetype" and z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    assert z.read("mimetype") == b"application/vnd.mlaser.project+zip"
    man = json.loads(z.read("manifest.json"))
    assert man["format"] == "mlaser-project" and man["version"] == 1
    assert set(man["files"]) == set(names) - {"mimetype", "manifest.json"}
    assert {"document.json", "sections/technology.json", "sections/queue.json", "nest/job.json",
            "fonts/Custom.ttf"} <= set(names)
    assert any(n.endswith(".npy") for n in names)
    # a second save of the same content: the same members, the same bytes except the save times
    # (manifest) and the nest's measured run time.  (It used to assert that the two archives
    # differ: with Windows' coarse clock before Python 3.13 they can be identical.)
    import re
    z2 = zipfile.ZipFile(io.BytesIO(dumps_project(_rich_project())))
    assert z2.namelist() == names

    def norm(n, data):
        return re.sub(rb'"elapsed_s":[0-9.e-]+', b"", data) if n == "nest/job.json" else data
    assert all(norm(n, z2.read(n)) == norm(n, z.read(n)) for n in names if n != "manifest.json")
    from mlaser.project.schema import DOCUMENT_SCHEMA, validate
    assert validate(json.loads(z.read("document.json")), DOCUMENT_SCHEMA) == []


# --------------------------------------------------------------------------- versions
def test_v0_fixture_migrates():
    pr = load_project(DATA / "project" / "v0_sample.mlz")
    assert pr.notes == ["migrated from project format 0"] and pr.meta["migrated_from"] == 0
    p = pr.document.parts[0]
    c = p.craft
    assert c.micro_joints == [MicroJoint(10.0, 0.7), MicroJoint(30.0, 0.8)] and c.joint_width == 0.7
    assert c.cool_points == [12.5, (5.0, 0.0)]
    assert (c.start_param, c.overcut, c.lead_error) == (0.25, 1.2, True)
    assert dict(dict.items(c.extra)) == {"pwm_mode": 1}
    assert p.contours[1].craft.layer == 3 and dict(dict.items(p.contours[1].extra)) == {"depth": 2}
    assert p.extra["array_cell"] == (0, 1) and pr.document.extra["dock_point"] == (1.0, 2.0)
    assert pr.document.layers[2].speed == 80.0
    assert pr.technology["2"]["material"] == "S235" and pr.queue == [{"name": "plate", "count": 3}]
    # saved again it is a current-version file
    again = _roundtrip(pr)
    assert again.notes == [] and again.document.parts[0] == p


def test_newer_format_is_refused_with_a_clear_message():
    b = dumps_project(Document())
    members = {i.filename: zipfile.ZipFile(io.BytesIO(b)).read(i) for i in zipfile.ZipFile(io.BytesIO(b)).infolist()}
    man = json.loads(members["manifest.json"])
    man["version"] = 99
    members["manifest.json"] = json.dumps(man).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    with pytest.raises(ProjectError, match=r"newer mlaser \(project format 99"):
        loads_project(buf.getvalue(), "new.mlz")


# --------------------------------------------------------------------------- damage
def _members(b: bytes) -> dict[str, bytes]:
    z = zipfile.ZipFile(io.BytesIO(b))
    return {i.filename: z.read(i) for i in z.infolist()}


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_corrupted_files_give_clear_errors(tmp_path):
    good = dumps_project(load_chf(str(DATA / "chf" / "sample_v5.chf")))
    cases = {
        "truncated": (good[: len(good) // 2], r"broken\.mlz: the file is damaged"),
        "not a zip": (b"#!chf 9\nhello", r"not an mlaser project \(\.mlz\) file"),
        "empty": (b"", r"not an mlaser project"),
    }
    m = _members(good)
    doc = json.loads(m["document.json"])
    doc["parts"][0]["contours"][0]["segments"][0] = ["Q", 1, 2]
    bad_schema = dict(m, **{"document.json": json.dumps(doc).encode()})
    cases["checksum"] = (_zip(bad_schema), r"document\.json is damaged \(checksum mismatch\)")
    from mlaser.project.migrations import manifest_for
    bad_schema["manifest.json"] = json.dumps(manifest_for(bad_schema, 1)).encode()
    cases["schema"] = (_zip(bad_schema), r"does not match the schema: document\.parts\[0\]\.contours\[0\]\.segments\[0\]\[0\]")
    no_doc = {k: v for k, v in m.items() if k != "document.json"}
    cases["missing"] = (_zip(no_doc), r"document\.json is missing")
    flipped = bytearray(good)
    i = good.index(b"document.json") + 40            # inside the deflated document member
    flipped[i] ^= 0xFF
    cases["bitflip"] = (bytes(flipped), r"damaged")
    for what, (data, pat) in cases.items():
        p = tmp_path / "broken.mlz"
        p.write_bytes(data)
        with pytest.raises(ProjectError, match=pat):
            load_project(p)
    with pytest.raises(ProjectError, match="cannot read the file"):
        load_project(tmp_path / "missing.mlz")


# --------------------------------------------------------------------------- atomic save, autosave
def test_save_is_atomic(tmp_path, monkeypatch):
    p = tmp_path / "job.mlz"
    save_project(p, Document([Part([G._rect(0, 0, 1, 1)])]))
    old = p.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        save_project(p, Document())
    assert p.read_bytes() == old
    assert sorted(x.name for x in tmp_path.iterdir()) == ["job.mlz"]   # no temporary left


def test_autosave_writes_both_and_recovers(tmp_path):
    a = Autosave(tmp_path / "File")
    assert a.recover() is None
    doc = load_chf(str(DATA / "chf" / "sample_v5.chf"))
    doc.parts[0].craft.cool_points = [3.0]          # only the project keeps it
    written = a.save(Project(document=doc, queue=[{"name": "x"}]))
    assert [w.name for w in written] == ["autosave.mlz", "autosave.chf"]
    assert (tmp_path / "File" / "autosave.chf").read_bytes() == export_chf(doc)
    rec = a.recover()
    assert rec.path.name == "autosave.mlz" and rec.document.parts[0].craft.cool_points == [3.0]
    assert rec.project.queue == [{"name": "x"}]
    # a damaged project falls back to the .chf of the original's recovery path
    (tmp_path / "File" / "autosave.mlz").write_bytes(b"PK\3\4garbage")
    rec = a.recover()
    assert rec.path.name == "autosave.chf" and "damaged" in rec.notes[0]
    assert len(rec.document.parts) == len(doc.parts)
    # an autosave.chf newer than the project (written by the original program) wins
    a.save(doc)
    t = (tmp_path / "File" / "autosave.mlz").stat().st_mtime
    os.utime(tmp_path / "File" / "autosave.chf", (t + 10, t + 10))
    assert a.available().name == "autosave.chf"


def test_open_document_reads_both_formats(tmp_path):
    doc = load_chf(str(DATA / "chf" / "sample_v5.chf"))
    save_project(tmp_path / "a.mlz", doc)
    assert open_document(tmp_path / "a.mlz").parts == doc.parts
    assert open_document(DATA / "chf" / "sample_v5.chf").parts == doc.parts


def test_deep_copies_of_loaded_projects_are_independent():
    back = _roundtrip(_rich_project()).document
    cp = copy.deepcopy(back)
    cp.parts[0].contours[1].craft.layer = 9
    assert back.parts[0].contours[1].craft.layer == 3


def test_command_line_convert_info_and_schema(tmp_path, capsys):
    from mlaser.project.__main__ import main

    src = DATA / "chf" / "sample_v9.chf"
    assert main(["convert", str(src), str(tmp_path / "a.mlz")]) == 0
    assert main(["convert", str(tmp_path / "a.mlz"), str(tmp_path / "a.chf")]) == 0
    assert (tmp_path / "a.chf").read_bytes() == src.read_bytes()
    assert main(["info", str(tmp_path / "a.mlz")]) == 0 and "parts 7" in capsys.readouterr().out
    assert main(["schema"]) == 0 and '"document"' in capsys.readouterr().out
    (tmp_path / "bad.mlz").write_bytes(b"nope")
    assert main(["info", str(tmp_path / "bad.mlz")]) == 1
    assert "not an mlaser project" in capsys.readouterr().err
