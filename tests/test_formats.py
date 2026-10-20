"""Round-trip and semantics tests for mlaser.formats (own samples in tests/data; the original's
files too when a local copy of its install is available, tests/vendor_samples.py)."""
from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from mlaser.model import Arc, Contour, Document, Line, Part
from mlaser.formats import chf, scfile
from mlaser.formats.chf import dumps_chf, load_chf, loads_chf, save_chf
from mlaser.i18n.langfile import Translator
from mlaser.formats.params import (MachineParams, ParamStore, layer_category, layer_defs,
                                   load_layer_params, param_defs, save_layer_params)
from mlaser.formats.scio import fmt_double, split_lines
from vendor_samples import requires, vendor_file

DATA = Path(__file__).parent / "data"
APP = DATA / "appdir"


# ------------------------------------------------------------------ scio
def test_fmt_double_rules():
    assert fmt_double(0.0) == "0.0"
    assert fmt_double(5e-11) == "0.0"
    assert fmt_double(5e-7) == "0.0000005000"
    assert fmt_double(0.01) == "0.010000"
    assert fmt_double(-269.321) == "-269.321000"


def test_split_lines_crlf():
    assert split_lines(b"a\r\nb\r\n\r\nc") == [b"a", b"b", b"", b"c"]


# ------------------------------------------------------------------ chf
# tests/data/chf: drawings written by tools/make_test_chf.py; the original's own autosave files
# are checked too when a local copy of the install is available (tests/vendor_samples.py)
OWN_CHF = [("sample_v9.chf", 9), ("sample_v5.chf", 5)]
VENDOR_CHF = [("File/autosave.chf", 9), ("File/Temp/tempGraph.chf", 5)]
CHF_CASES = ([pytest.param(DATA / "chf" / n, v, id=n) for n, v in OWN_CHF]
             + [pytest.param(rel, v, id="vendor:" + rel, marks=requires(rel)) for rel, v in VENDOR_CHF])


def _src(p):
    return p if isinstance(p, Path) else vendor_file(p)


@pytest.mark.parametrize("src,version", CHF_CASES)
def test_chf_roundtrip_bytes(tmp_path, src, version):
    src = _src(src)
    doc = load_chf(src)
    assert doc.version == version
    out = tmp_path / src.name
    save_chf(doc, out, version=doc.version)
    assert out.read_bytes() == src.read_bytes()


def test_chf_generated_samples_are_current():
    """tests/data/chf is what tools/make_test_chf.py writes (deterministic)."""
    pytest.importorskip("uharfbuzz")              # the text graph is shaped with the bundled font
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_test_chf", Path(__file__).parents[1] / "tools" / "make_test_chf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, b in mod.build().items():
        assert (DATA / "chf" / name).read_bytes() == b, name


