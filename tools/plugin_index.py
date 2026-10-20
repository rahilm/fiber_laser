#!/usr/bin/env python3
"""Build, update or check an mlaser plugin index from wheels (docs/dev/PLUGIN_STANDARD.md §6).

    python tools/plugin_index.py build plugins/dist/*.whl -o mlaser-plugin-index.json --sha256sums SHA256SUMS
    python tools/plugin_index.py check mlaser-plugin-index.json plugins/mlaser-onshape/mlaser_onshape/mlaser-plugin.toml

The implementation is :mod:`mlaser.services.plugin_index` (also ``python -m
mlaser.services.plugin_index``, which is what a plugin repository's CI runs).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from mlaser.services.plugin_index import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
