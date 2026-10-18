"""Typed access to a group of original parameters (descriptor table, mlaser.formats.params).

The test / auxiliary dialogs of the original are BCGP property grids over parameter
descriptors (FUN_005ee240 builds them from a table id; the edit callback FUN_005689e0 checks
the descriptor min / max at +0x98 / +0xa0 and stores the value).  :class:`ParamSet` gives the
same view over the rebuild's ``window.settings`` dict (keys are the parameter names, e.g.
``"ST.Type"``, as MainWindow's options dialogs use).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable, Optional

from mlaser.formats.params import ParamDef, param_defs


@lru_cache(maxsize=None)
def find_def(key: str) -> Optional[ParamDef]:
    for d in param_defs():
        if d.key == key:
            return d
    return None


class ParamSet:
    """Values of ``keys`` backed by ``settings`` with the descriptor defaults."""

    def __init__(self, keys: Iterable[str], settings: Optional[dict] = None, overrides: Optional[dict] = None):
        self.keys = list(keys)
        self.settings = settings if settings is not None else {}
        self.overrides = dict(overrides or {})   # rebuild defaults where the original's are 0/U

    def definition(self, key: str) -> Optional[ParamDef]:
        return find_def(key)

    def default(self, key: str) -> Any:
        if key in self.overrides:
            return self.overrides[key]
        d = find_def(key)
        return d.default_value if d is not None else 0

    def get(self, key: str) -> Any:
        if key in self.settings:
            d = find_def(key)
            v = self.settings[key]
            if d is not None and isinstance(v, str):
                return d.parse(v)
            return v
        return self.default(key)

    def set(self, key: str, value: Any) -> None:
        d = find_def(key)
        if d is not None and d.type in (1, 2, 9) and d.max > d.min:
            value = min(max(value, d.min), d.max)
        self.settings[key] = value

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def as_dict(self) -> dict:
        return {k: self.get(k) for k in self.keys}