def test_chf_sample_v9_structure():
    doc = load_chf(DATA / "chf" / "sample_v9.chf")
    assert doc.version == 9
    kinds = [p.extra["chf_type"] for p in doc.parts]
    assert kinds == [chf.G_SCAN, chf.G_GROUP, chf.G_CONTOUR, chf.G_CONTOUR, chf.G_CONTOUR, chf.G_CONTOUR, chf.G_TEXT]
    p = doc.parts[0]
    scan = p.extra["chf"]
    assert len(scan.group.elems) == len(p.contours) == 270
    assert len(scan.paths) == 1 and scan.e0 == 4
    path = scan.paths[0]
    assert {g.kind for g in path.glys} == {chf.GLY_SEGMENT, chf.GLY_SPLINE}
    sp = next(g for g in path.glys if g.kind == chf.GLY_SPLINE)
    assert len(sp.data["knots"]) == len(sp.data["ctrl"]) + 4      # cubic, FUN_10097450 check
    assert path.pwm_mode == 3 and len(path.pwm_switch) == 2 * len(p.contours)
    assert {e.seg_kind for e in scan.group.elems} == {0, -1066022767, 1048576, -1, 327680000}
    c0 = p.contours[0]
    assert isinstance(c0.segments[0], Line) and not c0.closed
    assert p.craft.layer == 4 and p.craft.kerf_comp == -1
    # the plate: outer contour + holes with their own craft
    plate = doc.parts[1]
    assert plate.craft.kerf_comp == 1 and plate.craft.lead_in.kind == 1 and len(plate.craft.micro_joints) == 2
    assert plate.craft.overcut == pytest.approx(0.5) and plate.craft.start_param == pytest.approx(0.125)
    assert plate.contours[1].craft.lead_in.kind == 2 and plate.contours[1].craft.kerf_comp == 0
    glyph_kinds = {g.kind for part in doc.parts[1:6] for c in part.contours for g in c.source_glyphs() or []}
    assert glyph_kinds >= {chf.GLY_SEGMENT, chf.GLY_ARC, chf.GLY_CIRCLE, chf.GLY_ELLIPSE, chf.GLY_LWPOLY,
                           chf.GLY_SPLINE}
    assert doc.parts[2].craft.lead_in.at_corner and doc.parts[4].craft.overcut < 0
    mark = doc.parts[5].extra["chf"]
    assert mark.vision and len(mark.seg_table) == 2 and len(mark.glys[0].data["tabs"]) == 2
    text = doc.parts[6].extra["chf"]
    assert text.text == "MLASER" and len(text.group.elems) == len(doc.parts[6].contours) >= 6
    assert doc.extra["chf"]["vision_graphs"] == [5]
    assert doc.extra["chf"]["rotated"] is False


@requires("File/autosave.chf")
def test_chf_vendor_autosave_structure():
    doc = load_chf(vendor_file("File/autosave.chf"))
    assert len(doc.parts) == 1
    p = doc.parts[0]
    assert p.extra["chf_type"] == chf.G_SCAN
    scan = p.extra["chf"]
    assert len(scan.group.elems) == 839 and len(p.contours) == 839
    assert len(scan.paths) == 1 and scan.e0 == 4
    path = scan.paths[0]
    assert len(path.glys) == 2077
    assert {g.kind for g in path.glys} == {chf.GLY_SEGMENT, chf.GLY_SPLINE}
    assert path.pwm_mode == 3 and len(path.pwm_switch) == 1678
    c0 = p.contours[0]
    assert isinstance(c0.segments[0], Line)
    assert math.isclose(c0.length(), 20.0)
    assert p.craft.layer == 6 and p.craft.kerf_comp == -1
    assert doc.extra["chf"]["vision_graphs"] == []


def test_chf_sample_v5_circle():
    doc = load_chf(DATA / "chf" / "sample_v5.chf")
    p = doc.parts[0]
    assert p.extra["chf_type"] == chf.G_CONTOUR
    (seg,) = p.contours[0].segments
    assert isinstance(seg, Arc) and math.dist(seg.start, seg.end) < 1e-12
    assert math.isclose(seg.radius, 47.5)
    assert p.contours[0].closed
    rec = p.extra["chf"]
    assert rec.tol == 0.1
    assert math.isclose(rec.length, 2 * math.pi * 47.5, rel_tol=1e-6)
    assert rec.lead_in.length == 5.0                     # v5 lead block has no lead-out
    assert [q.extra["chf_type"] for q in doc.parts] == [chf.G_CONTOUR, chf.G_GROUP, chf.G_CONTOUR]


@requires("File/Temp/tempGraph.chf")
def test_chf_vendor_tempgraph_circle():
    doc = load_chf(vendor_file("File/Temp/tempGraph.chf"))
    p = doc.parts[0]
    (seg,) = p.contours[0].segments
    assert isinstance(seg, Arc) and math.isclose(seg.radius, 47.422977)
    assert p.extra["chf"].lead_in.length == 5.0


