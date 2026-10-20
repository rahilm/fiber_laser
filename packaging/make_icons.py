"""Render the application icon (mlaser/ui/resources/icons/app/mlaser.svg) for the bundles.

``python packaging/make_icons.py OUTDIR`` writes

* ``mlaser.icns`` (macOS bundle icon, ``CFBundleIconFile``),
* ``mlaser.ico`` (Windows executable icon: 16 ... 256 px),
* ``hicolor/<n>x<n>/apps/io.github.mlaser.png`` (Linux icon theme layout, next to the .desktop file),
* ``mlaser-<n>.png``.

Qt renders the SVG (offscreen platform), Pillow writes the containers.  packaging/mlaser.spec
runs it in a subprocess before the build.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "mlaser" / "ui" / "resources" / "icons" / "app" / "mlaser.svg"
APP_ID = "io.github.mlaser"
SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)


def render(size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(str(SVG)).render(p)
    p.end()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba.data())


def main(argv: list[str]) -> int:
    out = Path(argv[0] if argv else "packaging/build/icons")
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PIL import Image
    from PySide6.QtGui import QGuiApplication
    _app = QGuiApplication.instance() or QGuiApplication(["make_icons"])  # noqa: F841
    pngs = {n: render(n) for n in SIZES}
    for n, data in pngs.items():
        (out / f"mlaser-{n}.png").write_bytes(data)
        if n <= 512:
            d = out / "hicolor" / f"{n}x{n}" / "apps"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{APP_ID}.png").write_bytes(data)
    big = Image.open(io.BytesIO(pngs[1024])).convert("RGBA")
    big.save(out / "mlaser.icns")
    Image.open(io.BytesIO(pngs[256])).convert("RGBA").save(
        out / "mlaser.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    print(f"icons: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
