import os
import time
import ctypes
from ctypes import wintypes

from PySide6.QtCore import (
    Qt, QFileInfo, QTimer, QThread, QUrl, QSize, QPointF, QRect, QRectF, QLineF,
    QMimeData, QEvent, Signal, QByteArray, QObject,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QImage, QKeySequence, QLinearGradient,
    QPainter, QPainterPath, QPolygonF, QPixmap, QPen, QShortcut, QGuiApplication,
    QDrag, QDesktopServices,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFileIconProvider, QFrame, QGraphicsItem,
    QGraphicsLineItem, QGraphicsObject, QGraphicsScene, QGraphicsView, QGridLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QStyle, QStyledItemDelegate, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QDialog, QPlainTextEdit,
)

from ezexp.fsutil import (
    ext_of, category_for, kind_for, human_size, fmt_date, join_path, real_dir,
    ACCENT, SUBTLE, TEXT
)
from ezexp.shell_win import (
    shell_thumbnail, recycle_path, show_properties, reveal_in_explorer
)
from ezexp.settings_store import (
    APP_EXTS, get_quicklaunch, set_quicklaunch, get_launch_counts, bump_launch_count, remove_launch_count
)
from ezexp.search_engine import sort_rows

CAT_COLORS = {
    "folder": "#0a84ff", "img": "#34c759", "vid": "#bf5af2", "aud": "#ff9f0a",
    "zip": "#64d2ff", "code": "#30d158", "doc": "#98989d", "other": "#98989d",
    "app": "#0a84ff",
}

_icon_cache = {}

def _painter_on(size):
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    return pm, p

