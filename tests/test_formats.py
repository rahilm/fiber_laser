"""Round-trip and semantics tests for mlaser.formats (samples copied from the original install)."""
from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from mlaser.model import Arc, Contour, Document, Line, Part
from mlaser.formats import chf, scfile
from mlaser.formats.chf import dumps_chf, load_chf, loads_chf, save_chf
from mlaser.formats.lang import Translator
from mlaser.formats.params import (MachineParams, ParamStore, layer_category, layer_defs,
                                   load_layer_params, param_defs, save_layer_params)
from mlaser.formats.scio import fmt_double, split_lines

DATA = Path(__file__).parent / "data"


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
@pytest.mark.parametrize("name,version", [("autosave.chf", 9), ("tempGraph.chf", 5)])
def test_chf_roundtrip_bytes(tmp_path, name, version):
    src = DATA / name
    doc = load_chf(src)
    assert doc.version == version
    out = tmp_path / name
    save_chf(doc, out, version=doc.version)
    assert out.read_bytes() == src.read_bytes()


def test_chf_autosave_structure():
    doc = load_chf(DATA / "autosave.chf")
    assert len(doc.parts) == 1
    p = doc.parts[0]
    assert p.extra["chf_type"] == chf.G_SCAN
    scan = p.extra["chf"]
    assert len(scan.group.elems) == 839 and len(p.contours) == 839
    assert len(scan.paths) == 1 and scan.e0 == 4
    path = scan.paths[0]
    assert len(path.glys) == 2077
    kinds = {g.kind for g in path.glys}
    assert kinds == {chf.GLY_SEGMENT, chf.GLY_SPLINE}
    sp = next(g for g in path.glys if g.kind == chf.GLY_SPLINE)
    assert len(sp.data["knots"]) == len(sp.data["ctrl"]) + 4      # cubic, FUN_10097450 check
    assert path.pwm_mode == 3 and len(path.pwm_switch) == 1678
    # first element: a 20 mm line, layer 6
    c0 = p.contours[0]
    assert isinstance(c0.segments[0], Line)
    assert math.isclose(c0.length(), 20.0)
    assert p.craft.layer == 6 and p.craft.kerf_comp == -1
    assert doc.extra["chf"]["vision_graphs"] == []
    assert doc.extra["chf"]["rotated"] is False


def test_chf_tempgraph_circle():
    doc = load_chf(DATA / "tempGraph.chf")
    p = doc.parts[0]
    assert p.extra["chf_type"] == chf.G_CONTOUR
    (seg,) = p.contours[0].segments
    assert isinstance(seg, Arc) and math.dist(seg.start, seg.end) < 1e-12
    assert math.isclose(seg.radius, 47.422977)
    assert p.contours[0].closed
    rec = p.extra["chf"]
    assert math.isclose(rec.length, 2 * math.pi * 47.422977, rel_tol=1e-6)
    assert rec.lead_in.length == 5.0                     # v5 lead block has no lead-out


def test_chf_version_conversion(tmp_path):
    d5 = load_chf(DATA / "tempGraph.chf")
    b9 = dumps_chf(d5, 9)
    d9 = loads_chf(b9)
    assert d9.version == 9
    assert d9.parts[0].contours[0].segments == d5.parts[0].contours[0].segments
    assert b"<Vision Feature>" in b9 and b"<Begin Vision>" in b9
    # and back down to 5 is identical to the original
    assert dumps_chf(d9, 5) == (DATA / "tempGraph.chf").read_bytes()


def test_chf_edit_craft_and_geometry():
    doc = load_chf(DATA / "tempGraph.chf")
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
@pytest.mark.parametrize("rel", ["AutosaveParam1.ini", "AutosaveParam2.ini", "ManuContour.dat",
                                 "PithCompensate.pcf", "Temp/AutosaveParam1.ini",
                                 "Temp/ManuContour.dat", "Temp/tempIsBreak.ini"])
def test_scfile_generic_roundtrip(rel):
    src = DATA / rel
    vals = scfile.read_values(src)
    assert scfile.write_values([int(v) for v in vals]) == src.read_bytes()


def test_scfile_typed():
    a1 = scfile.AutosaveParam.load(DATA / "AutosaveParam1.ini")
    assert (a1.counter, a1.x_um, a1.y_um) == (1974, -199773, -249421)
    assert a1.dumps() == (DATA / "AutosaveParam1.ini").read_bytes()
    assert scfile.newest_autosave(DATA).counter == 1975
    mc = scfile.ManuContour.load(DATA / "Temp/ManuContour.dat")
    assert mc.values == [0, 0] and mc.dumps() == (DATA / "Temp/ManuContour.dat").read_bytes()
    tb = scfile.TempIsBreak.load(DATA / "Temp/tempIsBreak.ini")
    assert (tb.is_break, tb.laser_type) == (1, 1)
    assert tb.dumps() == (DATA / "Temp/tempIsBreak.ini").read_bytes()
    pc = scfile.PitchCompensation.load(DATA / "PithCompensate.pcf")
    assert pc.count == 0 and pc.dumps() == (DATA / "PithCompensate.pcf").read_bytes()


def test_ini_files(tmp_path):
    sp = scfile.SoftPara.load(DATA / "softPara.ini")
    assert sp.normal_exit == 1 and sp.x_um == -335447
    sp.save(tmp_path / "s.ini")
    assert (tmp_path / "s.ini").read_bytes() == (DATA / "softPara.ini").read_bytes()
    j = scfile.JumpAddTime.load(DATA / "JumpAddTime.txt")
    assert j.add_time_ms == 200 and j.k_x == 100 and j.slow_ratio == 3
    lf = scfile.read_ini(DATA / "LanFormatEStr.txt")
    assert lf["SC"]["Var1"] == "A260119_0"
    rows = scfile.read_calib_csv(DATA / "Help/calib.csv")
    assert rows[0] == (413023, 0) and rows[3] == (101, 410429)
    scfile.write_calib_csv(rows, tmp_path / "c.csv")
    assert (tmp_path / "c.csv").read_bytes() == (DATA / "Help/calib.csv").read_bytes()


# ------------------------------------------------------------------ lang
def test_translator():
    # tests/data/Lang is a small synthetic fixture in the original's file format (not vendor text)
    t = Translator(DATA / "Lang")
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
    shutil.copytree(DATA / "Lang", tmp_path / "Lang")
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
    assert cs.label_en == "Cut Basic.Cut Speed"     # from mlaser/i18n/en.json, not the data file
    assert len(layer_defs()) == 164 and len(layer_defs(True)) == 22
    gas = by[("layer", "GP.CutGasType")]
    assert [o[1] for o in gas.options] == ["Air(CO2)", "O2(Fiber)", "N2/Air(Fiber)"]
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
