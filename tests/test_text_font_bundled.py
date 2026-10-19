"""Cutting text defaults to the bundled Noto Sans SC subset (machine-independent geometry);
``MLASER_TEXT_FONT=system`` restores the original's installed-font lookup (SimSun / WenQuanYi)."""
import pytest

ezdxf = pytest.importorskip("ezdxf")

from mlaser.cad import fonts as F  # noqa: E402
from mlaser.io import dxf_import  # noqa: E402

pytestmark = pytest.mark.skipif(F.bundled_font() is None, reason="bundled fonts not present")


def _text_dxf(tmp_path, text="Ag8& 中文"):
    d = ezdxf.new("R2010")
    d.modelspace().add_text(text, dxfattribs={"height": 5.0, "insert": (1.0, 2.0)})
    p = str(tmp_path / "t.dxf")
    d.saveas(p)
    return p


def _geometry(doc):
    return [[(s.start, s.end) for s in c.segments] for p in doc.parts for c in p.contours]


def _hide_installed_fonts(mp):
    mp.setattr(F, "_system_files", lambda: [])
    mp.setattr(F, "_FONT_FILES", {})
    F._faces.cache_clear()


def test_defaults_are_bundled(monkeypatch):
    monkeypatch.delenv("MLASER_TEXT_FONT", raising=False)
    from mlaser.cad.text import DEFAULT_FONT, resolve_family
    assert DEFAULT_FONT == F.BUNDLED_FAMILY
    assert F.text_font_mode() == "bundled"
    assert dxf_import._text_family() == F.BUNDLED_FAMILY
    assert resolve_family(None) == F.BUNDLED_FAMILY
    assert resolve_family("No Such Family 123") == F.BUNDLED_FAMILY
    # a bundled family name is the bundled file, even when a full copy is installed
    assert F.locate(F.BUNDLED_FAMILY).bundled
    assert F.text_font_file(F.BUNDLED_FAMILY)[0] == F.bundled_font().path


def test_draw_text_default_font():
    from mlaser.ui.ext_draw import TextParams
    assert TextParams().font == F.BUNDLED_FAMILY


def test_dxf_text_independent_of_installed_fonts(tmp_path, monkeypatch):
    monkeypatch.delenv("MLASER_TEXT_FONT", raising=False)
    p = _text_dxf(tmp_path)
    a = dxf_import.import_dxf(p, placement=None)
    with pytest.MonkeyPatch.context() as mp:
        _hide_installed_fonts(mp)
        b = dxf_import.import_dxf(p, placement=None)
    F._faces.cache_clear()
    ga, gb = _geometry(a), _geometry(b)
    assert ga and ga == gb
    # outline records of the CFF font: cubic spline glyphs as for a TrueType face
    grp = [pt for pt in a.parts if pt.extra.get("dxf_type") == "TEXT"][0]
    assert any(g.kind == 7 for c in grp.contours for g in (c.source_glyphs() or []))
    ys = [y for c in grp.contours for s in c.segments for y in (s.start[1], s.end[1])]
    assert max(ys) - min(ys) == pytest.approx(5.0, abs=1e-6)


def test_modern_dxf_text_default_is_bundled(tmp_path, monkeypatch):
    monkeypatch.delenv("MLASER_TEXT_FONT", raising=False)
    p = _text_dxf(tmp_path, "HI")
    a = dxf_import.import_dxf(p, placement=None, mode="modern")
    with pytest.MonkeyPatch.context() as mp:
        _hide_installed_fonts(mp)
        b = dxf_import.import_dxf(p, placement=None, mode="modern")
    F._faces.cache_clear()
    assert _geometry(a) and _geometry(a) == _geometry(b)


def test_system_mode_is_the_original_lookup(monkeypatch):
    monkeypatch.setenv("MLASER_TEXT_FONT", "system")
    assert F.text_font_mode() == "system"
    fam = dxf_import._text_family()
    installed = [f for f in dxf_import.TEXT_FAMILIES if F.find_font_file(f) is not None]
    if installed:
        assert fam == installed[0]
    else:
        assert fam in F.FALLBACK_FAMILIES + (F.BUNDLED_FAMILY,)


def test_profile_option_selects_mode(monkeypatch):
    monkeypatch.delenv("MLASER_TEXT_FONT", raising=False)
    from mlaser.parity_ports import ORIGINAL, use_profile
    prof = ORIGINAL.derive("test-system-fonts", "test", options={"text_font": "system"})
    with use_profile(prof):
        assert F.text_font_mode() == "system"
    assert F.text_font_mode() == "bundled"