def category_icon(cat):
    if cat in _icon_cache:
        return _icon_cache[cat]
    pm, p = _painter_on(64)
    pen = QPen(QColor("#c2c2c9"), 2)
    if cat == "folder":
        grad = QLinearGradient(0, 12, 0, 52)
        grad.setColorAt(0, QColor("#6cb5ff"))
        grad.setColorAt(1, QColor("#0a84ff"))
        p.setPen(QPen(QColor("#0a84ff"), 1))
        p.setBrush(QColor("#3f9bff"))
        p.drawRoundedRect(7, 14, 22, 12, 3, 3)
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(7, 19, 50, 32, 7, 7)
    else:
        p.setPen(pen)
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(18, 8, 32, 48, 5, 5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e9e9ee"))
        fold = QPainterPath()
        fold.moveTo(38, 8)
        fold.lineTo(50, 20)
        fold.lineTo(38, 20)
        fold.closeSubpath()
        p.drawPath(fold)
        color = QColor(CAT_COLORS.get(cat, "#98989d"))
        if cat == "doc":
            p.setPen(QPen(QColor("#b0b0b7"), 2.4))
            for i, y in enumerate((30, 36, 42)):
                p.drawLine(25, y, 43 if i < 2 else 36, y)
        elif cat == "img":
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawEllipse(QColor("#ffcB3E").name()) if False else p.drawEllipse(24, 24, 8, 8)
            path = QPainterPath()
            path.moveTo(23, 44)
            path.lineTo(30, 32)
            path.lineTo(35, 39)
            path.lineTo(38, 35)
            path.lineTo(45, 44)
            path.closeSubpath()
            p.drawPath(path)
        elif cat == "vid":
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            tri = QPainterPath()
            tri.moveTo(27, 28)
            tri.lineTo(27, 44)
            tri.lineTo(43, 36)
            tri.closeSubpath()
            p.drawPath(tri)
        elif cat == "aud":
            p.setPen(QPen(color, 2.6))
            p.drawLine(33, 44, 33, 26)
            p.drawLine(33, 26, 41, 29)
            p.setPen(Qt.NoPen)
            p.drawEllipse(27, 40, 8, 6)
        elif cat == "zip":
            p.setPen(QPen(color, 2.6, Qt.DashLine))
            p.drawLine(34, 24, 34, 46)
        elif cat == "code":
            p.setPen(QPen(color, 3))
            p.drawLine(27, 28, 20, 36)
            p.drawLine(20, 36, 27, 44)
            p.drawLine(41, 28, 48, 36)
            p.drawLine(48, 36, 41, 44)
    p.end()
    icon = QIcon(pm)
    _icon_cache[cat] = icon
    return icon

def place_icon(kind):
    key = "place:" + kind
    if key in _icon_cache:
        return _icon_cache[key]
    pm, p = _painter_on(18)
    pen = QPen(QColor(ACCENT), 1.7)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if kind == "grid":
        for x, y in ((3, 3), (10, 3), (3, 10), (10, 10)):
            p.drawRoundedRect(x, y, 5, 5, 1.5, 1.5)
    elif kind == "monitor":
        p.drawRoundedRect(2, 3, 14, 9, 2, 2)
        p.drawLine(9, 12, 9, 15)
        p.drawLine(6, 15, 12, 15)
    elif kind == "files":
        p.drawRoundedRect(4, 4, 9, 12, 2, 2)
        p.drawLine(7, 2, 14, 2)
        p.drawLine(14, 2, 14, 13)
    elif kind == "download":
        p.drawLine(9, 2, 9, 11)
        p.drawLine(5, 7, 9, 11)
        p.drawLine(13, 7, 9, 11)
        p.drawLine(3, 14, 15, 14)
    elif kind == "image":
        p.drawRoundedRect(2, 3, 14, 12, 2, 2)
        p.drawEllipse(5, 5, 3, 3)
        p.drawLine(3, 12, 7, 9)
        p.drawLine(7, 9, 10, 12)
        p.drawLine(10, 12, 13, 8)
    elif kind == "music":
        p.drawEllipse(4, 11, 4, 3)
        p.drawLine(8, 12, 8, 3)
        p.drawLine(8, 3, 13, 5)
        p.drawLine(13, 5, 13, 8)
    elif kind == "film":
        p.drawRoundedRect(2, 3, 14, 12, 2, 2)
        p.drawLine(6, 3, 6, 15)
        p.drawLine(2, 7, 6, 7)
        p.drawLine(2, 11, 6, 11)
    elif kind == "drive":
        p.drawRoundedRect(2, 5, 14, 8, 2, 2)
        p.drawEllipse(12, 8, 2, 2)
        p.drawLine(4, 10, 9, 10)
    elif kind == "vault":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#8b5cf6"))
        gem = QPainterPath()
        gem.moveTo(9, 2)
        gem.lineTo(15, 8)
        gem.lineTo(9, 16)
        gem.lineTo(3, 8)
        gem.closeSubpath()
        p.drawPath(gem)
        p.setBrush(QColor("#c4b5fd"))
        p.drawPolygon(QPolygonF([QPointF(9, 2), QPointF(12, 8), QPointF(9, 8)]))
    p.end()
    icon = QIcon(pm)
    _icon_cache[key] = icon
    return icon

def search_glyph():
    key = "glyph:search"
    if key in _icon_cache:
        return _icon_cache[key]
    pm, p = _painter_on(16)
    p.setPen(QPen(QColor(SUBTLE), 1.8))
    p.drawEllipse(2, 2, 9, 9)
    p.drawLine(10, 10, 14, 14)
    p.end()
    icon = QIcon(pm)
    _icon_cache[key] = icon
    return icon

def app_icon(size=18):
    """Quick-launch glyph: a rounded tile with a play wedge."""
    key = "glyph:app:%d" % size
    if key in _icon_cache:
        return _icon_cache[key]
    pm, p = _painter_on(size)
    s = size / 18.0
    p.setPen(QPen(QColor(ACCENT), 1.6 * s))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(2 * s, 2 * s, 14 * s, 14 * s, 4 * s, 4 * s)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ACCENT))
    tri = QPainterPath()
    tri.moveTo(7 * s, 6 * s)
    tri.lineTo(7 * s, 12 * s)
    tri.lineTo(13 * s, 9 * s)
    tri.closeSubpath()
    p.drawPath(tri)
    p.end()
    icon = QIcon(pm)
    _icon_cache[key] = icon
    return icon