@pytest.mark.parametrize("src", [pytest.param(DATA / "chf" / "sample_v5.chf", id="sample_v5"),
                                 pytest.param("File/Temp/tempGraph.chf", id="vendor:tempGraph",
                                              marks=requires("File/Temp/tempGraph.chf"))])
def test_chf_version_conversion(tmp_path, src):
    src = _src(src)
    d5 = load_chf(src)
    b9 = dumps_chf(d5, 9)
    d9 = loads_chf(b9)
    assert d9.version == 9
    assert [c.segments for p in d9.parts for c in p.contours] == [c.segments for p in d5.parts for c in p.contours]
    assert b"<Vision Feature>" in b9 and b"<Begin Vision>" in b9
    # and back down to 5 is identical to the original
    assert dumps_chf(d9, 5) == src.read_bytes()


def test_chf_edit_craft_and_geometry():
    doc = load_chf(DATA / "chf" / "sample_v5.chf")
    p = doc.parts[0]
    p.craft.layer = 3
    p.craft.kerf_width = 0.15
    p.contours[0].segments = [Line((0, 0), (10, 0)), Line((10, 0), (10, 10)),
                              Arc((10, 10), (0, 0), (5, 5), ccw=True)]
    back = loads_chf(dumps_chf(doc, 9))
    q = back.parts[0]
    assert q.craft.layer == 3 and math.isclose(q.craft.kerf_width, 0.15)
    segs = q.contours[0].segments
    assert len(segs) == 3 and isinstance(segs[2], Arc)
    assert math.dist(segs[2].end, (0, 0)) < 1e-5 and segs[2].ccw


def test_chf_new_document(tmp_path):
    sq = Contour([Line((0, 0), (5, 0)), Line((5, 0), (5, 5)), Line((5, 5), (0, 5)), Line((0, 5), (0, 0))])
    circ = Contour([Arc((12, 2), (12, 2), (10, 2), ccw=False)])
    doc = Document(parts=[Part(contours=[sq]), Part(contours=[sq, circ])])
    out = tmp_path / "new.chf"
    save_chf(doc, out)
    back = load_chf(out)
    assert [p.extra["chf_type"] for p in back.parts] == [chf.G_CONTOUR, chf.G_GROUP]
    assert math.isclose(back.parts[0].contours[0].length(), 20.0)
    c = back.parts[1].contours[1].segments[0]
    assert isinstance(c, Arc) and not c.ccw and math.isclose(c.radius, 2.0)
    # stable after a second round trip
    assert dumps_chf(load_chf(out)) == out.read_bytes()


# ------------------------------------------------------------------ small scFlie files
# tests/data/appdir mirrors the install folder layout with files written for these tests
SC_FILES = ["File/AutosaveParam1.ini", "File/AutosaveParam2.ini", "File/ManuContour.dat",
            "File/PithCompensate.pcf", "File/Temp/AutosaveParam1.ini", "File/Temp/AutosaveParam2.ini",
            "File/Temp/ManuContour.dat", "File/Temp/tempIsBreak.ini"]


@pytest.mark.parametrize("src", [pytest.param(APP / r, id=r) for r in SC_FILES]
                         + [pytest.param(r, id="vendor:" + r, marks=requires(r)) for r in SC_FILES])
def test_scfile_generic_roundtrip(src):
    src = _src(src)
    vals = scfile.read_values(src)
    assert scfile.write_values([v if "." in v else int(v) for v in vals]) == src.read_bytes()


