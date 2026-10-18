"""One geometry library: segment/glyph/spline flattening, bounding boxes and arc-length contour
operations (docs/dev/ARCHITECTURE.md).

* :mod:`.curves`   bulge arcs, splines (evaluation, flattening, derivatives, length), glyph
  expansion, segment chaining, contour area and reversal, bounding boxes (was ``io.geom``)
* :mod:`.contour`  arc-length parametrisation of contours: points, tangents, sub-paths, start
  rotation, orientation, point-in-polygon, polyline bbox (was ``cam.geometry``)

Foundation layer: imports only :mod:`mlaser.model`.  Several flattenings with different
tolerances still exist (``Contour.points``, ``contour.polyline`` 0.02 mm, ...); they are kept as
they are because the goldens pin their output, and new code should use the ones here.
"""
from .contour import (contour_bbox, is_ccw, locate, point_at, point_in_contour, polyline, rot,  # noqa: F401
                      rotate_start, seg_point, seg_tangent, split_seg, stored_length, sub_path,
                      sub_seg, tangent_at, turn)
from .curves import (SPLINE_MAX_ANGLE, SPLINE_MAX_CHORD, SPLINE_SPLIT_ANGLE, SPLINE_TOL,  # noqa: F401
                     bspline_derivative, bspline_length, bspline_point, bulge_to_arc,
                     ellipse_points, flatten_bspline, flatten_bspline_params, glyph_endpoints,
                     glyph_length, glyph_segments, join_segments, lwpoly_segments,
                     polyline_segments, reverse_contour, segments_bbox, seg_reversed,
                     signed_area, translate_glyph)