def resolve_lnk_target(path):
    """Target path of a .lnk shortcut via IShellLink (ctypes COM, same
    style as shell_thumbnail). None when unresolvable."""
    from ctypes import byref, c_long, c_ubyte, c_ulong, c_ushort, c_void_p, \
        c_wchar_p, create_unicode_buffer
    if not path or not path.lower().endswith(".lnk"):
        return None

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", c_ulong), ("Data2", c_ushort),
                    ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

    def guid(a):
        return GUID(a, 0, 0, (c_ubyte * 8)(0xC0, 0, 0, 0, 0, 0, 0, 0x46))

    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)
    link = c_void_p()
    persist = c_void_p()
    try:
        hr = ole32.CoCreateInstance(byref(guid(0x00021401)), None, 1,
                                    byref(guid(0x000214F9)), byref(link))
        if hr != 0 or not link:
            return None
        vtbl = ctypes.cast(
            link, ctypes.POINTER(ctypes.POINTER(c_void_p))).contents
        qi = ctypes.WINFUNCTYPE(
            c_long, c_void_p, c_void_p,
            ctypes.POINTER(c_void_p))(vtbl[0])
        if qi(link, byref(guid(0x0000010B)),
              byref(persist)) != 0 or not persist:
            return None
        pvtbl = ctypes.cast(
            persist, ctypes.POINTER(ctypes.POINTER(c_void_p))).contents
        load = ctypes.WINFUNCTYPE(
            c_long, c_void_p, c_wchar_p, c_ulong)(pvtbl[5])
        if load(persist, path, 0) != 0:
            return None
        getpath = ctypes.WINFUNCTYPE(
            c_long, c_void_p, c_wchar_p, c_long,
            c_void_p, c_ulong)(vtbl[3])
        buf = create_unicode_buffer(32768)
        if getpath(link, buf, 32768, None, 0) != 0 or not buf.value:
            return None
        return buf.value
    except Exception:
        return None
    finally:
        for ptr in (persist, link):
            if ptr:
                vt = ctypes.cast(
                    ptr, ctypes.POINTER(
                        ctypes.POINTER(c_void_p))).contents
                ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vt[2])(ptr)
        ole32.CoUninitialize()

_icon_provider = None

def real_file_icon(path):
    """The file's real shell icon (embedded exe/lnk icon on Windows);
    the painted generic app glyph when none is available."""
    global _icon_provider
    try:
        if _icon_provider is None:
            _icon_provider = QFileIconProvider()
        icon = _icon_provider.icon(QFileInfo(path))
        if icon is not None and not icon.isNull():
            return icon
    except Exception:
        pass
    return app_icon()

def app_icon_for(path):
    """Icon for a Quick Launch entry: for a .lnk, the TARGET app's own
    icon (no shortcut overlay); otherwise the file's real icon."""
    if path and path.lower().endswith(".lnk"):
        target = resolve_lnk_target(path)
        if target:
            return real_file_icon(target)
    return real_file_icon(path)

_spot_icon_cache = {}

def spot_row_icon(row):
    cat = row.get("category", "other")
    try:
        path = join_path(row.get("parent", ""), row.get("name", ""))
        if len(path) == 2 and path[1] == ":":
            path += "\\"
        elif len(path) == 2:
            path += "\\"
        key = path.lower()
        if key in _spot_icon_cache:
            return _spot_icon_cache[key]

        name = row.get("name", "")
        if ext_of(name) in APP_EXTS:
            try:
                icon = app_icon_for(path)
            except Exception:
                icon = category_icon(cat)
        elif cat == "img":
            try:
                icon = real_file_icon(path)
            except Exception:
                icon = category_icon(cat)
        else:
            icon = category_icon(cat)

        if icon is None:
            icon = category_icon(cat)

        if len(_spot_icon_cache) > 300:
            _spot_icon_cache.clear()
        _spot_icon_cache[key] = icon
        return icon
    except Exception:
        try:
            return category_icon(cat)
        except Exception:
            return QIcon()

