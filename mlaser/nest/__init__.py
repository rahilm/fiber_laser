"""Nesting (排样): data model, true-shape nester, result application and project files.

See docs/nest.md for what the original (SmartNest.dll / AutoNest.dll driven from MainApp /
CADModule) does and how this package maps onto it.
"""
from .model import (FIT_RECT, FIT_SHAPE, GRAVITY_AUTO, GRAVITY_BOTTOM, GRAVITY_LEFT, GRAVITY_RIGHT, GRAVITY_TOP,
                    NestJob, NestParams, NestPart, NestResult, NestSheet, Placement, SheetLayout)
from .engine import Nester, nest
from .apply import apply_result, job_from_document, layout_parts, stats

__all__ = ["FIT_RECT", "FIT_SHAPE", "GRAVITY_AUTO", "GRAVITY_BOTTOM", "GRAVITY_LEFT", "GRAVITY_RIGHT", "GRAVITY_TOP",
           "NestJob", "NestParams", "NestPart", "NestResult", "NestSheet", "Placement", "SheetLayout",
           "Nester", "nest", "apply_result", "job_from_document", "layout_parts", "stats"]
