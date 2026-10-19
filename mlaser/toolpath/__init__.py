"""Shared toolpath vocabulary: the MotionItem types that CAM produces and motion, NC, the
G-code writer and the UI consume (docs/re/cam.md, "MotionItem stream").

* :mod:`.items`  Rapid, CutLine, CutArc, Pierce, LaserOn/Off, Delay, IO, Follow, Marker ...

Foundation layer: pure data, imports nothing else from mlaser (docs/dev/ARCHITECTURE.md).
"""
