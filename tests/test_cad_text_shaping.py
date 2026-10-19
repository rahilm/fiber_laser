"""Draw -> Text and the DXF TEXT fallback through fontTools + uharfbuzz (no Qt)."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("uharfbuzz")

from mlaser.cad import fonts as F  # noqa: E402
from mlaser.cad import shaping as S  # noqa: E402
from mlaser.cad.text import resolve_family, text_contours  # noqa: E402

SC = F.bundled_font("Noto Sans SC")
AR = F.bundled_font("Noto Sans Arabic")


def _bbox(cs):
    xs = [p[0] for c in cs for p in c.points()]
    ys = [p[1] for c in cs for p in c.points()]
    return min(xs), min(ys), max(xs), max(ys)


def test_bundled_fonts_and_licences_present():
    assert SC is not None and AR is not None
    for fn in ("OFL-NotoSansCJK.txt", "OFL-NotoSansArabic.txt"):
        txt = open(os.path.join(F.BUNDLED_DIR, fn), encoding="utf-8").read()
        assert "SIL OPEN FONT LICENSE Version 1.1" in txt
    assert os.path.getsize(SC.path) < 3_000_000


def test_latin_contours_closed_at_origin():
    cs = text_contours("HI", "Noto Sans SC", 10.0, (5, 5))
    assert len(cs) == 2 and all(c.closed for c in cs)
    x0, y0, _x1, y1 = _bbox(cs)
    assert (x0, y0) == pytest.approx((5, 5))
    assert 3 < y1 - y0 <= 10.0
    assert all(c.extra["text"] == "HI" for c in cs)
    assert text_contours("", "Noto Sans SC", 10) == []


def test_cjk_outlines():
    cs = text_contours("中文", "Noto Sans SC", 10.0)
    assert len(cs) >= 6 and all(c.closed for c in cs)
    _x0, _y0, x1, y1 = _bbox(cs)
    em = 10.0 / 1.448                              # height = cell (winAscent + winDescent)
    assert 1.7 * em < x1 < 2.0 * em and 0.8 * em < y1 <= 1.0 * em   # two full-width ideographs


def test_missing_glyphs_fall_back_to_the_bundled_font():
    # the Arabic subset has no hanzi: they come from the Noto Sans SC subset
    line = S.shape_line("a中", S.font_chain(AR))
    fams = [g.font.ref.family for g in line.glyphs]
    assert fams[-1] == "Noto Sans SC"
    assert text_contours("中", AR.path, 10.0)


def test_arabic_joining_forms_differ_from_isolated():
    chain = S.font_chain(AR)
    iso = {g.gid for g in S.shape_line("ب", chain).glyphs}
    joined = [g.gid for g in S.shape_line("ببب", chain).glyphs]
    # three different base forms (initial, medial, final), none of them the isolated one
    bases = set(joined) - iso
    assert len(bases) >= 3
    cs = text_contours("سلام", AR.path, 10.0)
    assert cs and all(c.closed for c in cs)


def test_rtl_runs_in_visual_order():
    chain = S.font_chain(None)
    g = S.shape_line("ab سل", chain).glyphs            # LTR paragraph: Latin left
    x_of = {gl.cluster: gl.x for gl in g}
    assert x_of[0] < x_of[1] < x_of[4] < x_of[3]       # Arabic right of Latin, right to left
    g = S.shape_line("سل ab", chain).glyphs            # RTL paragraph: Latin on the left
    x_of = {gl.cluster: gl.x for gl in g}
    assert x_of[3] < x_of[4] < x_of[1] < x_of[0]


def test_kerning_pair_and_ligature():
    chain = S.font_chain(SC)
    kern = S.shape_line("AV", chain).advance
    nokern = S.shape_line("AV", chain, {"kern": False}).advance
    assert kern < nokern                              # GPOS kern pulls A and V together
    assert len(S.shape_line("fi", chain).glyphs) == 1  # liga
    assert len(S.shape_line("fi", chain, {"liga": False}).glyphs) == 2


def test_multiline_and_styles():
    one = _bbox(text_contours("ab", "Noto Sans SC", 10.0))
    two = _bbox(text_contours("ab\nab", "Noto Sans SC", 10.0))
    assert two[3] - two[1] > 1.8 * (one[3] - one[1])
    it = _bbox(text_contours("l", "Noto Sans SC", 10.0, italic=True))
    up = _bbox(text_contours("l", "Noto Sans SC", 10.0))
    assert it[2] - it[0] > up[2] - up[0]              # synthetic slant
    bd = _bbox(text_contours("l", "Noto Sans SC", 10.0, bold=True))
    assert bd[2] - bd[0] > up[2] - up[0]              # synthetic bold


def test_lookup_order(tmp_path):
    # explicit file first
    r = F.locate("SimSun", font_file=AR.path)
    assert r is not None and r.path == AR.path
    # an unknown family is not found; resolve_family falls back (installed or bundled)
    assert F.locate("No Such Family 123") is None
    fam = resolve_family("No Such Family 123")
    assert fam == F.BUNDLED_FAMILY                    # default mode: never an installed face
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MLASER_TEXT_FONT", "system")       # the original's lookup chain
        assert resolve_family("No Such Family 123") in F.FALLBACK_FAMILIES + (F.BUNDLED_FAMILY,)
    # bundled family by name, also through text_contours
    assert F.locate("noto sans arabic").path == AR.path
    assert "Noto Sans SC" in F.available_families()


def test_dxf_text_fallback_uses_shaped_outline(tmp_path, monkeypatch):
    ezdxf = pytest.importorskip("ezdxf")
    from mlaser.io import dxf_import
    d = ezdxf.new("R2010")
    d.modelspace().add_text("Ag 中文", dxfattribs={"height": 5.0, "insert": (0, 0)})
    p = str(tmp_path / "t.dxf")
    d.saveas(p)
    monkeypatch.setattr(dxf_import, "_tt_font", lambda fam, **kw: None)   # no outline file
    doc = dxf_import.import_dxf(p, placement=None)
    grp = [pt for pt in doc.parts if pt.extra.get("dxf_type") == "TEXT"]
    assert len(grp) == 1 and len(grp[0].contours) >= 8
    ys = [y for c in grp[0].contours for s in c.segments for y in (s.start[1], s.end[1])]
    assert max(ys) - min(ys) == pytest.approx(5.0, abs=1e-6)


def test_imports_without_pyside6(tmp_path):
    code = textwrap.dedent(f"""
        import sys
        sys.modules["PySide6"] = None          # any "import PySide6..." raises ImportError
        import ezdxf
        import mlaser.cad.text as T
        import mlaser.io.dxf_import as D
        cs = T.text_contours("Hi 中文", "Noto Sans SC", 10.0)
        assert cs, "no outline"
        d = ezdxf.new("R2010")
        d.modelspace().add_text("Hi", dxfattribs={{"height": 5.0}})
        d.saveas({str(tmp_path / 'x.dxf')!r})
        doc = D.import_dxf({str(tmp_path / 'x.dxf')!r})
        assert any(p.name == "text" for p in doc.parts)
        assert not any(m.startswith("PySide6.") for m in sys.modules)
        print("ok")
    """)
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                       cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("ok")
