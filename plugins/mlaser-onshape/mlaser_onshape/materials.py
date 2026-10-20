"""Material names: thickness written in the name, and the mapping to the technology database.

* :func:`label_thickness` reads a thickness from a material name: ``3mm``, ``6 mm``, ``1/8"``,
  ``1/4in``, ``0.125 in``, ``.25"``, ``3/16 inch``, ``1-1/2"``, ``0,8 mm``.  A bare number is not
  a thickness (``6061-T6`` is an alloy).
* :class:`MaterialMap` maps an Onshape material (the ``Material`` property of the BOM, or the
  part's ``BTPartMaterial`` name) to the material name of the technology database, which is
  also the sheet material Orders and Nest use.  Unmapped names get suggestions
  (:func:`suggest`): alloy and family words are normalised (aluminum / aluminium / 6061 / 5052,
  steel / S235 / A36, stainless / 304 / 1.4301 ...) and compared with difflib.
* :func:`non_metal` tells plastics and wood apart: a fiber laser does not cut them, so they are
  never mapped automatically.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

INCH = 25.4

_NUM = r"(?:\d+(?:[.,]\d+)?|[.,]\d+)"
_FRAC = r"(?<![\d.,/])(?:(?P<whole>\d{1,2})(?:-|\s+))?(?P<num>\d+)\s*/\s*(?P<den>\d+)"
_IN = r"(?:\"|''|″|”|\s*(?:in(?:ch(?:es)?)?\.?)(?![a-z]))"
_MM = r"\s*mm(?![a-z])"
_RE_FRAC_IN = re.compile(_FRAC + r"\s*" + _IN, re.I)
_RE_DEC_IN = re.compile(r"(?<![\d.,/])(?P<v>" + _NUM + r")\s*" + _IN, re.I)
_RE_MM = re.compile(r"(?<![\d.,/])(?P<v>" + _NUM + r")" + _MM, re.I)


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def label_thickness(text: str) -> Optional[float]:
    """The thickness (mm) written in a material name, or None."""
    t = str(text or "")
    m = _RE_FRAC_IN.search(t)
    if m:
        den = int(m["den"])
        if den:
            return round((int(m["whole"] or 0) + int(m["num"]) / den) * INCH, 4)
    m = _RE_DEC_IN.search(t)
    if m:
        return round(_f(m["v"]) * INCH, 4)
    m = _RE_MM.search(t)
    if m:
        return _f(m["v"])
    return None


def strip_thickness(text: str) -> str:
    """The material name without its thickness: ``Aluminum 1/8"`` -> ``Aluminum``."""
    t = str(text or "")
    for rx in (_RE_FRAC_IN, _RE_DEC_IN, _RE_MM):
        t = rx.sub(" ", t)
    return " ".join(t.replace("()", " ").split()).strip(" -,/")


# ============================================================================== families
FAMILIES: dict[str, tuple[str, ...]] = {
    "aluminium": ("aluminium", "aluminum", "alu", "al", "almg", "almg3", "5052", "5083", "5754", "6061",
                  "6063", "6082", "7075", "2024", "3003", "1100"),
    "stainless": ("stainless", "inox", "ss", "304", "316", "1.4301", "1.4404", "v2a", "v4a", "430"),
    "steel": ("steel", "mild", "s235", "s355", "dc01", "a36", "1018", "1020", "4130", "4140", "cold", "rolled",
              "crs", "hrs", "galvanized", "galvanised"),
    "copper": ("copper", "cu"),
    "brass": ("brass", "cuzn"),
    "titanium": ("titanium", "ti"),
}
NON_METALS = ("polycarbonate", "lexan", "acrylic", "pmma", "plexi", "hdpe", "uhmw", "delrin", "acetal", "pom",
              "nylon", "abs", "petg", "pla", "plywood", "wood", "mdf", "carbon fiber", "carbon fibre", "g10", "fr4",
              "garolite", "polycarb", "pvc", "foam", "rubber")


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9.]+", str(text or "").lower()) if t]


def family(text: str) -> str:
    toks = tokens(text)
    low = str(text or "").lower()
    # stainless before steel ("stainless steel"); a grade number decides the family too
    for fam in ("stainless", "aluminium", "brass", "copper", "titanium", "steel"):
        words = FAMILIES[fam]
        if any(t in words for t in toks) or any(len(w) > 3 and w in low for w in words):
            return fam
    return ""


def non_metal(text: str) -> bool:
    low = " " + str(text or "").lower() + " "
    return not family(text) and any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low) for w in NON_METALS)


def suggest(label: str, names: Iterable[str], n: int = 3) -> list[str]:
    """Technology material names that may mean ``label``, best first."""
    names = list(dict.fromkeys(names))
    if not names or not label:
        return []
    base = strip_thickness(label)
    fam = family(base)
    scored = []
    for nm in names:
        r = difflib.SequenceMatcher(None, base.lower(), nm.lower()).ratio()
        if fam and family(nm) == fam:
            r += 1.0
        elif fam and family(nm):
            r -= 1.0
        common = set(tokens(base)) & set(tokens(nm))
        r += 0.2 * len(common)
        scored.append((r, nm))
    scored.sort(key=lambda x: -x[0])
    return [nm for r, nm in scored if r > 0.45][:n]


@dataclass
class MaterialMap:
    """Onshape material name (case-insensitive, spaces collapsed) -> technology material."""

    table: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def norm(label: str) -> str:
        return " ".join(str(label or "").split()).casefold()

    def get(self, label: str) -> str:
        return self.table.get(self.norm(label), "")

    def set(self, label: str, material: str) -> None:
        k = self.norm(label)
        if material:
            self.table[k] = material
        else:
            self.table.pop(k, None)

    def resolve(self, label: str, names: Iterable[str]) -> tuple[str, str]:
        """(sheet material, how): the mapped name; else the database name equal to the label
        without its thickness; else (``""``, "unmapped")."""
        hit = self.get(label)
        if hit:
            return hit, "mapped"
        base = strip_thickness(label)
        for nm in names:
            if self.norm(nm) in (self.norm(base), self.norm(label)):
                return nm, "same name"
        return "", "unmapped"

    def to_json(self) -> dict:
        return dict(self.table)

    @classmethod
    def from_json(cls, d: object) -> "MaterialMap":
        if not isinstance(d, dict):
            return cls()
        return cls({cls.norm(k): str(v) for k, v in d.items() if isinstance(k, str) and isinstance(v, str) and v})