def app_logo_pixmap(size=256):
    """EzExp logo: mac-style tile, folder + magnifier, purple vault spark."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 256.0
    tile = QRectF(16 * s, 16 * s, 224 * s, 224 * s)
    tile_path = QPainterPath()
    tile_path.addRoundedRect(tile, 52 * s, 52 * s)
    grad = QLinearGradient(0, 16 * s, 0, 240 * s)
    grad.setColorAt(0, QColor("#fbfbfd"))
    grad.setColorAt(1, QColor("#e6e8ee"))
    p.fillPath(tile_path, grad)
    p.setPen(QPen(QColor("#d2d5dc"), 2 * s))
    p.drawPath(tile_path)
    # folder
    p.setPen(Qt.NoPen)
    fgrad = QLinearGradient(0, 70 * s, 0, 200 * s)
    fgrad.setColorAt(0, QColor("#6cb5ff"))
    fgrad.setColorAt(1, QColor("#0a84ff"))
    p.setBrush(QColor("#3f9bff"))
    p.drawRoundedRect(QRectF(50 * s, 78 * s, 66 * s, 26 * s), 9 * s, 9 * s)
    p.setBrush(fgrad)
    p.drawRoundedRect(QRectF(50 * s, 92 * s, 152 * s, 106 * s), 16 * s, 16 * s)
    # magnifier
    pen = QPen(QColor("#8b9198"), 15 * s)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(158 * s, 158 * s), 42 * s, 42 * s)
    pen.setWidthF(19 * s)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(188 * s, 188 * s), QPointF(214 * s, 214 * s))
    # vault spark on the glass
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#8b5cf6"))
    p.drawEllipse(QPointF(146 * s, 146 * s), 11 * s, 11 * s)
    p.end()
    return pm

def rounded_pixmap(pm, radius=8):
    out = QPixmap(pm.size())
    out.setDevicePixelRatio(pm.devicePixelRatio())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pm.width(), pm.height(), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out

def with_play_badge(pm):
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    w, h = pm.width(), pm.height()
    r = min(w, h) * 0.14
    cx, cy = w / 2.0, h / 2.0
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(20, 20, 22, 170))
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.setBrush(QColor(255, 255, 255, 235))
    tri = QPainterPath()
    s = r * 0.62
    tri.moveTo(cx - s * 0.5, cy - s)
    tri.lineTo(cx - s * 0.5, cy + s)
    tri.lineTo(cx + s * 0.85, cy)
    tri.closeSubpath()
    p.drawPath(tri)
    p.end()
    return pm

RESIZE_MARGIN = 6

class FramelessWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(900, 560)
        self._resize_dir = 0
        self._resize_geo = None
        self._resize_gpos = None
        self._move_offset = None
        self._maximized = False
        self._normal_geo = None

    # -- traffic light actions --------------------------------------------
    def toggle_max(self):
        if self._maximized:
            self._maximized = False
            if self._normal_geo:
                self.setGeometry(self._normal_geo)
            self.on_max_state()
        else:
            self._normal_geo = self.geometry()
            avail = self.screen().availableGeometry()
            self._maximized = True
            self.setGeometry(avail)
            self.on_max_state()

    def on_max_state(self):
        pass

    def begin_move(self, gpos):
        if self._maximized:
            return
        self._move_offset = gpos - self.frameGeometry().topLeft()

    def move_move(self, gpos):
        if self._maximized or self._move_offset is None:
            return
        self.move(gpos - self._move_offset)

    def end_move(self):
        self._move_offset = None

    # -- edge resizing ------------------------------------------------------
    def _hit_edges(self, pos):
        if self._maximized:
            return 0
        m = RESIZE_MARGIN
        r = self.rect()
        L = 1 if pos.x() <= m else 0
        R = 2 if pos.x() >= r.width() - m else 0
        T = 4 if pos.y() <= m else 0
        B = 8 if pos.y() >= r.height() - m else 0
        return L | R | T | B

    _CURSORS = {1: Qt.SizeHorCursor, 2: Qt.SizeHorCursor, 4: Qt.SizeVerCursor,
                8: Qt.SizeVerCursor, 5: Qt.SizeFDiagCursor, 10: Qt.SizeBDiagCursor,
                6: Qt.SizeBDiagCursor, 9: Qt.SizeFDiagCursor}

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return super().mousePressEvent(e)
        pos = e.position().toPoint()
        d = self._hit_edges(pos)
        if d:
            self._resize_dir = d
            self._resize_geo = self.geometry()
            self._resize_gpos = e.globalPosition().toPoint()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if self._resize_dir and self._resize_geo is not None:
            g = e.globalPosition().toPoint()
            geo = QRect(self._resize_geo)
            dx = g.x() - self._resize_gpos.x()
            dy = g.y() - self._resize_gpos.y()
            d = self._resize_dir
            mn = self.minimumSize()
            if d & 1:
                nw = max(mn.width(), geo.width() - dx)
                geo.setX(geo.right() - nw + 1)
                geo.setWidth(nw)
            if d & 2:
                geo.setWidth(max(mn.width(), geo.width() + dx))
            if d & 4:
                nh = max(mn.height(), geo.height() - dy)
                geo.setY(geo.bottom() - nh + 1)
                geo.setHeight(nh)
            if d & 8:
                geo.setHeight(max(mn.height(), geo.height() + dy))
            self.setGeometry(geo)
            return
        d = self._hit_edges(pos)
        if d in self._CURSORS:
            self.setCursor(self._CURSORS[d])
        else:
            self.unsetCursor()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._resize_dir = 0
        self._resize_geo = None
        super().mouseReleaseEvent(e)

class TitleBar(QFrame):
    def __init__(self, window, with_search=True):
        super().__init__(window)
        self.setObjectName("titlebar")
        self.setFixedHeight(52)
        self.win = window
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(10)

        self.btn_close = QPushButton(self)
        self.btn_min = QPushButton(self)
        self.btn_max = QPushButton(self)
        for b, name in ((self.btn_close, "tclose"), (self.btn_min, "tmin"), (self.btn_max, "tmax")):
            b.setObjectName(name)
            b.setFixedSize(13, 13)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
        self.btn_close.clicked.connect(window.close)
        self.btn_min.clicked.connect(window.showMinimized)
        self.btn_max.clicked.connect(window.toggle_max)
        lay.addWidget(self.btn_close)
        lay.addWidget(self.btn_min)
        lay.addWidget(self.btn_max)

        self.btn_back = QToolButton(self)
        self.btn_back.setObjectName("backBtn")
        self.btn_back.setText("‹")
        self.btn_back.setFixedSize(26, 26)
        self.btn_back.setToolTip("Back")
        self.btn_back.setCursor(Qt.PointingHandCursor)
        self.btn_back.setFocusPolicy(Qt.NoFocus)
        self.btn_back.setAutoRaise(True)
        lay.addSpacing(4)
        lay.addWidget(self.btn_back)

        lay.addStretch(1)
        self.title = QLabel(window.windowTitle(), self)
        self.title.setObjectName("title")
        lay.addWidget(self.title)
        lay.addStretch(1)

        self.search = None
        if with_search:
            self.search = SearchField(self)
            self.search.setFixedWidth(230)
            lay.addWidget(self.search)

    def setTitle(self, text):
        self.title.setText(text)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.win.begin_move(e.globalPosition().toPoint())
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self.win.move_move(e.globalPosition().toPoint())
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self.win.end_move()
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        self.win.toggle_max()

class SearchField(QFrame):
    def __init__(self, parent=None, placeholder="Search"):
        super().__init__(parent)
        self.setObjectName("searchField")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 0, 9, 0)
        lay.setSpacing(6)
        icon = QLabel(self)
        icon.setPixmap(search_glyph().pixmap(14, 14))
        icon.setFixedSize(14, 14)
        lay.addWidget(icon)
        self.edit = QLineEdit(self)
        self.edit.setObjectName("searchEdit")
        self.edit.setPlaceholderText(placeholder)
        self.edit.setAttribute(Qt.WA_MacShowFocusRect, False)
        lay.addWidget(self.edit, 1)
        self.edit.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if obj is self.edit and ev.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self.setProperty("focus", "true" if ev.type() == QEvent.FocusIn else "false")
            self.style().unpolish(self)
            self.style().polish(self)
        return super().eventFilter(obj, ev)

class NameDelegate(QStyledItemDelegate):
    ROW_H = 46

    def sizeHint(self, option, index):
        return QSize(120, self.ROW_H)

    def paint(self, p, option, index):
        row = index.data(Qt.UserRole)
        if not row:
            return super().paint(p, option, index)
        selected = bool(option.state & QStyle.State_Selected)
        p.save()
        r = option.rect
        if selected:
            p.fillRect(r, QColor(ACCENT))
        icon = category_icon(row["category"])
        pm = icon.pixmap(30, 30)
        p.drawPixmap(r.left() + 12, r.top() + (r.height() - 30) // 2, pm)
        tx = r.left() + 52
        tw = r.width() - 64
        name_font = QFont(option.font)
        name_font.setPointSizeF(9.0)
        p.setFont(name_font)
        fm = QFontMetrics(name_font)
        name = fm.elidedText(row["name"], Qt.ElideRight, tw)
        p.setPen(QColor("#ffffff") if selected else QColor(TEXT))
        p.drawText(tx, r.top() + 8, tw, 18, Qt.AlignLeft | Qt.AlignVCenter, name)
        small = QFont(option.font)
        small.setPointSizeF(7.5)
        p.setFont(small)
        p.setPen(QColor(255, 255, 255, 204) if selected else QColor(SUBTLE))
        path = row["parent"] + ("\\" + row["name"] if not row["is_dir"] else "")
        if len(path) == 2:
            path = path + "\\"
        p.drawText(tx, r.top() + 25, tw, 14, Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(small).elidedText(path, Qt.ElideLeft, tw))
        p.restore()

class ResultsTree(QTreeWidget):
    """Tree that maps Return/Ctrl+Return to reveal/open."""
    reveal_requested = Signal()
    open_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(4)
        self.setHeaderLabels(["Name", "Size", "Kind", "Date Modified"])
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAllColumnsShowFocus(True)
        self.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.setSelectionBehavior(QTreeWidget.SelectRows)
        self.setSortingEnabled(False)
        self.setItemDelegateForColumn(0, NameDelegate(self))
        h = self.header()
        h.setSectionsMovable(False)
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in ((1, 90), (2, 150), (3, 110)):
            h.setSectionResizeMode(col, QHeaderView.Fixed)
            self.setColumnWidth(col, w)
        self.setVerticalScrollMode(QTreeWidget.ScrollPerItem)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def keyPressEvent(self, e):
        win = self.window()
        if e.modifiers() & Qt.ControlModifier and hasattr(win, "cut_current"):
            if e.key() == Qt.Key_X:
                win.cut_current()
                return
            if e.key() == Qt.Key_C:
                win.copy_current_file()
                return
            if e.key() == Qt.Key_V:
                row = win.current_row()
                if row and row["is_dir"]:
                    folder = win.row_path(row)
                elif row:
                    folder = row["parent"]
                else:
                    folder = win.current_place
                win.paste_into(folder)
                return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if e.modifiers() & Qt.ControlModifier:
                self.open_requested.emit()
            else:
                self.reveal_requested.emit()
            return
        super().keyPressEvent(e)

    def startDrag(self, supportedActions):
        win = self.window()
        rows = [it.data(0, Qt.UserRole) for it in self.selectedItems()]
        if not rows or not hasattr(win, "row_path"):
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(win.row_path(r)) for r in rows])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction | Qt.CopyAction, Qt.MoveAction)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        md = e.mimeData()
        if not md.hasUrls():
            super().dropEvent(e)
            return
        win = self.window()
        item = self.itemAt(e.position().toPoint())
        if item is not None:
            row = item.data(0, Qt.UserRole)
            folder = win.row_path(row) if row["is_dir"] else row["parent"]
        else:
            folder = getattr(win, "current_place", None)
        paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        if folder and paths and hasattr(win, "handle_drop"):
            internal = e.source() is self
            copy = (not internal) or bool(
                e.keyboardModifiers() & Qt.ControlModifier) or \
                e.proposedAction() == Qt.CopyAction
            win.handle_drop(paths, folder, copy)
            e.acceptProposedAction()

def make_row_item(row):
    item = QTreeWidgetItem()
    item.setData(0, Qt.UserRole, row)
    item.setFlags(item.flags() | Qt.ItemIsDragEnabled)
    item.setText(1, "—" if row["is_dir"] else human_size(row["size"]))
    item.setText(2, row["kind"])
    item.setText(3, fmt_date(row["mtime"]))
    item.setTextAlignment(1, Qt.AlignLeft | Qt.AlignVCenter)
    item.setTextAlignment(2, Qt.AlignLeft | Qt.AlignVCenter)
    item.setTextAlignment(3, Qt.AlignLeft | Qt.AlignVCenter)
    item.setData(0, Qt.AccessibleTextRole, row["name"])
    return item

QSS = """
* { font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif; }
QWidget { color: #1d1d1f; font-size: 13px; }

#titlebar { background: #f1f1f4; border-bottom: 1px solid #e3e3e8;
    border-top-left-radius: 12px; border-top-right-radius: 12px; }
