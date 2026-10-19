"""cProfile MainWindow.set_document on a 5000-part engraving-scan document."""
import sys, cProfile, pstats, io
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import numpy as np
from mlaser.engrave.raster import generate_gcode, RasterParams
from mlaser.io.gcode import parse_gcode
from mlaser.model import Document
img = (np.indices((600, 800)).sum(0) % 255).astype(np.uint8)
full = parse_gcode(generate_gcode(img, RasterParams(mode="dither", width_mm=100, rho=10)))
doc = Document(parts=full.parts[:5000], layers=full.layers)
from PySide6.QtWidgets import QApplication
app = QApplication([])
from mlaser.ui.mainwindow import MainWindow
w = MainWindow()
pr = cProfile.Profile(); pr.enable(); w.set_document(doc); pr.disable()
s = io.StringIO(); pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(25); print(s.getvalue()[:5000])
s = io.StringIO(); pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(12); print(s.getvalue()[:3000])
