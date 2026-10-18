"""Drawing and editing geometry (phase 3, agent F): the CAD half of CADModule.dll.

Pure Python on top of :mod:`mlaser.model` (no Qt except :mod:`mlaser.cad.text`, which uses
QtGui's font engine for glyph outlines).  Evidence for every function is cited in its
docstring and collected in ``docs/draw.md``.

Modules
-------
``primitives``  shapes made by the Draw tab tools (CCreate* classes, CADModule 0x1007e6d0..0x10083ce0)
``intersect``   segment intersections and contour parameterisation
``edit``        explode / merge / split / trim / extend (CADModule 0x100b7ce0, 0x100bf7e0,
                0x100b6bb0, 0x100bebb0, 0x100beff0)
``arrange``     align, size, grouping, similar / inner / outer selection, measure
``snap``        the dynamic capture engine (CDynCaptureEng, 0x10054420)
``text``        text to contours (CGlyText / GetGlyphOutlineW, 0x100f51c0)
``qr``          QR code as scan lines (CQRCodeCreateDlg, MainApp 0x004b67d0)
``tools``       interactive state machines for each drawing command (Qt-free)
"""
