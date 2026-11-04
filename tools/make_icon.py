"""Рисует иконку приложения и собирает assets/icon.icns.

Запуск: ./venv/bin/python tools/make_icon.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

ROOT = Path(__file__).resolve().parent.parent
SIZE = 1024


def draw() -> QImage:
    img = QImage(SIZE, SIZE, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    # подложка по сетке иконок macOS: 824×824 со скруглением ~185
    bg = QRectF(100, 100, 824, 824)
    grad = QLinearGradient(bg.topLeft(), bg.bottomLeft())
    grad.setColorAt(0, QColor("#ef4444"))
    grad.setColorAt(1, QColor("#b91c1c"))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(bg, 185, 185)

    # лист с загнутым углом
    page = QRectF(262, 205, 500, 620)
    fold = 120
    path = QPainterPath()
    path.moveTo(page.left(), page.top())
    path.lineTo(page.right() - fold, page.top())
    path.lineTo(page.right(), page.top() + fold)
    path.lineTo(page.right(), page.bottom())
    path.lineTo(page.left(), page.bottom())
    path.closeSubpath()
    p.setBrush(QColor(0, 0, 0, 50))
    p.drawPath(path.translated(0, 14))
    p.setBrush(QColor("#ffffff"))
    p.drawPath(path)
    corner = QPainterPath()
    corner.moveTo(page.right() - fold, page.top())
    corner.lineTo(page.right() - fold, page.top() + fold)
    corner.lineTo(page.right(), page.top() + fold)
    corner.closeSubpath()
    p.setBrush(QColor("#e5e7eb"))
    p.drawPath(corner)

    # строки текста
    p.setBrush(QColor("#d1d5db"))
    for y, w in ((560, 380), (625, 330), (690, 380), (755, 250)):
        p.drawRoundedRect(QRectF(322, y, w, 26), 13, 13)

    # редактируемая строка: выделение, текст и курсор
    sel = QRectF(306, 395, 412, 120)
    p.setBrush(QColor(37, 99, 235, 40))
    p.setPen(QPen(QColor("#2563eb"), 10))
    p.drawRoundedRect(sel, 14, 14)
    font = QFont("Georgia")
    font.setPixelSize(104)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor("#111827"))
    p.drawText(QRectF(330, 395, 300, 120), Qt.AlignVCenter | Qt.AlignLeft, "Aa")
    p.setPen(QPen(QColor("#2563eb"), 12, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(520, 415), QPointF(520, 495))
    p.end()
    return img


def main():
    QGuiApplication(sys.argv)
    img = draw()
    assets = ROOT / "assets"
    iconset = assets / "icon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True)
    img.save(str(assets / "icon.png"))
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            img.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(str(iconset / name))
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(assets / "icon.icns")], check=True)
    shutil.rmtree(iconset)
    print("assets/icon.icns готов")


if __name__ == "__main__":
    main()
