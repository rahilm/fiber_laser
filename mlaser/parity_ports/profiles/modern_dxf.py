"""``"modern-dxf"``: ORIGINAL with the DXF import's entity collection on ezdxf
(:mod:`mlaser.io.dxf_modern`): HATCH boundaries, DIMENSION/LEADER geometry, ATTRIB, MINSERT
grids, full INSERT transforms, TEXT/MTEXT through fontTools + uharfbuzz or SHX fonts.  Chaining,
contour building, sorting and every other slot stay the original ones.

    from mlaser.parity_ports import use_profile
    import mlaser.parity_ports.profiles.modern_dxf  # registers the profile
    with use_profile("modern-dxf"):
        doc = import_dxf(path)            # same as import_dxf(path, mode="modern")
"""
from __future__ import annotations

from ..profile import ORIGINAL, PROFILES, register_profile

NAME = "modern-dxf"
_M = "mlaser.io.dxf_modern"

MODERN_DXF = PROFILES.get(NAME) or register_profile(ORIGINAL.derive(
    NAME,
    "DXF import on ezdxf (HATCH, DIMENSION, ATTRIB, MINSERT, SHX/TrueType text); not parity",
    dxf=ORIGINAL.dxf.with_(collect=f"{_M}:collect", entity_glyphs=f"{_M}:entity_glyphs"),
))