#title { font-size: 15px; font-weight: 600; color: #26262a; background: transparent; }
QPushButton#tclose { background: #ff5f57; border: none; border-radius: 6.5px; min-width: 13px; max-width: 13px; min-height: 13px; max-height: 13px; }
QPushButton#tclose:hover { background: #e0443e; }
QPushButton#tmin { background: #febc2e; border: none; border-radius: 6.5px; min-width: 13px; max-width: 13px; min-height: 13px; max-height: 13px; }
QPushButton#tmin:hover { background: #e6a926; }
QPushButton#tmax { background: #28c840; border: none; border-radius: 6.5px; min-width: 13px; max-width: 13px; min-height: 13px; max-height: 13px; }
QPushButton#tmax:hover { background: #22ad38; }

#sidebar { background: #f0f0f4; border-right: 1px solid #e3e3e8; }
#sideSection { color: #98989d; font-size: 11px; font-weight: 600; background: transparent; }
QPushButton#sideBtn { background: transparent; border: none; border-radius: 7px;
    padding: 6px 10px; text-align: left; color: #1d1d1f; }
QPushButton#sideBtn:hover:!checked { background: rgba(0,0,0,0.06); }
QPushButton#sideBtn:checked { background: #0a84ff; color: #fff; }
QPushButton#sideBtn:checked:hover { background: #0a84ff; }
QPushButton#sideAction { background: transparent; border: none; border-radius: 7px;
    padding: 6px 10px; text-align: left; color: #6e6e73; font-size: 12px; }
