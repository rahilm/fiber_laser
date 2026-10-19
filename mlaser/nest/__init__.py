"""Nesting (排样): data model, true-shape nester, result application and project files.

See docs/re/nest.md for what the original (SmartNest.dll / AutoNest.dll driven from MainApp /
CADModule) does and how this package maps onto it. ``NestParams.engine`` selects the engine:
the built-in raster nester (default) or the optional sparrow engine (``sparrow_engine``).
"""
from .model import (ENGINE_RASTER, ENGINE_SPYRROW, FIT_RECT, FIT_SHAPE, GRAVITY_AUTO, GRAVITY_BOTTOM,
                    GRAVITY_LEFT, GRAVITY_RIGHT, GRAVITY_TOP, NestJob, NestParams, NestPart, NestResult,
                    NestSheet, Placement, SheetLayout)
from .engine import NestEngineUnavailable, Nester, available_engines, engine_class, nest
from .apply import apply_result, job_from_document, layout_parts, stats

__all__ = ["ENGINE_RASTER", "ENGINE_SPYRROW", "FIT_RECT", "FIT_SHAPE", "GRAVITY_AUTO", "GRAVITY_BOTTOM",
           "GRAVITY_LEFT", "GRAVITY_RIGHT", "GRAVITY_TOP",
           "NestJob", "NestParams", "NestPart", "NestResult", "NestSheet", "Placement", "SheetLayout",
           "Nester", "NestEngineUnavailable", "available_engines", "engine_class", "nest",
           "apply_result", "job_from_document", "layout_parts", "stats"]
