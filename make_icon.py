"""Generate ezexp.ico from the app's app_logo_pixmap (run by build.bat)."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtCore import QSize

import app as ezexp

app = QApplication(sys.argv)
icon = QIcon()
base = ezexp.app_logo_pixmap(256)
for size in (16, 20, 24, 32, 48, 64, 128, 256):
    icon.addPixmap(base.scaled(QSize(size, size)))
tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ezexp.png")
icon.pixmap(256, 256).save(tmp, "PNG")
from PIL import Image
Image.open(tmp).save(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ezexp.ico"),
    format="ICO",
    sizes=[(s, s) for s in (16, 20, 24, 32, 48, 64, 128, 256)])
os.remove(tmp)
print("ezexp.ico written")