def test_scfile_typed():
    a1 = scfile.AutosaveParam.load(APP / "File/AutosaveParam1.ini")
    assert (a1.counter, a1.x_um, a1.y_um, a1.graph_index, a1.path_index) == (120, -152340, 48715, 3, 812)
    assert a1.x == -152.34
    assert a1.dumps() == (APP / "File/AutosaveParam1.ini").read_bytes()
    assert scfile.newest_autosave(APP / "File").counter == 121
    assert scfile.newest_autosave(APP / "File/Temp").counter == 8          # file 1 is the newer one
    mc = scfile.ManuContour.load(APP / "File/Temp/ManuContour.dat")
    assert mc.values == [4, 7] and mc.dumps() == (APP / "File/Temp/ManuContour.dat").read_bytes()
    tb = scfile.TempIsBreak.load(APP / "File/Temp/tempIsBreak.ini")
    assert (tb.is_break, tb.laser_type) == (0, 1)
    assert tb.dumps() == (APP / "File/Temp/tempIsBreak.ini").read_bytes()
    pc = scfile.PitchCompensation.load(APP / "File/PithCompensate.pcf")
    assert pc.count == 2 and len(pc.raw) == 12              # rows kept verbatim (layout UNKNOWN)
    assert pc.dumps() == (APP / "File/PithCompensate.pcf").read_bytes()


@requires("File/AutosaveParam1.ini")
def test_scfile_typed_vendor():
    a1 = scfile.AutosaveParam.load(vendor_file("File/AutosaveParam1.ini"))
    assert (a1.counter, a1.x_um, a1.y_um) == (1974, -199773, -249421)
    assert scfile.newest_autosave(vendor_file("File")).counter == 1975
    mc = scfile.ManuContour.load(vendor_file("File/Temp/ManuContour.dat"))
    assert mc.values == [0, 0] and mc.dumps() == vendor_file("File/Temp/ManuContour.dat").read_bytes()
    tb = scfile.TempIsBreak.load(vendor_file("File/Temp/tempIsBreak.ini"))
    assert (tb.is_break, tb.laser_type) == (1, 1)
    pc = scfile.PitchCompensation.load(vendor_file("File/PithCompensate.pcf"))
    assert pc.count == 0 and pc.dumps() == vendor_file("File/PithCompensate.pcf").read_bytes()


def test_ini_files(tmp_path):
    sp = scfile.SoftPara.load(APP / "File/softPara.ini")
    assert sp.normal_exit == 0 and (sp.x_um, sp.y_um, sp.z_um) == (-120500, -80250, 1500)
    sp.save(tmp_path / "s.ini")
    assert (tmp_path / "s.ini").read_bytes() == (APP / "File/softPara.ini").read_bytes()
    j = scfile.JumpAddTime.load(APP / "JumpAddTime.txt")         # LF line ends, no final newline
    assert (j.add_time_ms, j.is_4freq, j.limit_small_circle, j.slow_ratio, j.k_x, j.k_y) == (150, 1, 1, 2, 90, 110)
    lf = scfile.read_ini(APP / "LanFormatEStr.txt")
    assert lf["SC"]["Var1"] == "A250421_0" and len(lf["SC"]) == 5
    rows = scfile.read_calib_csv(APP / "Help/calib.csv")
    assert rows[0] == (405120, 0) and rows[3] == (100, 401633) and len(rows) == 31
    scfile.write_calib_csv(rows, tmp_path / "c.csv")
    assert (tmp_path / "c.csv").read_bytes() == (APP / "Help/calib.csv").read_bytes()


@requires("File/softPara.ini")
def test_ini_files_vendor(tmp_path):
    sp = scfile.SoftPara.load(vendor_file("File/softPara.ini"))
    assert sp.normal_exit == 1 and sp.x_um == -335447
    sp.save(tmp_path / "s.ini")
    assert (tmp_path / "s.ini").read_bytes() == vendor_file("File/softPara.ini").read_bytes()
    j = scfile.JumpAddTime.load(vendor_file("JumpAddTime.txt"))
    assert j.add_time_ms == 200 and j.k_x == 100 and j.slow_ratio == 3
    assert scfile.read_ini(vendor_file("LanFormatEStr.txt"))["SC"]["Var1"] == "A260119_0"
    rows = scfile.read_calib_csv(vendor_file("Help/calib.csv"))
    assert rows[0] == (413023, 0) and rows[3] == (101, 410429)
    scfile.write_calib_csv(rows, tmp_path / "c.csv")
    assert (tmp_path / "c.csv").read_bytes() == vendor_file("Help/calib.csv").read_bytes()