QPushButton#sideAction:hover { background: rgba(0,0,0,0.06); }
QPushButton#sideAction:disabled { color: #c7c7cc; }

#content { background: #ffffff; }
#chips { background: #ffffff; }
#chips QPushButton { background: #f1f1f4; border: none; border-radius: 999px;
    padding: 5px 12px; font-size: 12px; color: #3a3a3c; }
#chips QPushButton:hover:!checked { background: #e8e8ed; }
#chips QPushButton:checked { background: #0a84ff; color: #fff; }

QTreeWidget { background: #ffffff; border: none; outline: none; }
QTreeWidget::item { border: none; padding: 0; }
QTreeWidget::item:hover { background: #f6f6f8; }
QTreeWidget::item:selected { background: #0a84ff; color: #ffffff; }
QHeaderView::section { background: #fafafa; color: #86868b; font-size: 11px;
    font-weight: 600; border: none; border-bottom: 1px solid #ececf0;
    border-right: 1px solid #f4f4f6; padding: 6px 8px; }
#emptyLabel { color: #98989d; background: #ffffff; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #c9c9ce; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #b6b6bc; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #c9c9ce; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

#statusbar { background: #f6f6f8; border-top: 1px solid #e7e7ec;
    border-bottom-left-radius: 12px; border-bottom-right-radius: 12px; }
#statusText { color: #86868b; font-size: 11px; background: transparent; }

#details { background: #fafafc; border-left: 1px solid #ececf0; }
#dName { font-size: 15px; font-weight: 600; background: transparent; }
#dKind { color: #98989d; font-size: 12px; background: transparent; }
#dKey { color: #98989d; font-size: 12px; background: transparent; }
#dVal { color: #3a3a3c; font-size: 12px; background: transparent; }
QPushButton#btnPrimary { background: #0a84ff; color: #fff; border: none;
    border-radius: 8px; padding: 8px 12px; font-weight: 500; }
QPushButton#btnPrimary:hover { background: #0071e3; }
QPushButton#btnPrimary:disabled { background: #b7d7ff; }
QPushButton#btnSecondary { background: #e9e9ee; border: none; border-radius: 8px; padding: 8px 12px; }
QPushButton#btnSecondary:hover { background: #e0e0e6; }
QPushButton#btnSecondary:disabled { color: #c7c7cc; }
QPushButton#dots { background: #e9e9ee; border: none; border-radius: 8px;
    font-size: 15px; color: #6e6e73; padding: 6px 0; }
QPushButton#dots:hover { background: #e0e0e6; }

#searchField { background: #e4e4e9; border-radius: 8px; border: 1px solid transparent; }
#searchField[focus="true"] { background: #ffffff; border: 1px solid #0a84ff; }
#searchField QLineEdit { background: transparent; border: none; font-size: 13px; }

#spotPanel { background: #ffffff; border: 1px solid #e6e6ea; border-radius: 16px; }
#spotInput { font-size: 20px; border: none; background: transparent; }
#spotFooter { color: #86868b; font-size: 12px; border-top: 1px solid #ececf0;
    background: #fafafa; border-bottom-left-radius: 16px; border-bottom-right-radius: 16px; }
QPushButton#spotModeBtn { background: #f1f1f4; border: none; border-radius: 999px;
    padding: 3px 12px; font-size: 12px; color: #3a3a3c; }
QPushButton#spotModeBtn:hover:!checked { background: #e8e8ed; }
QPushButton#spotModeBtn:checked { background: #0a84ff; color: #ffffff; font-weight: 500; }
QPushButton#spotEndpointBtn { background: #f1f1f4; border: none; border-radius: 8px;
    padding: 3px 10px; font-size: 11px; color: #6e6e73; }
QPushButton#spotEndpointBtn:hover { background: #e8e8ed; color: #1d1d1f; }
QPushButton#spotWorkspaceBtn { background: #f1f1f4; border: none; border-radius: 8px;
    padding: 3px 10px; font-size: 11px; color: #6e6e73; }
QPushButton#spotWorkspaceBtn:hover { background: #e8e8ed; color: #1d1d1f; }
QPushButton#spotWorkspaceBtn:checked { background: #0a84ff; color: #ffffff; font-weight: 500; }
#spotWorkspacePane { background: #fafafc; border-left: 1px solid #e6e6ea; }
#wsFileList { background: #ffffff; border: 1px solid #e6e6ea; border-radius: 6px; font-size: 11px; }
#wsPreview { background: #ffffff; border: 1px solid #e6e6ea; border-radius: 6px; font-size: 11px; font-family: Consolas, monospace; }
QPushButton#wsBtnRefresh { background: #f1f1f4; border: none; border-radius: 4px; padding: 2px 8px; font-size: 11px; color: #3a3a3c; }
QPushButton#wsBtnRefresh:hover { background: #e8e8ed; }
QPushButton#wsBtnCollapse { background: transparent; border: none; border-radius: 4px; font-size: 12px; color: #86868b; }
QPushButton#wsBtnCollapse:hover { background: #e8e8ed; color: #1d1d1f; }
#spotTranscript { background: #ffffff; border: none; padding: 12px 18px; font-size: 13px; color: #1d1d1f; }
#dockTranscript { background: #ffffff; color: #1d1d1f; border: 1px solid #e6e6ea; border-radius: 8px; padding: 8px 10px; font-size: 13px; }
#dockInput { background: #ffffff; color: #1d1d1f; border: 1px solid #e6e6ea; border-radius: 8px; padding: 6px 10px; font-size: 13px; }
#dockInput:focus { border: 1px solid #0a84ff; }
QPushButton#dockBtnSend { background: #0a84ff; color: #ffffff; border: none; border-radius: 8px; padding: 6px 12px; font-weight: 500; font-size: 13px; }
QPushButton#dockBtnSend:hover { background: #0071e3; }
QPushButton#dockBtnSend:disabled { background: #b7d7ff; }

QMenu { background: #ffffff; border: 1px solid #e3e3e8; border-radius: 10px; padding: 5px; }
QMenu::item { padding: 6px 24px 6px 12px; border-radius: 6px; background: transparent; }
QMenu::item:selected { background: #0a84ff; color: #fff; }
QMenu::separator { height: 1px; background: #ececf0; margin: 4px 8px; }

QToolTip { background: rgba(28,28,30,0.92); color: #fff; border: none;
    padding: 5px 8px; border-radius: 6px; font-size: 12px; }

#backBtn { background: transparent; border: none; border-radius: 13px;
    color: #6e6e73; font-size: 16px; }
#backBtn:hover:!disabled { background: rgba(0,0,0,0.08); }
#backBtn:disabled { color: #c7c7cc; }
#textPreview { color: #3a3a3c; font-size: 11px; background: #f4f4f6;
    border-radius: 8px; padding: 10px; }
#previewLabel { background: transparent; }
#graphMsg { color: #98989d; font-size: 13px; background: transparent; }

QMessageBox, QInputDialog { background: #f5f5f7; }
QPushButton { min-height: 22px; }
"""