# ------------------------------------------------------------------ lang
def test_translator():
    # tests/data/lang_fixture is a small synthetic fixture in the original's file format (not vendor text)
    t = Translator(DATA / "lang_fixture")
    assert t.languages[:3] == ["简体中文", "English", "French"]
    assert t.current == 0                       # lang.ini: Lang=简体中文 (GBK in a UTF-8 file)
    assert t.tr("FileFilter_CraftFile", "English") == "Process file"
    assert t.tr("FileFilter_CraftFile", 0) == "工艺文件"
    assert t.tr("FileFilter_CraftFile", "French") == "Fichier de procédé"
    assert t.tr("0_SongFont", "English") == "Arial"
    assert t.tr("dup", "English") == "first"    # first occurrence wins
    assert t.tr("short", 0) == "short"          # lines with < 3 fields are ignored in lang.txt
    # fallback: missing in French -> English -> key
    k = next(k for k in t.maps[1] if k not in t.maps[2])
    assert t.tr(k, "French") == t.maps[1][k]
    assert t.tr("no_such_key", "French") == "no_such_key"


def test_translator_save_ini(tmp_path):
    shutil.copytree(DATA / "lang_fixture", tmp_path / "Lang")
    t = Translator(tmp_path / "Lang")
    t.set_language("English")
    t.save_ini()
    assert Translator(tmp_path / "Lang").current == 1


# ------------------------------------------------------------------ params
def test_param_table():
    defs = param_defs()
    assert len(defs) == 1129
    by = {(d.table, d.key): d for d in defs}
    cs = by[("layer", "GP.CutSpeed")]
    assert (cs.default, cs.unit, cs.min, cs.max, cs.type_name) == ("100", "mm/s", 0.01, 1200.0, "double")
    assert cs.label_en == "Cut basic.Cut speed"     # from mlaser/i18n/en.json, not the data file
    assert len(layer_defs()) == 164 and len(layer_defs(True)) == 22
    gas = by[("layer", "GP.CutGasType")]
    assert [o[1] for o in gas.options] == ["Air (CO2)", "O2 (fiber)", "N2 / air (fiber)"]
    assert all(o[0] for o in gas.options)                          # label keys kept as facts


def test_param_store_roundtrip(tmp_path):
    m = MachineParams.defaults()
    lp = m.layer_params(2)
    assert lp.speed == 100.0 and lp.frequency_hz == 5000.0 and lp.cut_height == 1.0
    assert len(lp.pierce["stages"]) == 5
    lp.speed = 33.5
    lp.gas = 2
    lp.pierce["stages"][0]["DrillHeight"] = 7.25
    m.set_layer_params(lp)
    m.save_dir(tmp_path)
    assert (tmp_path / "LayerPara.xml").read_bytes().startswith(b"<?xml")
    assert b"<PLayerParam3>" in (tmp_path / "LayerPara.xml").read_bytes()
    m2 = MachineParams.load_dir(tmp_path)
    lp2 = m2.layer_params(2)
    assert lp2.speed == 33.5 and lp2.gas == 2 and lp2.pierce["stages"][0]["DrillHeight"] == 7.25
    assert m2.layer.values == m.layer.values and m2.hard.values == m.hard.values
    layers = load_layer_params(tmp_path / "LayerPara.xml")
    assert layers[2].speed == 33.5 and layers[0].speed == 100.0
    save_layer_params(layers, tmp_path / "craft.xml")
    assert load_layer_params(tmp_path / "craft.xml")[2].speed == 33.5


def test_param_store_xml_shape():
    st = ParamStore.loads(b'<ParameterRoot><PLayerParam1><GP CutSpeed="12.5"/></PLayerParam1></ParameterRoot>')
    assert st.get_raw(layer_category(0), "GP.CutSpeed") == "12.5"
