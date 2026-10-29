"""
EzExp — a Mac-style universal file finder for Windows.

Indexes every file on every fixed drive into a local SQLite catalog, then
gives instant, Finder/Spotlight-style search. Files stay where they are;
EzExp just always knows where everything is.

- First launch: full index of all fixed drives.
- Every later launch: quick incremental scan (new/changed/deleted items).
- Search works immediately, even while the index is still being built.
- Enter / double-click reveals a result in Explorer; Ctrl+Enter opens it.
- Alt+Space anywhere in Windows summons the Spotlight-style quick search.
"""

import os
import sys
import re
import json
import math
import time
import traceback
import zlib
import ctypes
import threading
import subprocess
import queue
import collections
import shutil
import sqlite3
import datetime
from collections import deque
from ctypes import wintypes

from PySide6.QtCore import (
    Qt, QFileInfo, QTimer, QThread, QUrl, QSize, QPointF, QRect, QRectF, QLineF,
    QMimeData,
    QEvent, QSettings, Signal, QAbstractNativeEventFilter, QLockFile,
    QByteArray, QObject,
)
import urllib.parse

from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QImage, QKeySequence, QLinearGradient,
    QPainter, QPainterPath, QPolygonF, QPixmap, QPen, QShortcut, QGuiApplication,
    QDrag, QDesktopServices,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFileIconProvider, QFrame, QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsObject,
    QGraphicsScene, QGraphicsView, QGridLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QStyle,
    QStyledItemDelegate, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QDialog, QPlainTextEdit,
    QSplitter, QTableWidget, QTableWidgetItem, QAbstractItemView,
)
from PySide6.QtNetwork import (
    QNetworkAccessManager, QNetworkRequest, QNetworkReply,
)

if sys.platform != "win32":
    print("EzExp requires Windows.")
    sys.exit(1)

APP_NAME = "EzExp"
APP_VERSION = "1.1.0"
APP_DIR = DB_DIR = os.environ.get(
    "EZEXP_DB_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "EzExp"),
)
DB_PATH = os.path.join(DB_DIR, "index.db")


# ---------------------------------------------------------------------------
# Obsidian vault favourites (persisted per user)
# ---------------------------------------------------------------------------

def get_vaults():
    v = QSettings("ezexp", "EzExp").value("vaults", [])
    if isinstance(v, str):
        v = [v]
    return [p for p in (v or []) if isinstance(p, str) and p and os.path.isdir(p)]


def set_vaults(paths):
    s = QSettings("ezexp", "EzExp")
    s.setValue("vaults", [os.path.normpath(p) for p in paths])
    s.sync()


# ---------------------------------------------------------------------------
# Quick Launch (frequently-used + manually pinned apps)
# ---------------------------------------------------------------------------

APP_EXTS = {"exe", "lnk", "bat", "cmd", "msi"}


def get_quicklaunch():
    v = QSettings("ezexp", "EzExp").value("quicklaunch", [])
    if isinstance(v, str):
        v = [v]
    return [os.path.normpath(p) for p in (v or [])
            if isinstance(p, str) and p]


def set_quicklaunch(paths):
    s = QSettings("ezexp", "EzExp")
    s.setValue("quicklaunch", [os.path.normpath(p) for p in paths])
    s.sync()


def get_launch_counts():
    v = QSettings("ezexp", "EzExp").value("launch_counts", "")
    try:
        d = json.loads(v) if isinstance(v, str) and v else (v or {})
    except (ValueError, TypeError):
        d = {}
    out = {}
    for k, c in (d.items() if isinstance(d, dict) else []):
        try:
            out[k] = int(c)
        except (TypeError, ValueError):
            pass
    return out


def bump_launch_count(path):
    s = QSettings("ezexp", "EzExp")
    d = get_launch_counts()
    d[path] = d.get(path, 0) + 1
    s.setValue("launch_counts", json.dumps(d))
    s.sync()


def remove_launch_count(path):
    s = QSettings("ezexp", "EzExp")
    d = {p: c for p, c in get_launch_counts().items()
         if p.lower() != path.lower()}
    s.setValue("launch_counts", json.dumps(d))
    s.sync()


# ---------------------------------------------------------------------------
# Agent configuration (agent.json in APP_DIR)
# ---------------------------------------------------------------------------

DEFAULT_AGENT_CONFIG = {
    "backend": "endpoint",
    "harness_path": "",
    "harness_python": "py",
    "base_url": "https://integrate.api.nvidia.com/v1",
    "model": "meta/llama-3.3-70b-instruct",
    "api_key": "",
    "grant_dir": "",
}

_harness_probe_cache = {}


def clear_harness_probe_cache():
    global _harness_probe_cache
    _harness_probe_cache.clear()


def harness_available(cfg=None):
    if cfg is None:
        cfg = load_agent_config()
    backend = str(cfg.get("backend") or "harness").strip().lower()
    hp = (cfg.get("harness_path") or "").strip()
    python_bin = (cfg.get("harness_python") or "py").strip() or "py"
    if not hp or not os.path.isdir(hp):
        return False, f"Directory does not exist: '{hp}'" if hp else "Path is empty"

    cache_key = (backend, hp, python_bin)
    if cache_key in _harness_probe_cache:
        return _harness_probe_cache[cache_key]

    try:
        proc = subprocess.run(
            [python_bin, "-c", "import harness"],
            cwd=hp,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
        if proc.returncode == 0:
            res = (True, "")
            _harness_probe_cache[cache_key] = res
            return res
        else:
            stderr_lines = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
            last_line = stderr_lines[-1] if stderr_lines else f"Exited with code {proc.returncode}"
            return (False, last_line)
    except subprocess.TimeoutExpired as e:
        return (False, f"Harness availability check timed out after 30s: {e}")
    except Exception as e:
        return (False, str(e))


def agent_config_path():
    return os.path.join(APP_DIR, "agent.json")


def load_agent_config():
    cfg = dict(DEFAULT_AGENT_CONFIG)
    p = agent_config_path()
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


def save_agent_config(cfg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        full_cfg = dict(DEFAULT_AGENT_CONFIG)
        if isinstance(cfg, dict):
            full_cfg.update(cfg)
        with open(agent_config_path(), "w", encoding="utf-8") as fh:
            json.dump(full_cfg, fh, indent=2)
        clear_harness_probe_cache()
    except Exception:
        pass


def agent_status(cfg=None):
    if cfg is None:
        cfg = load_agent_config()
    backend = (cfg.get("backend") or "endpoint").lower()
    if backend == "harness":
        avail, err = harness_available(cfg)
        if not avail:
            return False, f"Organism Harness not available: {err}. Set the harness folder via 'Endpoint…' above."
        grant_dir = (cfg.get("grant_dir") or "").strip()
        if not grant_dir:
            return False, "Set a grant folder via 'Endpoint…' above — it becomes the harness workspace."
        return True, f"Organism Harness ready · workspace: {grant_dir}"
    else:
        api_key = (cfg.get("api_key") or "").strip()
        if not api_key:
            return False, "Setup required: No API key configured. Click 'Endpoint…' above to configure your OpenAI-compatible endpoint and API key."
        return True, ""


class AgentSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Agent Endpoint Settings")
        self.setMinimumWidth(440)
        self.cfg = load_agent_config()

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(18, 18, 18, 18)

        lbl_backend = QLabel("Backend:", self)
        self.combo_backend = QComboBox(self)
        self.combo_backend.addItem("Endpoint")
        self.combo_backend.addItem("Organism Harness")
        backend = (self.cfg.get("backend") or "endpoint").lower()
        if backend == "harness":
            self.combo_backend.setCurrentIndex(1)
        else:
            self.combo_backend.setCurrentIndex(0)
        lay.addWidget(lbl_backend)
        lay.addWidget(self.combo_backend)

        self.lbl_harness = QLabel("Harness folder (Organism repo path):", self)
        lay.addWidget(self.lbl_harness)
        harness_row = QHBoxLayout()
        self.edit_harness = QLineEdit(self)
        self.edit_harness.setText(self.cfg.get("harness_path", ""))
        self.edit_harness.setPlaceholderText("Path to organism_ai checkout")
        self.btn_choose_harness = QPushButton("Choose…", self)
        self.btn_choose_harness.clicked.connect(self._choose_harness)
        harness_row.addWidget(self.edit_harness, 1)
        harness_row.addWidget(self.btn_choose_harness)
        lay.addLayout(harness_row)

        self.lbl_python = QLabel("Harness Python executable:", self)
        lay.addWidget(self.lbl_python)
        self.edit_python = QLineEdit(self)
        self.edit_python.setText(self.cfg.get("harness_python", "py"))
        self.edit_python.setPlaceholderText("py")
        lay.addWidget(self.edit_python)

        lbl_url = QLabel("Base URL:", self)
        self.edit_url = QLineEdit(self)
        self.edit_url.setText(self.cfg.get("base_url", ""))
        self.edit_url.setPlaceholderText("https://integrate.api.nvidia.com/v1")
        lay.addWidget(lbl_url)
        lay.addWidget(self.edit_url)

        lbl_model = QLabel("Model:", self)
        self.edit_model = QLineEdit(self)
        self.edit_model.setText(self.cfg.get("model", ""))
        self.edit_model.setPlaceholderText("meta/llama-3.3-70b-instruct")
        lay.addWidget(lbl_model)
        lay.addWidget(self.edit_model)

        lbl_key = QLabel("API key:", self)
        self.edit_key = QLineEdit(self)
        self.edit_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_key.setText(self.cfg.get("api_key", ""))
        self.edit_key.setPlaceholderText("API key (stored locally only)")
        lay.addWidget(lbl_key)
        lay.addWidget(self.edit_key)

        lbl_grant = QLabel("Grant folder (agent write sandbox):", self)
        lay.addWidget(lbl_grant)
        grant_row = QHBoxLayout()
        self.edit_grant = QLineEdit(self)
        self.edit_grant.setText(self.cfg.get("grant_dir", ""))
        self.edit_grant.setPlaceholderText("Folder where agent is allowed to create files")
        btn_choose = QPushButton("Choose…", self)
        btn_choose.clicked.connect(self._choose_grant)
        grant_row.addWidget(self.edit_grant, 1)
        grant_row.addWidget(btn_choose)
        lay.addLayout(grant_row)

        self.combo_backend.currentIndexChanged.connect(self._sync_backend_ui)
        self._sync_backend_ui()

        lay.addSpacing(6)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_cancel = QPushButton("Cancel", self)
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("Save", self)
        btn_save.setObjectName("btnPrimary")
        btn_save.clicked.connect(self._on_save)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        lay.addLayout(btn_row)

    def _sync_backend_ui(self):
        is_harness = (self.combo_backend.currentIndex() == 1)
        self.lbl_harness.setEnabled(is_harness)
        self.edit_harness.setEnabled(is_harness)
        self.btn_choose_harness.setEnabled(is_harness)
        self.lbl_python.setEnabled(is_harness)
        self.edit_python.setEnabled(is_harness)
        self.lbl_harness.setVisible(is_harness)
        self.edit_harness.setVisible(is_harness)
        self.btn_choose_harness.setVisible(is_harness)
        self.lbl_python.setVisible(is_harness)
        self.edit_python.setVisible(is_harness)

    def _choose_harness(self):
        curr = self.edit_harness.text().strip() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose Organism Harness Folder", curr)
        if chosen:
            self.edit_harness.setText(os.path.normpath(chosen))

    def _choose_grant(self):
        curr = self.edit_grant.text().strip() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose Grant Folder", curr)
        if chosen:
            self.edit_grant.setText(os.path.normpath(chosen))

    def _on_save(self):
        cfg = {
            "backend": "harness" if self.combo_backend.currentIndex() == 1 else "endpoint",
            "harness_path": self.edit_harness.text().strip(),
            "harness_python": self.edit_python.text().strip() or "py",
            "base_url": self.edit_url.text().strip(),
            "model": self.edit_model.text().strip(),
            "api_key": self.edit_key.text().strip(),
            "grant_dir": self.edit_grant.text().strip(),
        }
        save_agent_config(cfg)
        clear_harness_probe_cache()
        self.cfg = cfg
        self.accept()


ACCENT = "#0a84ff"
TEXT = "#1d1d1f"
SUBTLE = "#86868b"

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

CAT_EXTS = {
    "app": APP_EXTS,
    "doc": {
        "pdf", "doc", "docx", "docm", "dot", "dotx", "xls", "xlsx", "xlsm",
        "xlsb", "ppt", "pptx", "pptm", "odt", "ods", "odp", "txt", "text",
        "md", "markdown", "rtf", "csv", "tsv", "epub", "mobi", "log", "ini",
        "cfg", "wps", "xps", "one", "vsd",
    },
    "img": {
        "jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "heic", "heif",
        "tif", "tiff", "ico", "psd", "ai", "raw", "cr2", "cr3", "nef",
        "arw", "dng", "orf", "rw2", "avif", "jfif",
    },
    "vid": {
        "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg",
        "mpeg", "3gp", "ts", "mts", "m2ts", "vob", "ogv",
    },
    "aud": {
        "mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus", "mid",
        "midi", "aiff", "alac", "amr",
    },
    "zip": {
        "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "iso", "cab",
        "tgz", "tbz2", "lz4",
    },
    "code": {
        "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "html", "htm", "css",
        "scss", "less", "json", "xml", "yml", "yaml", "toml", "java", "c",
        "cpp", "cc", "cxx", "h", "hpp", "cs", "go", "rb", "php", "sh",
        "bash", "bat", "cmd", "ps1", "psm1", "sql", "vbs", "lua", "rs",
        "swift", "kt", "kts", "dart", "vue", "svelte", "gradle", "cmake",
        "ipynb", "r", "pl",
    },
}

KIND_NAMES = {
    "pdf": "PDF Document", "doc": "Word Document", "docx": "Word Document",
    "xls": "Excel Spreadsheet", "xlsx": "Excel Spreadsheet",
    "ppt": "PowerPoint Presentation", "pptx": "PowerPoint Presentation",
    "txt": "Plain Text", "md": "Markdown Document", "csv": "CSV File",
    "jpg": "JPEG Image", "jpeg": "JPEG Image", "png": "PNG Image",
    "gif": "GIF Image", "webp": "WebP Image", "svg": "SVG Image",
    "heic": "HEIC Image", "mp4": "MP4 Video", "mov": "QuickTime Movie",
    "mkv": "Matroska Video", "avi": "AVI Video", "mp3": "MP3 Audio",
    "wav": "WAV Audio", "flac": "FLAC Audio", "zip": "ZIP Archive",
    "rar": "RAR Archive", "7z": "7z Archive", "iso": "Disc Image",
    "exe": "Application", "msi": "Windows Installer", "dll": "System Library",
    "py": "Python Script", "js": "JavaScript File", "ts": "TypeScript File",
    "json": "JSON File", "html": "HTML Document", "css": "CSS File",
    "bat": "Batch File", "ps1": "PowerShell Script", "sql": "SQL File",
}


def ext_of(name):
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


# Extensions whose content we can show as a text snippet in the details pane
TEXT_EXTS = {
    "txt", "text", "md", "markdown", "log", "ini", "cfg", "conf", "csv",
    "tsv", "json", "xml", "yml", "yaml", "toml", "py", "js", "ts", "tsx",
    "jsx", "html", "htm", "css", "scss", "c", "cpp", "h", "hpp", "cs", "go",
    "rb", "php", "sh", "bat", "ps1", "sql", "lua", "rs", "swift", "kt",
    "java", "r", "pl", "srt", "vtt",
}


def category_for(name, is_dir):
    if is_dir:
        return "folder"
    ext = ext_of(name)
    for cat, exts in CAT_EXTS.items():
        if ext in exts:
            return cat
    return "other"


def kind_for(name, is_dir):
    if is_dir:
        return "Folder"
    ext = ext_of(name)
    if ext in KIND_NAMES:
        return KIND_NAMES[ext]
    return ext.upper() + " File" if ext else "File"


def human_size(n):
    if n is None or n < 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{n} B"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return "—"


def fmt_date(mtime):
    try:
        return time.strftime("%d %b %Y", time.localtime(mtime))
    except (ValueError, OverflowError):
        return "—"


def join_path(parent, name):
    return parent + "\\" + name if parent else name


def split_path(path):
    if len(path) == 2 and path[1] == ":":
        return "", path
    i = path.rfind("\\")
    if i < 0:
        return "", path
    return path[:i], path[i + 1:]


def real_dir(path):
    return path + "\\" if len(path) == 2 and path[1] == ":" else path


def like_escape(s, esc="#"):
    return s.replace(esc, esc + esc).replace("%", esc + "%").replace("_", esc + "_")


# ---------------------------------------------------------------------------
# Windows shell helpers
# ---------------------------------------------------------------------------

SKIP_DIR_NAMES = {
    "$recycle.bin", "system volume information", "$winreagent",
    "config.msi", "recovery",
}
SKIP_FILE_NAMES = {"hiberfil.sys", "pagefile.sys", "swapfile.sys",
                   "dumpstack.log", "dumpstack.log.tmp"}
REPARSE_ATTR = 0x400


def fixed_drives():
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    drives = []
    for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if mask >> i & 1:
            root = letter + ":\\"
            if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:  # DRIVE_FIXED
                drives.append(root)
    return drives or ["C:\\"]


def drive_label(root):
    name = ctypes.create_unicode_buffer(261)
    k32 = ctypes.windll.kernel32
    ok = k32.GetVolumeInformationW(ctypes.c_wchar_p(root), name, 261, None, None, None, None, 0)
    label = name.value.strip() if ok and name.value.strip() else "Local Disk"
    return f"{label} ({root[:2]})"


def shell_folders():
    """Resolve the user's real Desktop/Documents/... paths (handles OneDrive redirection)."""
    import winreg
    GUIDS = {
        "Desktop": "Desktop",
        "Personal": "Documents",
        "{374DE290-123F-4565-9164-39C4925E467B}": "Downloads",
        "My Pictures": "Pictures",
        "My Music": "Music",
        "My Video": "Videos",
    }
    home = os.path.expanduser("~")
    out = {}
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        )
        with key:
            for regname, nice in GUIDS.items():
                try:
                    val, _ = winreg.QueryValueEx(key, regname)
                    out[nice] = val
                except OSError:
                    pass
    except OSError:
        pass
    fallbacks = {
        "Desktop": os.path.join(home, "Desktop"),
        "Documents": os.path.join(home, "Documents"),
        "Downloads": os.path.join(home, "Downloads"),
        "Pictures": os.path.join(home, "Pictures"),
        "Music": os.path.join(home, "Music"),
        "Videos": os.path.join(home, "Videos"),
    }
    for nice, path in fallbacks.items():
        out.setdefault(nice, path)
    order = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"]
    return [(n, out[n]) for n in order if out.get(n)]


def reveal_in_explorer(path):
    try:
        subprocess.Popen(["explorer", "/select," + path])
    except OSError:
        subprocess.Popen(["explorer", real_dir(path) if len(path) <= 3 else path])


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def recycle_path(path):
    """Send a file/folder to the Recycle Bin."""
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT = 3, 0x40, 0x10, 0x4
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = path + "\0"
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return res == 0 and not op.fAnyOperationsAborted


def show_properties(path):
    """Open the native Explorer Properties dialog."""
    import ctypes as c

    class SHELLEXECUTEINFOW(c.Structure):
        _fields_ = [
            ("cbSize", c.c_ulong), ("fMask", c.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", c.c_wchar_p), ("lpFile", c.c_wchar_p),
            ("lpParameters", c.c_wchar_p), ("lpDirectory", c.c_wchar_p),
            ("nShow", c.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", c.c_void_p), ("lpClass", c.c_wchar_p),
            ("hkeyClass", wintypes.HKEY), ("dwHotKey", c.c_ulong),
            ("hIconOrMonitor", wintypes.HANDLE),
        ]

    SEE_MASK_INVOKEIDLIST = 0x0000000C
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = c.sizeof(sei)
    sei.fMask = SEE_MASK_INVOKEIDLIST
    sei.lpVerb = "properties"
    sei.lpFile = path
    sei.nShow = 1
    try:
        return bool(ctypes.windll.shell32.ShellExecuteExW(c.byref(sei)))
    except Exception:
        return False


def _hbitmap_to_qimage(hbm):
    """Copy a GDI HBITMAP into a QImage (32bpp, alpha forced opaque if absent)."""
    from ctypes import byref, c_long, c_void_p, c_ushort

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", c_long), ("bmWidth", c_long), ("bmHeight", c_long),
                    ("bmWidthBytes", c_long), ("bmPlanes", c_ushort),
                    ("bmBitsPixel", c_ushort), ("bmBits", c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_ulong), ("biWidth", c_long),
                    ("biHeight", c_long), ("biPlanes", c_ushort),
                    ("biBitCount", c_ushort), ("biCompression", ctypes.c_ulong),
                    ("biSizeImage", ctypes.c_ulong), ("biXPelsPerMeter", c_long),
                    ("biYPelsPerMeter", c_long), ("biClrUsed", ctypes.c_ulong),
                    ("biClrImportant", ctypes.c_ulong)]

    gdi32 = ctypes.windll.gdi32
    bm = BITMAP()
    if not gdi32.GetObjectW(hbm, ctypes.sizeof(bm), byref(bm)):
        return None
    w, h = bm.bmWidth, bm.bmHeight
    if w <= 0 or h <= 0 or w * h > 4096 * 4096:
        return None
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h  # top-down
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    hdc = gdi32.CreateCompatibleDC(None)
    buf = ctypes.create_string_buffer(w * h * 4)
    try:
        if gdi32.GetDIBits(hdc, hbm, 0, h, buf, byref(bi), 0) != h:
            return None
    finally:
        gdi32.DeleteDC(hdc)
    raw = bytearray(buf.raw)
    if not any(raw[3::4]):  # no alpha in the bitmap — make it opaque
        raw[3::4] = b"\xff" * len(raw[3::4])
    return QImage(bytes(raw), w, h, w * 4, QImage.Format_ARGB32).copy()


def shell_thumbnail(path, maxdim=512):
    """Explorer-style thumbnail (works for videos, images, documents).
    Returns a QImage or None. Call from a background thread only."""
    from ctypes import byref, c_long, c_void_p, c_wchar_p

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8)]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", c_long), ("cy", c_long)]

    GetImageProto = ctypes.WINFUNCTYPE(
        ctypes.c_long, c_void_p, SIZE, ctypes.c_uint, ctypes.POINTER(c_void_p))
    ReleaseProto = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)

    path = os.path.normpath(path.replace("/", "\\"))
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)  # MTA is fine for shell thumbnails
    pp = c_void_p()
    funcs = None
    try:
        # IID_IShellItemImageFactory {BCC18B79-BA16-442F-80C4-8A59C30C463B}
        iid = GUID(0xBCC18B79, 0xBA16, 0x442F,
                   (ctypes.c_ubyte * 8)(0x80, 0xC4, 0x8A, 0x59, 0xC3, 0x0C, 0x46, 0x3B))
        hr = ctypes.windll.shell32.SHCreateItemFromParsingName(
            c_wchar_p(path), None, byref(iid), byref(pp))
        if hr < 0 or not pp.value:
            return None
        vtbl_ptr = ctypes.cast(pp, ctypes.POINTER(c_void_p)).contents
        funcs = ctypes.cast(vtbl_ptr, ctypes.POINTER(c_void_p * 4)).contents
        get_image = GetImageProto(funcs[3])
        hbm = c_void_p()
        SIIGBF_BIGGERSIZEOK, SIIGBF_THUMBNAILONLY = 0x1, 0x4
        size = SIZE(maxdim, maxdim)
        hr = get_image(pp, size, SIIGBF_BIGGERSIZEOK | SIIGBF_THUMBNAILONLY, byref(hbm))
        if hr < 0 or not hbm.value:  # no thumbnail: fall back to the file icon
            hr = get_image(pp, size, SIIGBF_BIGGERSIZEOK, byref(hbm))
            if hr < 0 or not hbm.value:
                return None
        img = _hbitmap_to_qimage(hbm.value)
        ctypes.windll.gdi32.DeleteObject(hbm)
        return img
    except Exception:
        return None
    finally:
        if pp.value and funcs:
            try:
                ReleaseProto(funcs[2])(pp)
            except Exception:
                pass
        ole32.CoUninitialize()


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

FTS_OK = False
_tls = threading.local()


def db_connect():
    os.makedirs(DB_DIR, exist_ok=True)
    deadline = time.time() + 180
    while True:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-65536")
            conn.execute("PRAGMA busy_timeout=30000")
            _ensure_schema(conn)
            return conn
        except sqlite3.OperationalError as exc:
            try:
                conn.close()
            except Exception:
                pass
            if "locked" not in str(exc) or time.time() > deadline:
                raise
            time.sleep(1)


def thread_conn():
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = db_connect()
        _tls.conn = conn
    return conn


def _ensure_schema(conn):
    """Tables and the UNIQUE index only — this runs on EVERY
    connection, so it must stay instant. The performance indexes are
    built once by ensure_indexes() (indexer thread): building one
    over millions of rows here would hold the write lock while every
    other connection queues behind it and dies on 'database is
    locked'."""
    global FTS_OK
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY,
            parent TEXT NOT NULL,
            name TEXT NOT NULL,
            is_dir INTEGER NOT NULL,
            ext TEXT,
            size INTEGER,
            mtime REAL,
            drive TEXT,
            category TEXT,
            kind TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_files_key ON files(parent, name);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS note_links(
            parent TEXT NOT NULL,
            name TEXT NOT NULL,
            mtime REAL NOT NULL,
            target TEXT NOT NULL
        );
    """)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5("
            "name, content='files', content_rowid='id', tokenize='trigram')"
        )
        conn.execute("SELECT count(*) FROM fts")
        FTS_OK = True
    except sqlite3.Error:
        FTS_OK = False
    conn.commit()


def ensure_indexes(conn):
    """Performance indexes, built once per database by the indexer
    thread before it scans. IF NOT EXISTS keeps later calls cheap."""
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent);
        CREATE INDEX IF NOT EXISTS idx_files_parent_nc ON files(parent COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_files_drive ON files(drive);
        CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime);
        CREATE INDEX IF NOT EXISTS idx_files_cat ON files(category);
        CREATE INDEX IF NOT EXISTS idx_links_key ON note_links(parent, name);
    """)
    conn.commit()


def rebuild_fts(conn):
    global FTS_OK
    if not FTS_OK:
        return
    conn.execute("DROP TABLE IF EXISTS fts")
    conn.execute(
        "CREATE VIRTUAL TABLE fts USING fts5("
        "name, content='files', content_rowid='id', tokenize='trigram')"
    )
    conn.execute("INSERT INTO fts(rowid, name) SELECT id, name FROM files")


def fts_touch(conn, rowid, name):
    if not FTS_OK:
        return
    conn.execute("DELETE FROM fts WHERE rowid=?", (rowid,))
    conn.execute("INSERT INTO fts(rowid, name) VALUES(?,?)", (rowid, name))


def fts_drop_ids(conn, ids):
    if not FTS_OK or not ids:
        return
    conn.executemany("DELETE FROM fts WHERE rowid=?", [(i,) for i in ids])


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, str(value)))


def update_total(conn):
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    set_meta(conn, "total", total)
    conn.commit()
    return total


def insert_rows(conn, rows, sync_fts=True):
    """rows: (parent, name, is_dir, ext, size, mtime, drive, category, kind)"""
    if not rows:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO files(parent,name,is_dir,ext,size,mtime,drive,category,kind)"
        " VALUES(?,?,?,?,?,?,?,?,?)", rows)
    if sync_fts and FTS_OK:
        conn.executemany(
            "INSERT INTO fts(rowid, name)"
            " SELECT id, name FROM files WHERE parent=? AND name=?",
            [(r[0], r[1]) for r in rows])
        # De-duplicate defensively: a rowid that somehow got indexed twice.
        conn.execute("DELETE FROM fts WHERE rowid NOT IN (SELECT id FROM files)")


def delete_tree(conn, path):
    """Remove an item (file or directory) and all descendants from the index."""
    if len(path) == 2 and path[1] == ":":
        ids = [r[0] for r in conn.execute("SELECT id FROM files WHERE drive=?", (path,))]
        conn.execute("DELETE FROM note_links WHERE parent=? OR parent LIKE ? ESCAPE '#'",
                     (path, like_escape(path) + "\\%"))
    else:
        parent, name = split_path(path)
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM files WHERE parent=? OR parent LIKE ? ESCAPE '#'"
            " OR (parent=? AND name=?)",
            (path, like_escape(path) + "\\%", parent, name))]
        conn.execute(
            "DELETE FROM note_links WHERE (parent=? AND name=?) OR parent=?"
            " OR parent LIKE ? ESCAPE '#'",
            (parent, name, path, like_escape(path) + "\\%"))
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        fts_drop_ids(conn, chunk)
        conn.executemany("DELETE FROM files WHERE id=?", [(c,) for c in chunk])
    conn.commit()


ROW_FIELDS = "parent, name, is_dir, size, mtime, category, kind"
ROW_FIELDS_F = ("files.parent, files.name, files.is_dir, files.size, "
                "files.mtime, files.category, files.kind")


def _row_dict(r):
    return {
        "parent": r[0], "name": r[1], "is_dir": r[2], "size": r[3],
        "mtime": r[4], "category": r[5], "kind": r[6],
    }


def _unique_dest(folder, name):
    """A destination path that never overwrites: 'name (2).ext',
    'name (3).ext', ... (Explorer-style)."""
    dest = join_path(folder, name)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(name)
    n = 2
    while True:
        cand = join_path(folder, f"{stem} ({n}){ext}")
        if not os.path.exists(cand):
            return cand
        n += 1

def index_add_path(conn, path):
    """Insert a file, or a folder and its whole subtree, into the
    index using the scanner's own row builder."""
    st = os.stat(path)
    parent = os.path.dirname(path)
    if not os.path.isdir(path):
        insert_rows(conn, [_make_row(parent, os.path.basename(path),
                                     False, st, path[:2])])
        return
    rows = [_make_row(parent, os.path.basename(path), True, st,
                      path[:2])]
    for base, dirs, files in os.walk(path):
        for nm in dirs + files:
            p = os.path.join(base, nm)
            try:
                s2 = os.stat(p)
            except OSError:
                continue
            rows.append(_make_row(base, nm, os.path.isdir(p), s2,
                                  path[:2]))
    insert_rows(conn, rows)


def sort_rows(rows, mode):
    """Client-side ordering for the list view (fetch is capped at 600)."""
    if mode == "name":
        return sorted(rows, key=lambda r: (not r["is_dir"],
                                           r["name"].lower()))
    if mode == "newest":
        return sorted(rows, key=lambda r: (-(r["mtime"] or 0),
                                           r["name"].lower()))
    if mode == "oldest":
        return sorted(rows, key=lambda r: ((r["mtime"] or 0),
                                           r["name"].lower()))
    if mode == "largest":
        return sorted(rows, key=lambda r: (r["is_dir"],
                                           -(r["size"] or 0),
                                           r["name"].lower()))
    if mode == "smallest":
        return sorted(rows, key=lambda r: (r["is_dir"],
                                           (r["size"] or 0),
                                           r["name"].lower()))
    return rows


def do_search(conn, query, place=None, cat="all", limit=600):
    """Returns (rows, note). Empty query = browse the current place by date."""
    q = (query or "").strip()
    conds, args = [], []
    if place:
        p = place.rstrip("\\")
        if q:
            # searching within a place: everything beneath it
            conds.append("(files.parent = ? COLLATE NOCASE OR files.parent LIKE ? ESCAPE '#')")
            args += [p, like_escape(p) + "\\%"]
        else:
            # browsing a place: its direct children only
            conds.append("files.parent = ? COLLATE NOCASE")
            args.append(p)
    if cat and cat != "all":
        conds.append("files.category = ?")
        args.append(cat)
    base = " FROM files"
    if q and FTS_OK and len(q) >= 3:
        base = " FROM fts JOIN files ON files.id = fts.rowid"
    conds_sql = (" WHERE " + " AND ".join(conds)) if conds else ""

    rows = []
    FETCH_CAP = 20000  # hard ceiling before in-Python ranking
    if q:
        if base.startswith(" FROM fts"):
            m = '"' + q.replace('"', '""') + '"'
            try:
                cur = conn.execute("SELECT " + ROW_FIELDS_F + base + conds_sql +
                                   (" AND " if conds else " WHERE ") + "fts.name MATCH ?" +
                                   " LIMIT ?",
                                   args + [m, FETCH_CAP])
                rows = [_row_dict(r) for r in cur.fetchall()]
            except sqlite3.Error:
                rows = []
            if not rows:
                # trigram found nothing (rare) — fall back to plain LIKE
                base = " FROM files"
                cur = conn.execute("SELECT " + ROW_FIELDS + base + conds_sql +
                                   (" AND " if conds else " WHERE ") +
                                   "files.name LIKE ? ESCAPE '#' LIMIT ?",
                                   args + ["%" + like_escape(q) + "%", FETCH_CAP])
                rows = [_row_dict(r) for r in cur.fetchall()]
        else:
            cur = conn.execute("SELECT " + ROW_FIELDS + base + conds_sql +
                               (" AND " if conds else " WHERE ") +
                               "files.name LIKE ? ESCAPE '#' LIMIT ?",
                               args + ["%" + like_escape(q) + "%", FETCH_CAP])
            rows = [_row_dict(r) for r in cur.fetchall()]
    else:
        # browsing a place = its direct children, folders first (Finder-style);
        # browsing All Files = most recently modified across the whole index
        if place:
            order = " ORDER BY files.is_dir DESC, files.name COLLATE NOCASE ASC LIMIT ?"
        else:
            order = " ORDER BY files.mtime DESC LIMIT ?"
        cur = conn.execute("SELECT " + ROW_FIELDS + base + conds_sql + order,
                           args + [limit])
        rows = [_row_dict(r) for r in cur.fetchall()]

    if q:
        def rank(r):
            n = r["name"].lower()
            if n == q.lower():
                g = 0
            elif n.startswith(q.lower()):
                g = 1
            else:
                g = 2
            return (g, len(n), -(r["mtime"] or 0))
        rows.sort(key=rank)
        truncated = len(rows) >= limit
        rows = rows[:limit]
        note = "search"
    else:
        truncated = len(rows) >= limit
        note = "browse"
    return rows, ("truncated" if truncated else "")


# ---------------------------------------------------------------------------
# Indexing engine
# ---------------------------------------------------------------------------

BATCH = 4000
# NTFS stores 100ns mtimes; both reads see the identical double, so compare
# with microsecond tolerance. Anything looser silently misses rapid changes.
MTIME_EPS = 1e-6


def _entry_skipped(name, is_dir):
    low = name.lower()
    return (low in SKIP_DIR_NAMES) if is_dir else (low in SKIP_FILE_NAMES)


def _make_row(parent, name, is_dir, st, drive):
    return (parent, name, 1 if is_dir else 0, ext_of(name),
            st.st_size if not is_dir else 0, st.st_mtime, drive,
            category_for(name, is_dir), kind_for(name, is_dir))


def scan_full(conn, roots, progress=None, should_run=None, wipe=True):
    """Rebuild the index from the given roots. wipe=False indexes extra roots
    without erasing the rest of the catalog (used for newly-appeared drives)."""
    if should_run is None:
        should_run = lambda: True
    if wipe:
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM note_links")
        rebuild_fts(conn)
        conn.commit()
    total = 0
    batch = []
    for root in roots:
        if not should_run():
            break
        drive = root[:2]
        try:
            root_st = os.stat(real_dir(root))
        except OSError:
            continue
        stack = []
        if len(root) == 3 and root.endswith("\\"):
            row = _make_row("", drive, True, root_st, drive)
            batch.append(row)
        stack.append(root.rstrip("\\"))
        while stack and should_run():
            d = stack.pop()
            real = real_dir(d)
            try:
                it = os.scandir(real)
            except OSError:
                continue
            with it:
                for e in it:
                    if not should_run():
                        break
                    name = e.name
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    is_dir = e.is_dir(follow_symlinks=False)
                    if is_dir:
                        if getattr(st, "st_file_attributes", 0) & REPARSE_ATTR:
                            continue  # junction / symlink dir: never recurse
                        if _entry_skipped(name, True):
                            continue
                        try:
                            # Directory enumeration can report a stale (lagged)
                            # mtime; os.stat is authoritative, and dirs are
                            # rare enough that the extra syscall is free.
                            st = os.stat(e.path, follow_symlinks=False)
                        except OSError:
                            continue
                    else:
                        if _entry_skipped(name, False):
                            continue
                        if os.path.abspath(e.path).lower() == DB_PATH.lower():
                            continue
                    batch.append(_make_row(d, name, is_dir, st, drive))
                    if is_dir:
                        stack.append(join_path(d, name))
                    if len(batch) >= BATCH:
                        total += len(batch)
                        insert_rows(conn, batch, sync_fts=False)
                        conn.commit()
                        if progress:
                            progress(d, total)
                        batch = []
        if batch:
            total += len(batch)
            insert_rows(conn, batch, sync_fts=False)
            batch = []
        conn.commit()
        if progress:
            progress(root, total)
    if should_run():
        rebuild_fts(conn)  # index all names in one pass
        conn.commit()
        set_meta(conn, "phase", "full_done")
        conn.commit()


def scan_delta(conn, roots, progress=None, should_run=None):
    """Incremental scan: only re-read directories whose mtime changed."""
    if should_run is None:
        should_run = lambda: True
    changed_items = 0
    for root in roots:
        if not should_run():
            break
        drive = root[:2]
        existing = {}
        for parent, name, mtime, in conn.execute(
                "SELECT parent, name, mtime FROM files WHERE drive=? AND is_dir=1", (drive,)):
            existing[join_path(parent, name)] = mtime
        if not existing:
            scan_full(conn, [root], progress, should_run, wipe=False)
            continue
        stack = [root.rstrip("\\")]
        visited = set()
        while stack and should_run():
            d = stack.pop()
            if d in visited:
                continue
            visited.add(d)
            real = real_dir(d)
            try:
                cur_mtime = os.stat(real).st_mtime
            except OSError:
                if os.path.exists(real):
                    continue  # permission problem, not a deletion — keep the index
                delete_tree(conn, d)
                continue
            old_mtime = existing.get(d)
            if old_mtime is not None and abs(cur_mtime - old_mtime) < MTIME_EPS:
                # unchanged — keep walking into known child dirs
                for parent, name in conn.execute(
                        "SELECT parent, name FROM files WHERE parent=? AND is_dir=1", (d,)):
                    stack.append(join_path(parent, name))
                continue

            # changed or new directory: rescan its direct children
            prev = {}
            for row in conn.execute(
                    "SELECT id, name, is_dir, size, mtime FROM files WHERE parent=?", (d,)):
                prev[row[1]] = (row[0], row[2], row[3], row[4])
            conn.execute("UPDATE files SET mtime=? WHERE parent=? AND name=?",
                         (cur_mtime, *split_path(d)))
            seen = set()
            new_rows = []
            try:
                it = os.scandir(real)
            except OSError:
                if not os.path.exists(real):
                    delete_tree(conn, d)
                continue
            with it:
                for e in it:
                    if not should_run():
                        break
                    name = e.name
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    is_dir = e.is_dir(follow_symlinks=False)
                    if is_dir:
                        if getattr(st, "st_file_attributes", 0) & REPARSE_ATTR:
                            continue
                        if _entry_skipped(name, True):
                            continue
                        try:
                            # match scan_full: authoritative mtime for dirs
                            st = os.stat(e.path, follow_symlinks=False)
                        except OSError:
                            continue
                    else:
                        if _entry_skipped(name, False):
                            continue
                        if os.path.abspath(e.path).lower() == DB_PATH.lower():
                            continue
                    seen.add(name)
                    old = prev.get(name)
                    if old and old[1] == (1 if is_dir else 0):
                        if is_dir or (old[2] == st.st_size and abs(old[3] - st.st_mtime) < MTIME_EPS):
                            if is_dir:
                                stack.append(join_path(d, name))  # still check deeper levels
                            continue
                    if old:
                        if old[1]:
                            # was a folder: remove its whole subtree, not
                            # just the row, or its contents stay as orphans
                            delete_tree(conn, join_path(d, name))
                        else:
                            fts_drop_ids(conn, [old[0]])
                            conn.execute("DELETE FROM files WHERE id=?", (old[0],))
                    new_rows.append(_make_row(d, name, is_dir, st, drive))
                    changed_items += 1
            for name, old in prev.items():
                if name not in seen:
                    if old[1]:
                        # deleted folder: its contents must go too
                        delete_tree(conn, join_path(d, name))
                    else:
                        fts_drop_ids(conn, [old[0]])
                        conn.execute("DELETE FROM files WHERE id=?", (old[0],))
                    changed_items += 1
            if new_rows:
                insert_rows(conn, new_rows)
                changed_items += len(new_rows)
                for r in new_rows:
                    if r[2]:
                        stack.append(join_path(d, r[1]))
            conn.commit()
            if progress:
                progress(d, changed_items)


# ---------------------------------------------------------------------------
# Obsidian vault graph (data side)
# ---------------------------------------------------------------------------

WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


def parse_wikilinks(text):
    """Extract wikilink targets: [[Note]], [[Note|alias]], [[Note#Heading]]."""
    targets = []
    for m in WIKILINK_RE.finditer(text):
        t = m.group(1).split("|", 1)[0].split("#", 1)[0].split("^", 1)[0].strip()
        if t:
            targets.append(t)
    return targets


def active_vault_for(place):
    """The saved vault that contains this place (or None)."""
    if not place:
        return None
    p = place.rstrip("\\").lower()
    for v in get_vaults():
        vp = v.rstrip("\\")
        vl = vp.lower()
        if p == vl or p.startswith(vl + "\\"):
            return vp
    return None


def pending_link_count(conn, vaults):
    """Markdown notes whose [[wikilinks]] have not been parsed yet (or whose
    mtime changed since they were). Empty list → 0."""
    vaults = [v.rstrip("\\") for v in (vaults or []) if v]
    if not vaults:
        return 0
    cond_parts, args = [], []
    for v in vaults:
        cond_parts.append("(f.parent = ? COLLATE NOCASE OR f.parent LIKE ? ESCAPE '#')")
        args += [v, like_escape(v) + "\\%"]
    row = conn.execute(
        "SELECT COUNT(*) FROM files f LEFT JOIN note_links l"
        " ON l.parent = f.parent COLLATE NOCASE AND l.name = f.name COLLATE NOCASE"
        " AND l.mtime = f.mtime"
        " WHERE f.ext = 'md' AND l.name IS NULL AND (" + " OR ".join(cond_parts) + ")",
        args).fetchone()
    return row[0] if row else 0


def analyze_pending_links(conn, vaults, time_budget=6.0, progress=None):
    """Parse [[wikilinks]] out of not-yet-analyzed markdown notes and store
    them in note_links. Runs at most time_budget seconds per call (cloud
    placeholders can be slow); unchanged notes are never re-read. A note with
    zero links gets an empty-target marker row so it is not re-read forever.
    Returns (analyzed, still_pending)."""
    vaults = [v.rstrip("\\") for v in (vaults or []) if v]
    if not vaults:
        return 0, 0
    cond_parts, args = [], []
    for v in vaults:
        cond_parts.append("(f.parent = ? COLLATE NOCASE OR f.parent LIKE ? ESCAPE '#')")
        args += [v, like_escape(v) + "\\%"]
    rows = conn.execute(
        "SELECT f.parent, f.name, f.mtime FROM files f LEFT JOIN note_links l"
        " ON l.parent = f.parent COLLATE NOCASE AND l.name = f.name COLLATE NOCASE"
        " AND l.mtime = f.mtime"
        " WHERE f.ext = 'md' AND l.name IS NULL AND (" + " OR ".join(cond_parts) + ")",
        args).fetchall()
    remaining = len(rows)
    t0 = time.time()
    analyzed = 0
    for parent, name, mtime in rows:
        if time.time() - t0 > time_budget:
            break
        targets = []
        try:
            with open(join_path(parent, name), "rb") as f:
                text = f.read(512_000).decode("utf-8", "replace")
            targets = parse_wikilinks(text)
        except OSError:
            # unreadable right now (cloud placeholder etc.) — stays pending
            if progress and analyzed % 25 == 0:
                progress(analyzed, remaining)
            continue
        conn.execute("DELETE FROM note_links WHERE parent=? AND name=?", (parent, name))
        conn.executemany(
            "INSERT INTO note_links VALUES(?,?,?,?)",
            [(parent, name, mtime, t) for t in targets] or [(parent, name, mtime, "")])
        analyzed += 1
        remaining -= 1
        if progress and analyzed % 25 == 0:
            progress(analyzed, remaining)
    conn.commit()
    return analyzed, remaining


def build_vault_graph(conn, vault, max_nodes=4000):
    """Obsidian-style graph with progressive disclosure: the displayed tree
    is the vault root + its top-level folders (bubbles, sized by note count)
    + notes that live at the root; deeper folders/notes arrive on demand via
    the child provider. link_edges holds resolved [src_path, dst_path] note
    links from the note_links table — no file reads, instant."""
    vp = vault.rstrip("\\")
    scope = "(f.parent = ? COLLATE NOCASE OR f.parent LIKE ? ESCAPE '#')"
    sargs = [vp, like_escape(vp) + "\\%"]
    rows = conn.execute(
        f"SELECT f.parent, f.name FROM files f WHERE {scope} AND f.ext = 'md'",
        sargs).fetchall()
    total = len(rows)
    rows = rows[:max_nodes]
    nodes = []
    for parent, name in rows:
        path = join_path(parent, name)
        nodes.append({
            "name": name[:-3] if name.lower().endswith(".md") else name,
            "file": name,
            "parent": parent, "path": path,
        })

    by_base, by_rel = {}, {}
    note_paths = []
    for i, (parent, name) in enumerate(rows):
        path = join_path(parent, name)
        note_paths.append(path)
        stem = name[:-3] if name.lower().endswith(".md") else name
        by_base.setdefault(stem.lower(), []).append(i)
        rel = os.path.relpath(path, vp)
        rstem = rel[:-3] if rel.lower().endswith(".md") else rel
        by_rel.setdefault(rel.lower(), []).append(i)
        by_rel.setdefault(rstem.lower(), []).append(i)

    def resolve(target, cur_parent):
        t = target.replace("/", "\\").strip().lower()
        if t.endswith(".md"):
            t = t[:-3]
        for key in (t, t + ".md"):
            lst = by_rel.get(key)
            if lst:
                for i in lst:  # same-folder notes win on ambiguous names
                    if note_paths[i].rsplit("\\", 1)[0].lower() == cur_parent.lower():
                        return i
                return lst[0]
        lst = by_base.get(t)
        if lst:
            for i in lst:
                if note_paths[i].rsplit("\\", 1)[0].lower() == cur_parent.lower():
                    return i
            return lst[0]
        return None

    link_edges = []
    seen = set()
    link_rows = conn.execute(
        "SELECT l.parent, l.name, l.target FROM note_links l JOIN files f"
        " ON f.parent = l.parent COLLATE NOCASE AND f.name = l.name COLLATE NOCASE"
        " WHERE l.target != '' AND f.ext = 'md' AND " + scope, sargs).fetchall()
    for lparent, lname, ltarget in link_rows:
        sp = join_path(lparent, lname).lower()
        j = resolve(ltarget, lparent)
        if j is None:
            continue
        dp = note_paths[j].lower()
        if sp == dp:
            continue
        key = tuple(sorted((sp, dp)))
        if key in seen:
            continue
        seen.add(key)
        link_edges.append([sp, dp])
    degree = {}
    for a, b in link_edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1

    # displayed tree: vault root + top-level folder bubbles + root notes
    nodes = [{"label": vp.rsplit("\\", 1)[-1] or vp, "kind": "folder",
              "category": "folder",
              "weight": total, "sub": f"{total:,} notes", "path": vp,
              "parent_path": None, "file": None, "has_children": True}]
    edges = []
    root_idx = 0
    for (name,) in conn.execute(
            "SELECT name FROM files WHERE parent = ? COLLATE NOCASE AND is_dir = 1"
            " ORDER BY name COLLATE NOCASE", (vp,)).fetchall():
        full = join_path(vp, name)
        fl = full.lower()
        n = sum(1 for p in note_paths if p.lower().startswith(fl + "\\"))
        nodes.append({"label": name, "kind": "folder", "category": "folder",
                      "weight": n, "sub": f"{n:,} notes", "path": full,
                      "parent_path": vp, "file": name, "has_children": n > 0})
        edges.append([root_idx, len(nodes) - 1])
    for parent, name in rows:
        if parent.lower() != vp.lower():
            continue
        stem = name[:-3] if name.lower().endswith(".md") else name
        p = join_path(parent, name)
        d = degree.get(p.lower(), 0)
        nodes.append({"label": stem, "kind": "note", "category": "doc",
                      "weight": d, "sub": f"{d} links" if d else None, "path": p,
                      "parent_path": vp, "file": name, "has_children": False})
        edges.append([root_idx, len(nodes) - 1])
    return {"nodes": nodes, "edges": edges, "mode": "vault",
            "link_edges": link_edges, "total_md": total, "vault": vp,
            "analyzed": total - pending_link_count(conn, [vp])}


def build_structure_graph(conn, place=None, roots=None, max_nodes=2500):
    """Folder-containment graph built purely from the index (no file reads,
    so it is instant even for whole drives). place=None → one node per fixed
    drive with two levels of folders; otherwise the place's own subtree,
    three levels deep, with its direct files as leaf nodes. Node 'weight' is
    the number of indexed items in the folder's subtree."""
    if place:
        roots = [place.rstrip("\\")]
        depth_cap = 3
    else:
        roots = [r.rstrip("\\") for r in (roots or fixed_drives())]
        depth_cap = 2

    cond_parts, args = [], []
    for r in roots:
        cond_parts.append("(files.parent = ? COLLATE NOCASE OR files.parent LIKE ? ESCAPE '#')")
        args += [r, like_escape(r) + "\\%"]
    where = "(" + " OR ".join(cond_parts) + ")"

    # direct item count per directory
    counts = {}
    for parent, cnt in conn.execute(
            f"SELECT parent, COUNT(*) FROM files WHERE {where} GROUP BY parent COLLATE NOCASE",
            args):
        counts[parent.lower()] = cnt

    kids = {}  # parent_path_lower -> [(name, full_lower, full_orig)]
    orig_of = {r.lower(): r for r in roots}
    for parent, name in conn.execute(
            f"SELECT parent, name FROM files WHERE {where} AND is_dir = 1", args):
        pl = parent.lower()
        full = join_path(parent, name)
        kids.setdefault(pl, []).append((name, full.lower(), full))

    # walk the directory tree (BFS keeps parents before children)
    order = []        # non-root dirs, BFS order (lower-case keys)
    parent_of = {}
    depth_of = {}
    dq = deque()
    for r in roots:
        for name, fl, fo in kids.get(r.lower(), []):
            dq.append((fl, fo, r.lower(), 1))
    seen = set()
    while dq:
        pl, po, par, depth = dq.popleft()
        if pl in seen:
            continue
        seen.add(pl)
        orig_of[pl] = po
        order.append(pl)
        parent_of[pl] = par
        depth_of[pl] = depth
        for name, fl, fo in kids.get(pl, []):
            if fl not in seen:
                dq.append((fl, fo, pl, depth + 1))

    # subtree totals: accumulate deepest-first
    total = {p.lower(): counts.get(p.lower(), 0) for p in roots}
    for pl in order:
        total[pl] = counts.get(pl, 0)
    for pl in reversed(order):
        par = parent_of.get(pl)
        if par is not None:
            total[par] = total.get(par, 0) + total[pl]

    # pick which dirs become nodes: every level-1 dir, then heavier dirs per
    # level until the budget is spent
    kept = {r.lower() for r in roots}
    budget = max_nodes
    by_level = {}
    for pl in order:
        by_level.setdefault(depth_of[pl], []).append(pl)
    for depth in range(1, depth_cap + 1):
        lvl = by_level.get(depth, [])
        lvl.sort(key=lambda pl: -total.get(pl, 0))
        room = max(0, budget - len(kept))
        for pl in lvl[:room]:
            kept.add(pl)
        if len(kept) >= budget:
            break

    # nodes: optional virtual root (multi-drive view), then roots, then kept dirs
    nodes = []
    idx_of = {}
    virtual = None
    if len(roots) > 1:
        tot = sum(total.get(r.lower(), 0) for r in roots)
        nodes.append({"label": "This PC", "kind": "drive", "weight": tot,
                      "sub": f"{tot:,} items", "path": None,
                      "parent_path": None, "file": None, "has_children": True})
        virtual = len(nodes) - 1
    for r in roots:
        rl = r.lower()
        if len(r) == 2:
            label, kind = drive_label(r + "\\"), "drive"
        else:
            label, kind = r.rsplit("\\", 1)[-1] or r, "folder"
        idx_of[rl] = len(nodes)
        nodes.append({"label": label, "kind": kind, "weight": total.get(rl, 0),
                      "sub": f"{total.get(rl, 0):,} items", "path": r,
                      "parent_path": None, "file": None, "has_children": True})
    for depth in range(1, depth_cap + 1):
        for pl in sorted((p for p in kept if depth_of.get(p) == depth),
                         key=lambda p: orig_of[p].lower()):
            po = orig_of[pl]
            idx_of[pl] = len(nodes)
            nodes.append({"label": po.rsplit("\\", 1)[-1], "kind": "folder",
                          "category": "folder",
                          "weight": total.get(pl, 0),
                          "sub": f"{total.get(pl, 0):,} items", "path": po,
                          "parent_path": orig_of.get(parent_of.get(pl, "")),
                          "file": po.rsplit("\\", 1)[-1],
                          "has_children": counts.get(pl, 0) > 0})

    # edges: each kept dir connects to its nearest kept ancestor
    edges = []
    for pl in idx_of:
        if pl in {r.lower() for r in roots}:
            if virtual is not None:
                edges.append((virtual, idx_of[pl]))
            continue
        anc = parent_of.get(pl)
        while anc is not None and anc not in idx_of:
            anc = parent_of.get(anc)
        if anc is not None:
            edges.append((idx_of[anc], idx_of[pl]))

    # direct files of each root as leaf nodes (suppressed on huge trees so
    # drive-wide diagrams stay readable)
    truncated = False
    if len(order) <= 400:
        for r in roots:
            ri = idx_of[r.lower()]
            rows = conn.execute(
                "SELECT name, size FROM files WHERE parent = ? COLLATE NOCASE AND is_dir = 0"
                " ORDER BY name COLLATE NOCASE LIMIT 250", (r,)).fetchall()
            for name, size in rows:
                idx = len(nodes)
                nodes.append({"label": name, "kind": "file",
                              "category": category_for(name, False),
                              "weight": size or 0,
                              "sub": human_size(size), "path": join_path(r, name),
                              "parent_path": r, "file": name, "has_children": False})
                edges.append((ri, idx))
            if len(rows) == 250:
                truncated = True

    return {"nodes": nodes, "edges": edges, "mode": "structure",
            "roots": roots, "truncated_files": truncated}


# ---------------------------------------------------------------------------
# Painted icons (no external assets)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class SearchWorker(QThread):
    have = Signal(int, str, list, str)

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._job = None
        self._seq = 0
        self._alive = True

    def submit(self, q, place, cat):
        with self._lock:
            self._seq += 1
            self._job = (self._seq, q, place, cat)
            return self._seq

    def stop(self):
        self._alive = False

    def run(self):
        while self._alive:
            with self._lock:
                job = self._job
                self._job = None
            if job is None:
                self.msleep(20)
                continue
            seq, q, place, cat = job
            try:
                rows, note = do_search(thread_conn(), q, place, cat)
            except Exception as exc:  # keep the UI alive if a query fails
                rows, note = [], f"error: {exc}"
            self.have.emit(seq, q, rows, note)


class IndexerThread(QThread):
    progress = Signal(str, int)
    done = Signal(int, str)  # total, error-or-empty

    def __init__(self, rebuild=False):
        super().__init__()
        self.rebuild = rebuild
        self._stop = False

    def stop(self):
        self._stop = True

    def _run_once(self):
        conn = None
        try:
            conn = db_connect()
            ensure_indexes(conn)
            roots = fixed_drives()
            for v in get_vaults():  # vaults may live on drives we don't auto-scan
                if os.path.isdir(v):
                    rv = v.rstrip("\\")
                    if rv not in roots:
                        roots.append(rv)
            phase = get_meta(conn, "phase")

            def prog(path, count):
                self.progress.emit(path, count)

            def alive():
                return not self._stop

            if self.rebuild or phase != "full_done":
                scan_full(conn, roots, prog, alive)
            else:
                scan_delta(conn, roots, prog, alive)
            if self._stop:
                return -1
            total = update_total(conn)
            set_meta(conn, "indexed_at", str(time.time()))
            conn.commit()
            return total
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def run(self):
        for attempt in range(5):
            try:
                total = self._run_once()
                if self._stop:
                    self.done.emit(-1, "stopped")
                    return
                self.done.emit(total, "")
                return
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) and attempt < 4:
                    time.sleep(3)
                    continue
                self.done.emit(-1, str(exc))
                return
            except Exception as exc:
                self.done.emit(-1, str(exc))
                return


# ---------------------------------------------------------------------------
# Frameless mac-style window
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Results list
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Vault graph view (interactive force-directed rendering)
# ---------------------------------------------------------------------------

class GraphNode(QGraphicsObject):
    KIND_COLORS = {
        "drive": (QColor("#9aa0a7"), QColor("#787e86")),
        "folder": (QColor("#b9bfc6"), QColor("#989ea6")),
        "file": (QColor("#cdd2d7"), QColor("#b0b6bc")),
        "note": (QColor("#b9bfc6"), QColor("#989ea6")),
    }

    def __init__(self, view, index, label, radius, parent_path, file_name, path,
                 kind="note", sub=None):
        super().__init__()
        self.view = view
        self.index = index
        self.label = label
        self.radius = radius
        self.parent_path = parent_path
        self.file = file_name
        self.path = path
        self.kind = kind
        self.sub = sub
        self.show_label = True
        self.can_expand = False
        self.icon_pm = None
        self.state = 0      # 0 normal, 1 neighbor/hover, 2 selected, 3 dimmed
        self._hover = False
        self._dragging = False
        self._press_pos = None
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setZValue(1)

    def boundingRect(self):
        show_text = self.show_label or self._hover or self.state == 2
        extra = (14 if self.sub else 0) + (20 if show_text else 0) + 10
        return QRectF(-118, -self.radius - 10, 236, self.radius * 2 + 10 + extra)

    def paint(self, p, option, widget):
        st = self.state
        if st == 0 and self._hover:
            st = 1
        if st == 0:
            fill, border = self.KIND_COLORS.get(self.kind, self.KIND_COLORS["note"])
            text = QColor("#6e6e73")
        elif st == 1:
            fill, border, text = QColor("#9ba2a9"), QColor("#7c828a"), QColor("#3a3a3c")
        elif st == 2:
            fill, border, text = QColor("#8b5cf6"), QColor("#7c3aed"), QColor("#6d28d9")
        else:
            fill, border, text = QColor("#dfe2e6"), QColor("#e4e7ea"), QColor("#c7c7cc")
        p.setBrush(fill)
        p.setPen(QPen(border, 1.4))
        p.drawEllipse(QPointF(0, 0), self.radius, self.radius)
        if self.icon_pm is not None and not self.icon_pm.isNull():
            d = self.radius * 2.0
            p.drawPixmap(QRectF(-self.radius, -self.radius, d, d),
                         self.icon_pm, QRectF(self.icon_pm.rect()))
        if self.can_expand:
            bx, by = self.radius * 0.82, -self.radius * 0.82
            p.setBrush(QColor("#8b5cf6"))
            p.setPen(QPen(QColor("#7c3aed"), 1))
            p.drawEllipse(QPointF(bx, by), 6.5, 6.5)
            p.setPen(QPen(QColor("#ffffff"), 1.5))
            p.drawLine(QPointF(bx - 3, by), QPointF(bx + 3, by))
            p.drawLine(QPointF(bx, by - 3), QPointF(bx, by + 3))
        if not (self.show_label or self._hover or self.state == 2):
            return
        fm = QFontMetrics(self.view.label_font)
        p.setPen(QPen(text, 1))
        p.drawText(QRectF(-110, self.radius + 3, 220, 14),
                   Qt.AlignHCenter | Qt.AlignTop,
                   fm.elidedText(self.label, Qt.ElideRight, 200))
        if self.sub:
            p.setPen(QPen(QColor("#a6acb2") if st != 3 else QColor("#d5d8dc"), 1))
            p.drawText(QRectF(-110, self.radius + 16, 220, 12),
                       Qt.AlignHCenter | Qt.AlignTop,
                       fm.elidedText(self.sub, Qt.ElideRight, 200))

    def hoverEnterEvent(self, e):
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._dragging = True
            self._press_pos = e.pos()
            self.view.kick()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        moved = (self._press_pos is not None and e.button() == Qt.LeftButton
                 and (e.pos() - self._press_pos).manhattanLength() > 4)
        self._dragging = False
        self._press_pos = None
        super().mouseReleaseEvent(e)
        if not moved and e.button() == Qt.LeftButton:
            self.view.select_node(self)

    def mouseDoubleClickEvent(self, e):
        self.view.node_activated.emit(self)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and not self.view._simulating:
            self.view.update_edges_for(self.index)
        return super().itemChange(change, value)

    def contextMenuEvent(self, event):
        event.accept()
        self.view.show_node_menu.emit(self, event.screenPos())


class GraphView(QGraphicsView):
    node_clicked = Signal(object)
    node_activated = Signal(object)
    show_node_menu = Signal(object, object)

    CELL = 120.0
    CUTOFF = 300.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing)
        self.setBackgroundBrush(QColor("#ffffff"))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.label_font = QFont()
        self.label_font.setPointSize(8)
        self.nodes = []
        self.edge_items = []   # (line_item, a, b)
        self.edge_pairs = []
        self.vx = []
        self.vy = []
        self.folder_of = []
        self.sel_index = None
        self._mode = "links"
        self._simulating = False
        # structure mode: progressive disclosure bookkeeping
        self.child_provider = None   # set by MainWindow (on-demand expansion)
        self._items = {}             # node idx -> visible GraphNode
        self._visible = set()
        self._tree_children = {}     # idx -> [visible child idxs]
        self._edge_lines = {}        # child idx -> its line to the parent
        self._parent_of = {}         # child idx -> parent idx (visible tree)
        self._data_nodes = []
        self._children_data = {}     # idx -> [child idxs] (built + on-demand)
        self._parent_of_data = {}
        self._data_key = {}          # (parent_lower, file_lower) -> node idx
        self._roots_idx = []
        self._link_src_dst = []      # vault mode: resolved note-link paths
        self._link_lines = []        # vault mode: (line_item, a_idx, b_idx)
        self._pm_cache = {}          # (category, size) -> QPixmap for nodes
        self._zoom = 1.0
        self._settle = 0
        self._fitted = False
        self._pan_from = None
        self._msg = None
        self._timer = QTimer(self)
        self._timer.setInterval(35)
        self._timer.timeout.connect(self._tick)
        self._float_timer = QTimer(self)
        self._float_timer.setInterval(50)
        self._float_timer.timeout.connect(self._float_tick)
        self._anchors = {}

    # -- data ----------------------------------------------------------------
    def clear_graph(self):
        self._timer.stop()
        self._float_timer.stop()
        self._anchors = {}
        self.nodes = []
        self.edge_items = []
        self.edge_pairs = []
        self.vx = []
        self.vy = []
        self.folder_of = []
        self.sel_index = None
        self._fitted = False
        self._settle = 0
        self._simulating = False
        self._items = {}
        self._visible = set()
        self._tree_children = {}
        self._edge_lines = {}
        self._parent_of = {}
        self._data_nodes = []
        self._children_data = {}
        self._parent_of_data = {}
        self._data_key = {}
        self._roots_idx = []
        self._link_src_dst = []
        self._link_lines = []
        self.scene().clear()
        if self._msg is not None:
            self._msg.deleteLater()
            self._msg = None

    def show_message(self, text):
        self.clear_graph()
        self._msg = QLabel(text, self.viewport())
        self._msg.setObjectName("graphMsg")
        self._msg.setWordWrap(True)
        self._msg.setAlignment(Qt.AlignCenter)
        self._msg.show()
        self._place_msg()

    def _place_msg(self):
        if self._msg is not None:
            self._msg.setGeometry(self.viewport().rect().adjusted(40, 40, -40, -40))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_msg()

    def set_graph(self, data):
        self.clear_graph()
        nodes, edges = data["nodes"], data["edges"]
        self._mode = data.get("mode", "links")
        if not nodes:
            self.show_message("Nothing to graph here yet.")
            return
        scene = self.scene()
        if self._mode in ("structure", "vault"):
            self._data_nodes = [dict(nd) for nd in nodes]
            incoming = set()
            for a, b in edges:
                self._children_data.setdefault(a, []).append(b)
                self._parent_of_data[b] = a
                incoming.add(b)
            self._roots_idx = [i for i in range(len(nodes)) if i not in incoming]
            for i, nd in enumerate(self._data_nodes):
                if nd.get("file"):
                    self._data_key[(nd.get("parent_path") or "").lower(),
                                   nd["file"].lower()] = i
            visible = set(self._roots_idx)
            for r in self._roots_idx:
                for c in self._children_data.get(r, []):
                    visible.add(c)
            for i in sorted(visible):
                self._add_visible(i, self._parent_of_data.get(i))
            self._link_src_dst = [(a.lower(), b.lower())
                                  for a, b in data.get("link_edges", [])] \
                if self._mode == "vault" else []
            self._apply_radial_layout()
            self._recompute_link_lines()
            return
        small = len(nodes) <= 120
        for i, nd in enumerate(nodes):
            r = 6.0 + min(12.0, (nd.get("degree", 0) ** 0.5) * 2.4)
            item = GraphNode(self, i, nd.get("label") or nd.get("name", ""), r,
                             nd.get("parent_path") or nd.get("parent"),
                             nd.get("file"), nd.get("path"),
                             nd.get("kind", "note"), nd.get("sub"))
            item.show_label = small or nd.get("degree", 0) >= 3
            item.setToolTip((nd.get("label") or nd.get("name", ""))
                            + (("\n" + nd["sub"]) if nd.get("sub") else ""))
            scene.addItem(item)
            self.nodes.append(item)
            self.vx.append(0.0)
            self.vy.append(0.0)
            self.folder_of.append((nd.get("parent_path") or nd.get("parent") or "").lower())
        pen = QPen(QColor("#e3e5e8"), 1.4)
        for a, b in edges:
            li = QGraphicsLineItem(0, 0, 0, 0)
            li.setPen(pen)
            li.setZValue(0)
            scene.addItem(li)
            self.edge_items.append((li, a, b))
            self.edge_pairs.append((a, b))
        self.apply_highlights()
        self._cluster_init()
        self.kick()

    # ---- structure mode: progressive disclosure --------------------------------
    def node_icon_pm(self, cat, radius):
        size = max(16, min(56, int(radius * 2)))
        key = (cat, size)
        if key not in self._pm_cache:
            if cat == "drive":
                base = place_icon("drive")
            elif cat == "app":
                base = app_icon()
            else:
                base = category_icon(cat)
            self._pm_cache[key] = base.pixmap(size, size)
        return self._pm_cache[key]

    def _add_visible(self, i, parent_idx=None):
        nd = self._data_nodes[i]
        kind = nd.get("kind", "folder")
        if kind == "drive":
            r = 15.0
        elif kind == "file":
            r = 5.0
        else:
            r = 7.0 + min(13.0, math.log10((nd.get("weight") or 0) + 1) * 3.4)
        item = GraphNode(self, i, nd.get("label") or "", r, nd.get("parent_path"),
                         nd.get("file"), nd.get("path"), kind, nd.get("sub"))
        cat = nd.get("category")
        if cat is None:
            if kind == "drive":
                cat = "drive"
            elif kind == "note":
                cat = "doc"
            elif kind == "folder":
                cat = "folder"
            elif nd.get("file"):
                cat = category_for(nd["file"], False)
        item.icon_pm = self.node_icon_pm(cat or "other", r)
        item.setToolTip((nd.get("label") or "")
                        + (("\n" + nd["sub"]) if nd.get("sub") else ""))
        self.scene().addItem(item)
        self._items[i] = item
        self._visible.add(i)
        if parent_idx is not None and parent_idx in self._visible:
            li = QGraphicsLineItem(0, 0, 0, 0)
            li.setPen(QPen(QColor("#e3e5e8"), 1.4))
            li.setZValue(0)
            self.scene().addItem(li)
            self._edge_lines[i] = li
            self._parent_of[i] = parent_idx
            self._tree_children.setdefault(parent_idx, []).append(i)
        item.can_expand = (bool(nd.get("has_children"))
                           and not self._tree_children.get(i))

    def expand_node(self, idx):
        """Reveal the direct children of this node — from the built data when
        available, otherwise fetched live from the index, so any depth of any
        drive can be explored."""
        if self._mode not in ("structure", "vault"):
            return
        parent_path = self._data_nodes[idx].get("path")
        if parent_path and self.child_provider:
            for nd in self.child_provider(parent_path):
                if self._mode == "vault":
                    # the link graph only shows folders and markdown notes
                    if nd.get("kind") == "file":
                        fn = (nd.get("file") or "").lower()
                        if not fn.endswith(".md"):
                            continue
                        stem = nd["file"][:-3] if fn.endswith(".md") else nd["file"]
                        nd = dict(nd)
                        nd["kind"] = "note"
                        nd["label"] = stem
                        nd["weight"] = 0
                        nd["sub"] = None
                        nd["has_children"] = False
                key = ((nd.get("parent_path") or "").lower(),
                       (nd.get("file") or "").lower())
                if key in self._data_key:
                    continue
                ni = len(self._data_nodes)
                self._data_nodes.append(nd)
                self._data_key[key] = ni
                self._children_data.setdefault(idx, []).append(ni)
        added = False
        for c in self._children_data.get(idx, []):
            if c in self._visible:
                continue
            self._add_visible(c, parent_idx=idx)
            added = True
        item = self._items.get(idx)
        if item:
            item.can_expand = False
            item.update()
        if added:
            self._apply_radial_layout()

    def collapse_node(self, idx):
        if self._mode not in ("structure", "vault"):
            return

        def rm(i):
            for c in list(self._tree_children.get(i, [])):
                rm(c)
            it = self._items.pop(i, None)
            if it is not None:
                self.scene().removeItem(it)
            li = self._edge_lines.pop(i, None)
            if li is not None:
                self.scene().removeItem(li)
            self._visible.discard(i)
            self._tree_children.pop(i, None)

        for c in list(self._tree_children.get(idx, [])):
            rm(c)
        self._tree_children.pop(idx, None)
        if self.sel_index not in self._visible:
            self.sel_index = None
        item = self._items.get(idx)
        if item:
            item.can_expand = True
            item.update()
        self._apply_radial_layout()

    def _recompute_link_lines(self):
        """Vault mode: draw one line per resolved note link. Both endpoints
        visible → note-to-note; a hidden endpoint attaches to its nearest
        visible ancestor bubble, so collapsed folders still show their
        connections."""
        scene = self.scene()
        for li, a, b in self._link_lines:
            scene.removeItem(li)
        self._link_lines = []
        if self._mode != "vault":
            return
        vis = {}
        for i in self._visible:
            p = (self._data_nodes[i].get("path") or "").lower()
            if p:
                vis[p] = i

        def endpoint(pl):
            while pl:
                if pl in vis:
                    return vis[pl]
                if "\\" not in pl:
                    break
                pl = pl.rsplit("\\", 1)[0]
            return None

        pairs = set()
        deg = {}
        for s, d in self._link_src_dst:
            a = endpoint(s)
            b = endpoint(d)
            if a is None or b is None or a == b:
                continue
            key = (min(a, b), max(a, b))
            if key in pairs:
                continue
            pairs.add(key)
            deg[a] = deg.get(a, 0) + 1
            deg[b] = deg.get(b, 0) + 1
        for a, b in sorted(pairs):
            li = QGraphicsLineItem(0, 0, 0, 0)
            li.setPen(QPen(QColor("#c3b8e2"), 1.2))
            li.setZValue(0)
            scene.addItem(li)
            self._link_lines.append((li, a, b))
            self._update_line(li, a, b)
        for i, it in self._items.items():
            if it.kind == "note" and deg.get(i, 0) >= 3:
                it.show_label = True

    def _apply_radial_layout(self):
        """Deterministic radial tidy-tree over the visible nodes: centre root,
        branches clockwise A→Z, ring radius by depth, angular width by subtree
        size. No simulation involved."""
        if self._mode not in ("structure", "vault") or not self._items:
            return
        children = {i: list(c) for i, c in self._tree_children.items() if c}
        roots_idx = [i for i in self._visible if i not in self._edge_lines]
        if not roots_idx:
            return
        depth_of = {}
        post = []
        visited = set()
        stack = [(r, 0, False) for r in reversed(roots_idx)]
        while stack:
            node, depth, done = stack.pop()
            if done:
                post.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            depth_of[node] = depth
            stack.append((node, depth, True))
            for c in children.get(node, []):
                if c not in visited and c in self._visible:
                    stack.append((c, depth + 1, False))
        disp_leaf = {i: 1 for i in self._visible}
        for node in post:  # children accumulate before parents
            kids = [c for c in children.get(node, []) if c in self._visible]
            if kids:
                disp_leaf[node] = sum(disp_leaf[c] for c in kids)
        total_leaves = max(1, sum(disp_leaf[r] for r in roots_idx))
        max_depth = max([1] + list(depth_of.values()))
        ring = min(9000.0, max(320.0, total_leaves * 30.0 / (2 * math.pi)))
        small = len(self._visible) <= 120

        def assign(idxs, a0, a1, depth):
            total = sum(disp_leaf[i] for i in idxs)
            ang = a0
            for i in sorted(idxs, key=lambda x: self._items[x].label.lower()):
                w = (a1 - a0) * disp_leaf[i] / total
                mid = ang + w / 2
                rad = ring * depth / max_depth
                it = self._items[i]
                if depth > 0:
                    # a little deterministic "floating" so the tree doesn't
                    # look mechanically perfect (±4% radius, ±15% of wedge)
                    h = zlib.crc32(it.label.encode("utf-8"))
                    j1 = ((h & 0xFFFF) / 0xFFFF - 0.5)
                    j2 = (((h >> 16) & 0xFFFF) / 0xFFFF - 0.5)
                    rad *= 1.0 + j1 * 0.08
                    mid += w * j2 * 0.3
                it.setPos(rad * math.cos(mid), rad * math.sin(mid))
                it.show_label = small or depth <= 2 or it.kind == "drive"
                kids = children.get(i)
                if kids:
                    assign(kids, ang, ang + w, depth + 1)
                ang += w

        assign(roots_idx, 0.0, 2 * math.pi, 0)
        for c, li in self._edge_lines.items():
            self._update_line(li, self._parent_of[c], c)
        self._anchors = {i: it.pos() for i, it in self._items.items()}
        if not self._float_timer.isActive():
            self._float_timer.start()
        self._fitted = True
        self.fit_view()

    def _float_tick(self):
        """Gentle idle drift for structure/vault graphs: each node bobs a
        few px around its radial-layout anchor. The layout itself stays
        deterministic; this is a visual offset only."""
        if self._mode not in ("structure", "vault") or not self._items:
            return
        t = time.time()
        for i, it in self._items.items():
            anchor = self._anchors.get(i)
            if anchor is None:
                continue
            if getattr(it, "_dragging", False):
                self._anchors[i] = it.pos()
                continue
            ph = i * 2.399963  # golden-angle phase spread
            it.setPos(anchor.x() + math.sin(t * 0.9 + ph) * 3.5,
                      anchor.y() + math.cos(t * 0.7 + ph * 1.3) * 3.5)
        if self._mode == "vault":
            self._recompute_link_lines()

    def _cluster_init(self):
        """Place notes near their folder's group centre so the link graph
        forms communities instead of one hairball."""
        groups = {}
        for i, key in enumerate(self.folder_of):
            groups.setdefault(key, []).append(i)
        for gi, key in enumerate(sorted(groups.keys())):
            ga = gi * 2.39996
            gr = 70.0 * ((gi + 1) ** 0.6)
            cx, cy = gr * math.cos(ga), gr * math.sin(ga)
            for k, i in enumerate(groups[key]):
                a2 = k * 2.39996
                rr = 26.0 * ((k + 1) ** 0.5)
                self.nodes[i].setPos(cx + rr * math.cos(a2), cy + rr * math.sin(a2))

    def select_node(self, node):
        self.sel_index = None if node.index == self.sel_index else node.index
        self.apply_highlights()
        self.node_clicked.emit(node if self.sel_index is not None else None)
        if self._mode in ("structure", "vault") and self.sel_index is not None \
                and node.can_expand:
            self.expand_node(node.index)

    def apply_highlights(self):
        pen_norm = QPen(QColor("#e3e5e8"), 1.4)
        pen_hot = QPen(QColor("#a7adb4"), 2.0)
        pen_dim = QPen(QColor("#f0f1f4"), 1.2)
        if self._mode in ("structure", "vault"):
            link_norm = QPen(QColor("#c3b8e2"), 1.2)
            link_hot = QPen(QColor("#8b5cf6"), 2.0)
            link_dim = QPen(QColor("#eeeaf6"), 1.0)
            neigh = set()
            if self.sel_index is not None:
                neigh.update(self._tree_children.get(self.sel_index, []))
                par = self._parent_of.get(self.sel_index)
                if par is not None:
                    neigh.add(par)
            for i, it in self._items.items():
                if self.sel_index is None:
                    it.state = 0
                elif i == self.sel_index:
                    it.state = 2
                elif i in neigh:
                    it.state = 1
                else:
                    it.state = 3
                it.update()
            for c, li in self._edge_lines.items():
                if self.sel_index is None:
                    li.setPen(pen_norm)
                elif c == self.sel_index or self._parent_of.get(c) == self.sel_index:
                    li.setPen(pen_hot)
                else:
                    li.setPen(pen_dim)
            for li, a, b in self._link_lines:
                if self.sel_index is None:
                    li.setPen(link_norm)
                elif a == self.sel_index or b == self.sel_index:
                    li.setPen(link_hot)
                else:
                    li.setPen(link_dim)
            return
        neigh = set()
        if self.sel_index is not None:
            for a, b in self.edge_pairs:
                if a == self.sel_index:
                    neigh.add(b)
                elif b == self.sel_index:
                    neigh.add(a)
        for i, it in enumerate(self.nodes):
            if self.sel_index is None:
                it.state = 0
            elif i == self.sel_index:
                it.state = 2
            elif i in neigh:
                it.state = 1
            else:
                it.state = 3
            it.update()
        for li, a, b in self.edge_items:
            if self.sel_index is None:
                li.setPen(pen_norm)
            elif a == self.sel_index or b == self.sel_index:
                li.setPen(pen_hot)
            else:
                li.setPen(pen_dim)

    def pause(self):
        self._timer.stop()

    # -- simulation ------------------------------------------------------------
    def kick(self):
        self._settle = 0
        if self.nodes and not self._timer.isActive():
            self._timer.start()

    def update_edges_for(self, idx):
        if self._mode in ("structure", "vault"):
            li = self._edge_lines.get(idx)
            if li is not None:
                self._update_line(li, self._parent_of[idx], idx)
            return
        for li, a, b in self.edge_items:
            if a == idx or b == idx:
                self._update_line(li, a, b)

    def _update_line(self, li, a, b):
        if self._mode in ("structure", "vault"):
            pa = self._items[a].pos()
            pb = self._items[b].pos()
        else:
            pa, pb = self.nodes[a].pos(), self.nodes[b].pos()
        li.setLine(QLineF(pa.x(), pa.y(), pb.x(), pb.y()))

    def _tick(self):
        n = len(self.nodes)
        if n == 0:
            self._timer.stop()
            return
        items = self.nodes
        xs = [it.x() for it in items]
        ys = [it.y() for it in items]
        fx = [0.0] * n
        fy = [0.0] * n
        cell = self.CELL
        cutoff2 = self.CUTOFF * self.CUTOFF
        grid = {}
        for i in range(n):
            grid.setdefault((int(xs[i] // cell), int(ys[i] // cell)), []).append(i)
        kr = 26000.0
        for i in range(n):
            cx, cy = int(xs[i] // cell), int(ys[i] // cell)
            xi, yi = xs[i], ys[i]
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    for j in grid.get((gx, gy), ()):
                        if j == i:
                            continue
                        dx, dy = xi - xs[j], yi - ys[j]
                        d2 = dx * dx + dy * dy
                        if d2 == 0.0:
                            fx[i] += 2.0
                            fy[i] += 1.5
                            continue
                        if d2 > cutoff2:
                            continue
                        f = kr / d2
                        if f > 60.0:
                            f = 60.0
                        d = d2 ** 0.5
                        fx[i] += dx / d * f
                        fy[i] += dy / d * f
        ks, rest = 0.028, 115.0
        for a, b in self.edge_pairs:
            dx, dy = xs[b] - xs[a], ys[b] - ys[a]
            d = (dx * dx + dy * dy) ** 0.5
            if d == 0.0:
                continue
            f = ks * (d - rest)
            ux, uy = dx / d, dy / d
            fx[a] += ux * f
            fy[a] += uy * f
            fx[b] -= ux * f
            fy[b] -= uy * f
        # mild pull toward each folder's centroid → visible communities
        if self._mode == "links" and self.folder_of:
            cen, cnt = {}, {}
            for i in range(n):
                k = self.folder_of[i]
                if k:
                    c = cen.get(k)
                    if c is None:
                        cen[k] = [xs[i], ys[i]]
                        cnt[k] = 1
                    else:
                        c[0] += xs[i]
                        c[1] += ys[i]
                        cnt[k] += 1
            for i in range(n):
                k = self.folder_of[i]
                if k and cnt[k] > 1:
                    fx[i] += (cen[k][0] / cnt[k] - xs[i]) * 0.012
                    fy[i] += (cen[k][1] / cnt[k] - ys[i]) * 0.012
        moved = 0.0
        damp, maxv = 0.80, 11.0
        self._simulating = True
        for i in range(n):
            fx[i] += -xs[i] * 0.0016
            fy[i] += -ys[i] * 0.0016
            if items[i]._dragging:
                self.vx[i] = self.vy[i] = 0.0
                continue
            vx = (self.vx[i] + fx[i]) * damp
            vy = (self.vy[i] + fy[i]) * damp
            sp = (vx * vx + vy * vy) ** 0.5
            if sp > maxv:
                vx, vy = vx / sp * maxv, vy / sp * maxv
            self.vx[i], self.vy[i] = vx, vy
            items[i].setPos(xs[i] + vx, ys[i] + vy)
            moved += abs(vx) + abs(vy)
        self._simulating = False
        for li, a, b in self.edge_items:
            self._update_line(li, a, b)
        if moved / max(1, n) < 0.15:
            self._settle += 1
            if self._settle > 18:
                self._timer.stop()
                if not self._fitted:
                    self._fitted = True
                    self.fit_view()
        else:
            self._settle = 0

    def fit_view(self):
        br = self.scene().itemsBoundingRect().adjusted(-80, -80, 80, 80)
        if br.width() < 10:
            return
        self.fitInView(br, Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()

    # -- interaction -----------------------------------------------------------
    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        factor = 1.18 if delta > 0 else 1 / 1.18
        nz = self._zoom * factor
        if 0.08 <= nz <= 8.0:
            self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
            self.scale(factor, factor)
            self._zoom = nz
        e.accept()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not isinstance(self.itemAt(e.pos()), GraphNode):
            self._pan_from = e.pos()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._pan_from is not None:
            d = e.pos() - self._pan_from
            self._pan_from = e.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._pan_from is not None and e.button() == Qt.LeftButton:
            self._pan_from = None
            self.viewport().unsetCursor()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        if isinstance(self.itemAt(e.pos()), GraphNode):
            super().contextMenuEvent(e)
            return
        menu = QMenu(self)
        menu.addAction("Fit to view", self.fit_view)
        if self._mode == "links":
            menu.addAction("Re-run layout", self.kick)
        else:
            menu.addAction("Reapply radial layout", self._apply_radial_layout)
        menu.exec(e.globalPos())


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class QLaunchButton(QPushButton):
    """Sidebar quick-launch entry: click = inspect, double-click = launch."""
    launch = Signal()

    def __init__(self, path, parent):
        stem = os.path.splitext(os.path.basename(path))[0]
        super().__init__("  " + stem, parent)
        self._path = path
        self.setObjectName("sideBtn")
        self.setIcon(app_icon_for(path))
        self.setIconSize(QSize(16, 16))
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip(path)

    def path(self):
        return self._path

    def mouseDoubleClickEvent(self, e):
        self.launch.emit()


class MainWindow(FramelessWindow):
    graph_ready = Signal(int, dict, object)
    graph_progress = Signal(int, str)
    links_progress = Signal(int, int)
    links_done = Signal()

    def __init__(self, worker: SearchWorker):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.worker = worker
        self.worker.have.connect(self.on_results)
        self.graph_ready.connect(self.on_graph_ready)
        self.graph_progress.connect(self.on_graph_progress)
        self.links_progress.connect(self.on_links_progress)
        self.links_done.connect(self.on_links_done)
        self.view_seq = 0
        self.current_place = None
        self.current_place_name = "All Files"
        self.current_cat = "all"
        self.current_rows = []
        self.view_mode = "list"
        self._graph_seq = 0
        self._analyzer_running = False
        self._analyzer_stop = threading.Event()
        self._indexer = None
        self._index_pending = None
        self._ql_buttons = []
        self._ql_selected = None
        self.graph_scope_path = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.titlebar = TitleBar(self, with_search=True)
        self.titlebar.search.edit.textChanged.connect(self.on_search_changed)
        root.addWidget(self.titlebar)

        self._history = []
        self.titlebar.btn_back.clicked.connect(self.go_back)
        self.titlebar.btn_back.setEnabled(False)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root.addLayout(body, 1)

        # ---- sidebar ----
        self.sidebar = QFrame(self)
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(206)
        sbar = QVBoxLayout(self.sidebar)
        sbar.setContentsMargins(10, 10, 10, 10)
        sbar.setSpacing(2)
        self._side_buttons = []
        self._build_sidebar()
        body.addWidget(self.sidebar)

        # ---- main column ----
        main = QFrame(self)
        main.setObjectName("content")
        mbar = QVBoxLayout(main)
        mbar.setContentsMargins(0, 0, 0, 0)
        mbar.setSpacing(0)

        chips = QHBoxLayout()
        chips.setContentsMargins(16, 10, 16, 8)
        chips.setSpacing(8)
        self._cat_buttons = {}
        for label, cat in (("All", "all"), ("Folders", "folder"), ("Documents", "doc"),
                           ("Images", "img"), ("Videos", "vid"), ("Music", "aud"),
                           ("Archives", "zip"), ("Code", "code")):
            b = QPushButton(label, main)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda checked, c=cat: self.select_cat(c))
            chips.addWidget(b)
            self._cat_buttons[cat] = b
        self._cat_buttons["all"].setChecked(True)
        self.btn_graph = QPushButton("Graph", main)
        self.btn_graph.setCheckable(True)
        self.btn_graph.setCursor(Qt.PointingHandCursor)
        self.btn_graph.setFocusPolicy(Qt.NoFocus)
        self.btn_graph.setToolTip(
            "Graph of the current view: link graph for a selected vault, "
            "structure diagram for everything else")
        self.btn_graph.clicked.connect(lambda checked: self.toggle_graph(checked))
        chips.addWidget(self.btn_graph)
        self._sort_mode = "default"
        self.btn_sort = QPushButton("Sort: Default", main)
        self.btn_sort.setCursor(Qt.PointingHandCursor)
        self.btn_sort.clicked.connect(self.show_sort_menu)
        chips.addWidget(self.btn_sort)
        chips.addStretch(1)
        chipsw = QWidget(main)
        chipsw.setObjectName("chips")
        chipsw.setLayout(chips)
        mbar.addWidget(chipsw)

        self.tree = ResultsTree(main)
        self.tree.itemSelectionChanged.connect(self.on_selection)
        self.tree.itemClicked.connect(self.on_tree_clicked)
        self.tree.itemDoubleClicked.connect(
            lambda item, col: self.open_in_app(item.data(0, Qt.UserRole)))
        self.tree.reveal_requested.connect(lambda: self.open_in_app())
        self.tree.open_requested.connect(self.open_current)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_context_menu)
        mbar.addWidget(self.tree, 1)

        self.empty_label = QLabel("No matches in this view", main)
        self.empty_label.setObjectName("emptyLabel")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.hide()
        mbar.addWidget(self.empty_label, 1)

        self.graph_view = GraphView(main)
        self.graph_view.hide()
        self.graph_view.node_clicked.connect(self.on_node_clicked)
        self.graph_view.node_activated.connect(self.on_node_activated)
        self.graph_view.show_node_menu.connect(self.show_node_menu)
        self.graph_view.child_provider = self.graph_child_provider
        mbar.addWidget(self.graph_view, 1)
        body.addWidget(main, 1)

        # ---- details pane ----
        self.details = DetailsPane(self)
        body.addWidget(self.details)

        # ---- status bar ----
        self.status_bar = QFrame(self)
        self.status_bar.setObjectName("statusbar")
        self.status_bar.setFixedHeight(30)
        stab = QHBoxLayout(self.status_bar)
        stab.setContentsMargins(14, 0, 14, 0)
        self.status_left = QLabel("", self.status_bar)
        self.status_right = QLabel("", self.status_bar)
        for lab in (self.status_left, self.status_right):
            lab.setObjectName("statusText")
        stab.addWidget(self.status_left)
        stab.addStretch(1)
        stab.addWidget(self.status_right)
        root.addWidget(self.status_bar)

        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self.refresh_view)

        self._spinner = QTimer(self)
        self._spinner.setInterval(110)
        self._spinner.timeout.connect(self._tick_spinner)
        self._spin_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spin_i = 0
        self._indexing_path = ""

        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.titlebar.search.edit.setFocus())
        QShortcut(QKeySequence("Escape"), self, self.escape_pressed)

        self.select_cat("all")
        if not os.environ.get("EZEXP_NO_AUTOSCAN"):
            QTimer.singleShot(400, lambda: self.start_indexer(False))

    # ---- helpers -----------------------------------------------------------
    def on_max_state(self):
        r = 0 if self._maximized else 12
        self.titlebar.setStyleSheet(
            f"#titlebar {{ border-top-left-radius: {r}px; border-top-right-radius: {r}px; }}")
        self.status_bar.setStyleSheet(
            f"#statusbar {{ border-bottom-left-radius: {r}px; border-bottom-right-radius: {r}px; }}")

    def select_place(self, btn, path, name, vault=False):
        self._push_history()
        self._ql_selected = None
        self.graph_scope_path = None
        for b in self._side_buttons:
            b.setChecked(b is btn)
        for b in self._ql_buttons:
            b.setChecked(False)
        self.current_place = path
        self.current_place_name = name
        self.titlebar.setTitle(name)
        self.refresh_view()
        if vault and path:
            self.ensure_indexed(path)
        if self.view_mode == "graph":
            self.refresh_graph()

    # ---- quick launch ---------------------------------------------------------
    def select_app(self, path, btn):
        folder = os.path.dirname(path)
        if folder and path.lower().endswith(".lnk"):
            # the shortcut sits on the Desktop / in the Start Menu, but
            # the app's root folder is its TARGET's folder
            target = resolve_lnk_target(path)
            if target:
                folder = os.path.dirname(target) or folder
        if folder:
            # ALWAYS open the folder: current_place may already equal it
            # while the list shows search/category results instead of the
            # folder's contents, and in graph mode nothing would open.
            if self.view_mode != "list" or self.current_cat != "all":
                self.enter_list_mode("all")
            self.drill_to_path(folder,
                               os.path.basename(folder) or folder)
        self._ql_selected = path
        self.graph_scope_path = folder
        for b in self._side_buttons:
            b.setChecked(False)
        for b in self._ql_buttons:
            b.setChecked(b is btn)
        self.details.show_app(path)
        if self.view_mode == "graph":
            self.refresh_graph()

    def add_quicklaunch_app(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Add application to Quick Launch", "",
            "Programs (*.exe *.lnk *.bat *.cmd);;All files (*.*)")
        if not f:
            return
        apps = get_quicklaunch()
        if not any(a.lower() == f.lower() for a in apps):
            apps.append(f)
            set_quicklaunch(apps)
        self._build_sidebar()

    def remove_quicklaunch(self, path):
        set_quicklaunch([p for p in get_quicklaunch()
                         if p.lower() != path.lower()])
        remove_launch_count(path)
        if self._ql_selected and self._ql_selected.lower() == path.lower():
            self._ql_selected = None
            self.details.show_row(None)
        self._build_sidebar()

    def build_app_menu(self, path):
        folder = os.path.dirname(path)
        menu = QMenu(self)
        menu.addAction("Launch", lambda: self.launch_path(path))
        menu.addAction("Browse Folder",
                       lambda: self.drill_to_path(folder, os.path.basename(folder) or folder))
        menu.addAction("Show in Explorer", lambda: reveal_in_explorer(path))
        menu.addAction("Copy as path", lambda: QApplication.clipboard().setText(path))
        menu.addAction("Remove from Quick Launch",
                       lambda: self.remove_quicklaunch(path))
        return menu

    def launch_path(self, path):
        """Open with the default app; remember how often programs are used so
        the Quick Launch section can surface favourites."""
        try:
            os.startfile(path)
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"Could not open:\n{exc}")
            return
        if ext_of(path) in APP_EXTS:
            bump_launch_count(path)
            self._build_sidebar()

    # ---- sidebar ----------------------------------------------------------
    def _build_sidebar(self):
        sbar = self.sidebar.layout()
        while sbar.count():
            item = sbar.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._side_buttons = []

        def section(text):
            lab = QLabel(text, self.sidebar)
            lab.setObjectName("sideSection")
            sbar.addSpacing(8)
            sbar.addWidget(lab)

        def place_btn(name, path, icon_kind, vault=False):
            b = QPushButton("  " + name, self.sidebar)
            b.setObjectName("sideBtn")
            b.setIcon(place_icon(icon_kind))
            b.setIconSize(QSize(16, 16))
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b._place = path
            b._vault = vault
            if vault:
                b.setToolTip(path)
                b.setContextMenuPolicy(Qt.CustomContextMenu)
                b.customContextMenuRequested.connect(
                    lambda pos, btn=b, p=path: self.show_vault_menu(btn, p))
            b.clicked.connect(
                lambda checked, btn=b, p=path, n=name, v=vault: self.select_place(btn, p, n, v))
            sbar.addWidget(b)
            self._side_buttons.append(b)
            return b

        section("Library")
        self.btn_all = place_btn("All Files", None, "grid")
        section("Quick Launch")
        self._ql_buttons = []
        manual = get_quicklaunch()
        existing = {m.lower() for m in manual}
        counts = get_launch_counts()
        frequent = sorted(
            (p for p, c in counts.items()
             if c and ext_of(p) in APP_EXTS and os.path.isfile(p)
             and p.lower() not in existing),
            key=lambda p: -counts[p])[:6]
        for p in manual + frequent:
            b = QLaunchButton(p, self.sidebar)
            b.clicked.connect(lambda checked, btn=b, pp=p: self.select_app(pp, btn))
            b.launch.connect(lambda pp=p: self.launch_path(pp))
            if self._ql_selected and p.lower() == self._ql_selected.lower():
                b.setChecked(True)
            sbar.addWidget(b)
            self._ql_buttons.append(b)
        ql_add = QPushButton("  ＋ Add App…", self.sidebar)
        ql_add.setObjectName("sideAction")
        ql_add.setCursor(Qt.PointingHandCursor)
        ql_add.clicked.connect(self.add_quicklaunch_app)
        sbar.addWidget(ql_add)
        section("Favourites")
        for nice, path in shell_folders():
            icons = {"Desktop": "monitor", "Documents": "files", "Downloads": "download",
                     "Pictures": "image", "Music": "music", "Videos": "film"}
            place_btn(nice, path, icons.get(nice, "files"))
        for v in get_vaults():
            place_btn(os.path.basename(v) or v, v, "vault", vault=True)
        add_btn = QPushButton("  ＋ Add Obsidian Vault…", self.sidebar)
        add_btn.setObjectName("sideAction")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self.add_vault)
        sbar.addWidget(add_btn)
        section("Locations")
        for root_drive in fixed_drives():
            place_btn(drive_label(root_drive), root_drive.rstrip("\\") or root_drive[:2], "drive")

        sbar.addStretch(1)
        self.rescan_btn = QPushButton("  Rescan now", self.sidebar)
        self.rescan_btn.setObjectName("sideAction")
        self.rescan_btn.setIcon(place_icon("drive"))
        self.rescan_btn.setCursor(Qt.PointingHandCursor)
        self.rescan_btn.clicked.connect(lambda: self.start_indexer(False))
        sbar.addWidget(self.rescan_btn)
        self.rebuild_btn = QPushButton("  Rebuild index", self.sidebar)
        self.rebuild_btn.setObjectName("sideAction")
        self.rebuild_btn.setCursor(Qt.PointingHandCursor)
        self.rebuild_btn.clicked.connect(self.confirm_rebuild)
        sbar.addWidget(self.rebuild_btn)

        # restore the check mark on the current place
        cur = (self.current_place or "").lower()
        for b in self._side_buttons:
            b.setChecked(bool(b._place) and (b._place or "").lower() == cur)

    def show_vault_menu(self, btn, path):
        menu = QMenu(self)
        act = menu.addAction("Remove from Favourites")
        act.triggered.connect(lambda: self.remove_vault(path))
        menu.exec(btn.mapToGlobal(btn.rect().center()))

    def add_vault(self):
        vaults = get_vaults()
        suggested = vaults[0] if vaults else "G:/mydrive/obsidian"
        text, ok = QInputDialog.getText(
            self, "Add Obsidian Vault", "Path to your Obsidian vault folder:",
            text=suggested)
        if not ok:
            return
        path = os.path.normpath(text.strip().strip('"').replace("/", os.sep))
        if not os.path.isdir(path):
            QMessageBox.warning(self, APP_NAME, f"That folder doesn't exist:\n{path}")
            return
        if not any(v.lower() == path.lower() for v in vaults):
            vaults.append(path)
            set_vaults(vaults)
        self._build_sidebar()
        for b in self._side_buttons:
            if getattr(b, "_vault", False) and (b._place or "").lower() == path.lower():
                b.click()
                break

    def remove_vault(self, path):
        set_vaults([v for v in get_vaults() if v.lower() != path.lower()])
        self._build_sidebar()
        if self.current_place and self.current_place.lower() == path.lower():
            self.btn_all.click()

    def ensure_indexed(self, path):
        """If this place has never been indexed (e.g. a vault on a removable
        drive), kick off a scan right away."""
        p = path.rstrip("\\")
        try:
            n = thread_conn().execute(
                "SELECT COUNT(*) FROM files WHERE parent=? COLLATE NOCASE"
                " OR parent LIKE ? ESCAPE '#'",
                (p, like_escape(p) + "\\%")).fetchone()[0]
        except sqlite3.Error:
            n = 1
        if n == 0:
            self.start_indexer(False)
            self.status_left.setText("Scanning this vault for the first time…")

    # ---- navigation ---------------------------------------------------------
    def _push_history(self):
        self._history.append((self.current_place, self.current_place_name))
        if len(self._history) > 60:
            self._history.pop(0)
        self.titlebar.btn_back.setEnabled(True)

    def go_back(self):
        if not self._history:
            return
        place, name = self._history.pop()
        self.current_place = place
        self.current_place_name = name
        self.titlebar.setTitle(name)
        self.titlebar.search.edit.clear()
        self.refresh_view()
        self.titlebar.btn_back.setEnabled(bool(self._history))
        cur = (place or "").lower()
        for b in self._side_buttons:
            b.setChecked(bool(b._place) and (b._place or "").lower() == cur)
        if self.view_mode == "graph":
            self.refresh_graph()

    def drill_to_path(self, path, name):
        self._push_history()
        self._ql_selected = None
        self.graph_scope_path = None
        for b in self._ql_buttons:
            b.setChecked(False)
        self.current_place = path.rstrip("\\")
        self.current_place_name = name
        self.titlebar.setTitle(name)
        self.titlebar.search.edit.clear()
        cur = (self.current_place or "").lower()
        for b in self._side_buttons:
            b.setChecked(bool(b._place) and (b._place or "").lower() == cur)
        self.refresh_view()
        if self.view_mode == "graph":
            self.refresh_graph()

    def drill_in(self, row):
        path = join_path(row["parent"], row["name"])
        self.drill_to_path(path, row["name"])

    def open_in_app(self, row=None):
        """Enter / double-click: folders drill down in EzExp; files show their
        preview in the details pane (which selection already triggers)."""
        row = row or self.current_row()
        if not row:
            return
        if row["is_dir"]:
            self.drill_in(row)

    _SORT_LABELS = (("default", "Default"), ("name", "Name (A–Z)"),
                    ("newest", "Newest first"), ("oldest", "Oldest first"),
                    ("largest", "Largest first"),
                    ("smallest", "Smallest first"))

    def show_sort_menu(self):
        menu = QMenu(self)
        for key, label in self._SORT_LABELS:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._sort_mode == key)
            act.triggered.connect(
                lambda checked, k=key: self.set_sort_mode(k))
        menu.exec(self.btn_sort.mapToGlobal(
            self.btn_sort.rect().bottomLeft()))

    def set_sort_mode(self, mode):
        self._sort_mode = mode
        label = dict(self._SORT_LABELS).get(mode, "Default")
        self.btn_sort.setText("Sort: " + label)
        self.refresh_view()

    def select_cat(self, cat):
        self.enter_list_mode(cat)

    # ---- view modes ----------------------------------------------------------
    def toggle_graph(self, checked):
        if checked:
            self.enter_graph_mode()
        else:
            self.enter_list_mode("all")

    def enter_list_mode(self, cat="all"):
        self.view_mode = "list"
        self.current_cat = cat
        self.btn_graph.setChecked(False)
        for c, b in self._cat_buttons.items():
            b.setChecked(c == cat)
        self.graph_view.pause()
        self.graph_view.hide()
        self.tree.show()
        self.refresh_view()

    def enter_graph_mode(self):
        self.view_mode = "graph"
        for b in self._cat_buttons.values():
            b.setChecked(False)
        self.tree.hide()
        self.empty_label.hide()
        self.graph_view.show()
        self.refresh_graph()
        self.start_link_analyzer()

    def refresh_graph(self):
        """Scope follows the current view: a vault tab → its link graph;
        anything else (All Files, a drive, a favourite, a folder) → that
        place's structure graph, read straight from the index."""
        place = self.graph_scope_path or self.current_place
        vault = active_vault_for(place) if place else None
        is_vault_root = bool(vault) and place.rstrip("\\").lower() == vault.lower()
        self._graph_seq += 1
        seq = self._graph_seq
        if is_vault_root:
            self.graph_view.show_message(
                f"Building link graph of “{os.path.basename(vault)}”…")
            spec = {"mode": "vault", "vault": vault}
        else:
            self.graph_view.show_message("Building structure graph from the index…")
            spec = {"mode": "structure", "place": place}
        threading.Thread(target=self._graph_job, args=(seq, spec), daemon=True).start()

    def _graph_job(self, seq, spec):
        try:
            if spec["mode"] in ("links", "vault"):
                data = build_vault_graph(thread_conn(), spec["vault"])
            else:
                data = build_structure_graph(thread_conn(), spec.get("place"))
            data["mode"] = spec["mode"]
        except Exception as exc:
            data = {"error": str(exc)}
        self.graph_ready.emit(seq, spec, data)

    def on_graph_progress(self, seq, text):
        if seq == self._graph_seq and self.view_mode == "graph":
            self.graph_view.show_message(text)

    # ---- background link analyzer ---------------------------------------------
    def start_link_analyzer(self):
        """Parse [[wikilinks]] of not-yet-analyzed vault notes into the index.
        Runs in the background in budgeted passes until every note is done;
        unchanged notes are never re-read."""
        if self._analyzer_running:
            return
        vaults = get_vaults()
        if not vaults or pending_link_count(thread_conn(), vaults) == 0:
            return
        self._analyzer_stop.clear()
        threading.Thread(target=self._analyzer_job, daemon=True).start()

    def _analyzer_job(self):
        self._analyzer_running = True
        try:
            conn = thread_conn()
            vaults = get_vaults()
            t0 = time.time()

            def prog(done, remaining):
                self.links_progress.emit(done, remaining)

            while time.time() - t0 < 900 and not self._analyzer_stop.is_set():
                analyzed, remaining = analyze_pending_links(
                    conn, vaults, time_budget=6.0, progress=prog)
                if remaining == 0:
                    break
                time.sleep(2.5)
        finally:
            self._analyzer_running = False
            self.links_done.emit()

    def on_links_progress(self, done, remaining):
        if self.view_mode == "graph" and not self.graph_view.nodes:
            self.graph_view.show_message(f"Analyzing vault links… {remaining} notes left")

    def on_links_done(self):
        if self.view_mode == "graph":
            place = self.current_place
            vault = active_vault_for(place) if place else None
            if vault and place.rstrip("\\").lower() == vault.lower():
                self.refresh_graph()

    def on_graph_ready(self, seq, spec, data):
        if seq != self._graph_seq or self.view_mode != "graph":
            return
        if "error" in data:
            self.graph_view.show_message(f"Could not build the graph:\n{data['error']}")
            return
        self.graph_view.set_graph(data)
        if data.get("mode") == "structure":
            place = spec.get("place")
            scope = "all drives" if not place else (place.rsplit("\\", 1)[-1] or place)
            n_folders = sum(1 for nd in data["nodes"] if nd["kind"] == "folder")
            extra = " · file lists truncated" if data.get("truncated_files") else ""
            self.status_left.setText(
                f"Structure of {scope}: {n_folders} folders · "
                f"{len(data['edges'])} connections{extra}")
        else:
            v = spec["vault"]
            analyzed = data.get("analyzed", data["total_md"])
            ann = "" if analyzed >= data["total_md"] else \
                f" · analyzing {analyzed}/{data['total_md']} notes (completes in background)"
            self.status_left.setText(
                f"{data['total_md']} notes · {len(data.get('link_edges', []))} links · "
                f"{os.path.basename(v)}{ann}")

    def on_node_clicked(self, node):
        if node is None:
            return
        self._ql_selected = None
        for b in self._ql_buttons:
            b.setChecked(False)
        try:
            r = thread_conn().execute(
                "SELECT parent, name, is_dir, size, mtime, category, kind FROM files"
                " WHERE parent=? AND name=?", (node.parent_path, node.file)).fetchone()
        except sqlite3.Error:
            r = None
        if r:
            self.details.show_row(_row_dict(r))

    def on_node_activated(self, node):
        if not node.path:
            return
        try:
            os.startfile(node.path)
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"Could not open:\n{exc}")

    def graph_child_provider(self, parent_path):
        """Direct children of a folder, fetched live from the index for
        on-demand graph expansion (any depth)."""
        out = []
        try:
            rows = thread_conn().execute(
                "SELECT name, is_dir, size FROM files WHERE parent = ? COLLATE NOCASE"
                " ORDER BY is_dir DESC, name COLLATE NOCASE LIMIT 400",
                (parent_path,)).fetchall()
        except sqlite3.Error:
            return out
        for name, is_dir, size in rows:
            if is_dir:
                try:
                    n = thread_conn().execute(
                        "SELECT COUNT(*) FROM files WHERE parent = ? COLLATE NOCASE",
                        (join_path(parent_path, name),)).fetchone()[0]
                except sqlite3.Error:
                    n = 0
                out.append({"label": name, "kind": "folder", "category": "folder",
                            "weight": n, "sub": f"{n:,} items",
                            "path": join_path(parent_path, name),
                            "parent_path": parent_path, "file": name,
                            "has_children": n > 0})
            else:
                out.append({"label": name, "kind": "file",
                            "category": category_for(name, False),
                            "weight": size or 0,
                            "sub": human_size(size), "path": join_path(parent_path, name),
                            "parent_path": parent_path, "file": name,
                            "has_children": False})
        return out

    def show_node_menu(self, node, gpos):
        menu = QMenu(self)
        gv = self.graph_view
        if gv._mode in ("structure", "vault"):
            if node.can_expand:
                menu.addAction("Expand", lambda: gv.expand_node(node.index))
            if gv._tree_children.get(node.index):
                menu.addAction("Collapse branch", lambda: gv.collapse_node(node.index))
            menu.addSeparator()
        if node.path:
            menu.addAction("Open", lambda: self.on_node_activated(node))
            menu.addAction("Show in Explorer", lambda: reveal_in_explorer(node.path))
            menu.addAction("Copy as path", lambda: QApplication.clipboard().setText(node.path))
        menu.exec(gpos)

    def on_search_changed(self, text):
        self._search_debounce.start()

    def refresh_view(self):
        q = self.titlebar.search.edit.text()
        self.view_seq = self.worker.submit(q, self.current_place, self.current_cat)

    def on_results(self, seq, q, rows, note):
        if seq != self.view_seq:
            return
        if getattr(self, "_sort_mode", "default") != "default" and rows:
            rows = sort_rows(rows, self._sort_mode)
        self.current_rows = rows
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        for r in rows:
            self.tree.addTopLevelItem(make_row_item(r))
        self.tree.setUpdatesEnabled(True)
        if self.view_mode != "list":
            return
        has = bool(rows)
        self.tree.setVisible(has)
        self.empty_label.setVisible(not has)
        self.empty_label.setText("No matches in this view")
        total = get_meta(thread_conn(), "total", "?")
        try:
            total_s = f"{int(total):,}"
        except (TypeError, ValueError):
            total_s = "…"
        if q.strip():
            left = f"{len(rows)} result{'s' if len(rows) != 1 else ''} for “{q.strip()}”"
        else:
            left = f"{len(rows)} shown · {total_s} items in index"
        if note == "truncated":
            left += "  (showing first 600 — refine to narrow)"
        self.status_left.setText(left)
        if not rows and not self._ql_selected:
            self.details.show_row(None)
        else:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def on_selection(self):
        if self._ql_selected:
            return
        item = self.tree.currentItem()
        self.details.show_row(item.data(0, Qt.UserRole) if item else None)

    def on_tree_clicked(self, item, col=0):
        self._ql_selected = None
        for b in self._ql_buttons:
            b.setChecked(False)
        self.details.show_row(item.data(0, Qt.UserRole) if item else None)

    def current_row(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def row_path(self, row):
        p = join_path(row["parent"], row["name"])
        return p + "\\" if len(p) == 2 else p

    # ---- actions -----------------------------------------------------------
    def reveal_current(self):
        if self._ql_selected:
            reveal_in_explorer(self._ql_selected)
            return
        row = self.current_row()
        if row:
            reveal_in_explorer(self.row_path(row))

    def open_current(self):
        if self._ql_selected:
            self.launch_path(self._ql_selected)
            return
        row = self.current_row()
        if row:
            try:
                os.startfile(self.row_path(row))
            except OSError as exc:
                QMessageBox.warning(self, APP_NAME, f"Could not open:\n{exc}")

    def show_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        self.tree.setCurrentItem(item)
        row = item.data(0, Qt.UserRole)
        menu = self.build_menu(row)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def create_new_entry(self, folder, is_dir):
        kind = "folder" if is_dir else "file"
        name, ok = QInputDialog.getText(
            self, APP_NAME, f"New {kind} name:",
            text="New Folder" if is_dir else "untitled.txt")
        if not ok:
            return
        name = name.strip()
        if (not name or name in (".", "..")
                or os.path.basename(name) != name):
            QMessageBox.warning(self, APP_NAME,
                                "That name is not allowed.")
            return
        path = join_path(folder, name)
        try:
            if is_dir:
                os.mkdir(path)
            else:
                with open(path, "x"):
                    pass
        except FileExistsError:
            QMessageBox.warning(self, APP_NAME,
                                f"“{name}” already exists here.")
            return
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME,
                                f"Could not create:\n{exc}")
            return
        try:
            st = os.stat(path)
            conn = thread_conn()
            insert_rows(conn, [_make_row(folder, name, is_dir, st,
                                         folder[:2])])
            conn.commit()
        except Exception:
            pass  # the next rescan will pick it up regardless
        self.refresh_view()

    def build_place_menu(self):
        if not self.current_place:
            return None
        menu = QMenu(self)
        folder = self.current_place
        act = menu.addAction("New File…")
        act.triggered.connect(lambda: self.create_new_entry(folder, False))
        act = menu.addAction("New Folder…")
        act.triggered.connect(lambda: self.create_new_entry(folder, True))
        act = menu.addAction("Paste")
        act.triggered.connect(lambda: self.paste_into(folder))
        return menu

    def selected_rows(self):
        return [it.data(0, Qt.UserRole)
                for it in self.tree.selectedItems()]

    def move_or_copy(self, paths, dest_dir, copy):
        """Move/copy files or folders into dest_dir and keep the index
        in sync per item. Never overwrites (see _unique_dest); dropping
        a folder into itself is refused per item. Returns True if
        anything changed."""
        errors = []
        conn = thread_conn()
        changed = False
        for src in paths:
            if not os.path.exists(src):
                continue
            name = os.path.basename(src)
            if os.path.isdir(src) and (dest_dir == src or dest_dir.startswith(src + os.sep)):
                errors.append(f"Cannot move “{name}” into itself.")
                continue
            dest = _unique_dest(dest_dir, name)
            try:
                if copy:
                    if os.path.isdir(src):
                        shutil.copytree(src, dest)
                    else:
                        shutil.copy2(src, dest)
                else:
                    shutil.move(src, dest)
            except (OSError, shutil.Error) as exc:
                errors.append(f"{name}: {exc}")
                continue
            if not copy:
                if os.path.isdir(dest):
                    delete_tree(conn, src)
                else:
                    conn.execute(
                        "DELETE FROM files WHERE parent=? AND name=?",
                        (os.path.dirname(src), name))
            try:
                index_add_path(conn, dest)
            except Exception:
                pass  # the next rescan will pick it up regardless
            changed = True
        if changed:
            conn.commit()
        if errors:
            QMessageBox.warning(
                self, APP_NAME,
                ("Some items could not be copied:\n"
                 if copy else "Some items could not be moved:\n")
                + "\n".join(errors[:6]))
        if changed:
            self.refresh_view()
        return changed

    def handle_drop(self, paths, folder, copy):
        if not copy:  # a move within the same folder is a no-op
            paths = [p for p in paths
                     if os.path.dirname(p) != folder]
        if paths:
            self.move_or_copy(paths, folder, copy)

    def set_clipboard_files(self, paths, cut):
        """Explorer-compatible clipboard: file URLs plus the Windows
        'Preferred DropEffect' (2 = move for Cut, 1 = copy)."""
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        mime.setData("Preferred DropEffect",
                     bytes([2 if cut else 1, 0, 0, 0]))
        QApplication.clipboard().setMimeData(mime)

    def cut_current(self):
        rows = self.selected_rows()
        if rows:
            self.set_clipboard_files([self.row_path(r) for r in rows],
                                      True)

    def clipboard_files(self):
        md = QApplication.clipboard().mimeData()
        if not md or not md.hasUrls():
            return [], False
        paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        cut = False
        if md.hasFormat("Preferred DropEffect"):
            data = bytes(md.data("Preferred DropEffect"))
            if len(data) >= 4 and int.from_bytes(data[:4], "little") == 2:
                cut = True
        return paths, cut

    def paste_into(self, folder):
        paths, cut = self.clipboard_files()
        if not paths or not folder:
            return
        if self.move_or_copy(paths, folder, copy=not cut) and cut:
            QApplication.clipboard().clear()

    def build_menu(self, row):
        menu = QMenu(self)
        act_reveal = menu.addAction("Show in Explorer")
        act_reveal.triggered.connect(self.reveal_current)
        act_open = menu.addAction("Open")
        act_open.triggered.connect(self.open_current)
        menu.addSeparator()
        _nf = self.row_path(row) if row["is_dir"] else row["parent"]
        act_newf = menu.addAction("New File…")
        act_newf.triggered.connect(
            lambda f=_nf: self.create_new_entry(f, False))
        act_newd = menu.addAction("New Folder…")
        act_newd.triggered.connect(
            lambda f=_nf: self.create_new_entry(f, True))
        menu.addSeparator()
        act_ren = menu.addAction("Rename…")
        act_ren.triggered.connect(lambda: self.rename_current(row))
        act_cp = menu.addAction("Copy as path")
        act_cp.triggered.connect(lambda: QApplication.clipboard().setText(self.row_path(row)))
        act_cpf = menu.addAction("Copy file")
        act_cpf.triggered.connect(self.copy_current_file)
        act_cut = menu.addAction("Cut")
        act_cut.triggered.connect(self.cut_current)
        _pf = self.row_path(row) if row["is_dir"] else row["parent"]
        act_paste = menu.addAction("Paste")
        act_paste.triggered.connect(lambda f=_pf: self.paste_into(f))
        menu.addSeparator()
        act_del = menu.addAction("Move to Recycle Bin")
        act_del.triggered.connect(lambda: self.delete_current(row))
        act_prop = menu.addAction("Properties")
        act_prop.triggered.connect(lambda: show_properties(self.row_path(row)))
        return menu

    def copy_current_file(self):
        rows = self.selected_rows()
        if not rows:
            row = self.current_row()
            rows = [row] if row else []
        if rows:
            self.set_clipboard_files([self.row_path(r) for r in rows],
                                      False)

    def rename_current(self, row):
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=row["name"])
        if not ok or not new.strip() or new == row["name"]:
            return
        new = new.strip()
        old_path = self.row_path(row)
        parent = row["parent"]
        new_path = join_path(parent, new)
        if len(new_path) <= 3:
            QMessageBox.warning(self, APP_NAME, "That name is not allowed.")
            return
        try:
            os.rename(real_dir(old_path), real_dir(new_path))
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"Rename failed:\n{exc}")
            return
        conn = thread_conn()
        try:
            idr = conn.execute("SELECT id FROM files WHERE parent=? AND name=?",
                               (parent, row["name"])).fetchone()
            if idr:
                conn.execute(
                    "UPDATE files SET name=?, ext=?, category=?, kind=? WHERE id=?",
                    (new, ext_of(new), category_for(new, row["is_dir"]),
                     kind_for(new, row["is_dir"]), idr[0]))
                fts_touch(conn, idr[0], new)
            if row["is_dir"]:
                conn.execute(
                    "UPDATE files SET parent = ? || substr(parent, ?) "
                    "WHERE parent LIKE ? ESCAPE '#'",
                    (new_path, len(old_path) + 1, like_escape(old_path) + "\\%"))
                conn.execute(
                    "DELETE FROM note_links WHERE parent=? OR parent LIKE ? ESCAPE '#'",
                    (old_path, like_escape(old_path) + "\\%"))
            conn.execute("DELETE FROM note_links WHERE parent=? AND name=?",
                         (parent, row["name"]))
            conn.commit()
        except sqlite3.Error:
            pass
        self.refresh_view()

    def delete_current(self, row):
        path = self.row_path(row)
        kind = "folder and everything inside it" if row["is_dir"] else "file"
        if QMessageBox.question(
                self, "Move to Recycle Bin",
                f"Move “{row['name']}” ({kind}) to the Recycle Bin?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        if not recycle_path(path):
            QMessageBox.warning(self, APP_NAME, "Could not move to the Recycle Bin.")
            return
        try:
            delete_tree(thread_conn(), path)
        except sqlite3.Error:
            pass
        self.refresh_view()

    def escape_pressed(self):
        edit = self.titlebar.search.edit
        if edit.text():
            edit.clear()
            self.refresh_view()

    # ---- indexing ------------------------------------------------------------
    def start_indexer(self, rebuild):
        self._last_index_rebuild = rebuild
        self._index_lock_retried = False
        if self._indexer and self._indexer.isRunning():
            self._index_pending = True if (self._index_pending or rebuild) else False
            self.rescan_btn.setText("  Rescan queued…")
            return
        self._analyzer_stop.set()
        self._indexer = IndexerThread(rebuild=rebuild)
        self._indexer.progress.connect(self.on_index_progress)
        self._indexer.done.connect(self.on_index_done)
        self._indexer.start()
        self.rescan_btn.setEnabled(False)
        self._spinner.start()

    def confirm_rebuild(self):
        if QMessageBox.question(
                self, "Rebuild index",
                "Delete the current catalog and re-index every drive from scratch?\n"
                "This can take several minutes.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.start_indexer(True)

    def on_index_progress(self, path, count):
        self._indexing_path = path
        self._count = count

    def _tick_spinner(self):
        self._spin_i = (self._spin_i + 1) % len(self._spin_frames)
        if self._indexer and self._indexer.isRunning():
            self.status_right.setText(
                f"{self._spin_frames[self._spin_i]} Indexing: {self._indexing_path}  ·  "
                f"{getattr(self, '_count', 0):,} items this run")

    def on_index_done(self, total, err):
        self._spinner.stop()
        self.rescan_btn.setEnabled(True)
        if err and err != "stopped":
            if "locked" in err and not getattr(
                    self, "_index_lock_retried", False):
                self._index_lock_retried = True
                self.status_right.setText("Index busy — retrying in 5 s…")
                QTimer.singleShot(5000, self._retry_last_index)
                return
            self.status_right.setText(f"Index error: {err}")
            self._consume_pending_index()
            return
        self._index_lock_retried = False
        stamp = time.strftime("%H:%M")
        if total >= 0:
            self.status_right.setText(f"{total:,} items indexed · updated {stamp}")
            set_meta(thread_conn(), "total", total)
        else:
            self.status_right.setText("Indexing was interrupted")
        self.refresh_view()
        if self.view_mode == "graph":
            self.refresh_graph()
        self.start_link_analyzer()
        self._consume_pending_index()

    def _retry_last_index(self):
        self._index_pending = None
        self.start_indexer(getattr(self, "_last_index_rebuild", False))

    def _consume_pending_index(self):
        self.rescan_btn.setText("  Rescan now")
        if self._index_pending is None:
            return
        rebuild = self._index_pending
        self._index_pending = None
        QTimer.singleShot(400, lambda: self.start_indexer(rebuild))


class ClickableLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class DetailsPane(QFrame):
    preview_ready = Signal(int, str, object)
    PREVIEW_H = 200

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("details")
        self.setFixedWidth(280)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 20)
        lay.setSpacing(6)

        self.preview_label = ClickableLabel(self)
        self.preview_label.setObjectName("previewLabel")
        self.preview_label.setFixedHeight(self.PREVIEW_H)
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setCursor(Qt.PointingHandCursor)
        self.preview_label.setToolTip("Click to open with the default app")
        self.preview_label.clicked.connect(lambda: self.parent().open_current())
        self.preview_label.hide()
        lay.addWidget(self.preview_label)

        self.text_preview = QLabel(self)
        self.text_preview.setObjectName("textPreview")
        self.text_preview.setWordWrap(True)
        self.text_preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text_preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.text_preview.setMaximumHeight(self.PREVIEW_H + 40)
        self.text_preview.hide()
        lay.addWidget(self.text_preview)

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(72, 72)
        self.icon_label.setAlignment(Qt.AlignCenter)
        wrap = QHBoxLayout()
        wrap.addStretch(1)
        wrap.addWidget(self.icon_label)
        wrap.addStretch(1)
        lay.addLayout(wrap)

        self.name_label = QLabel(self)
        self.name_label.setObjectName("dName")
        self.name_label.setAlignment(Qt.AlignCenter)
        self.name_label.setWordWrap(False)
        self.name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.name_label)

        self.kind_label = QLabel(self)
        self.kind_label.setObjectName("dKind")
        self.kind_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.kind_label)

        lay.addSpacing(10)
        grid = QGridLayout()
        grid.setVerticalSpacing(7)
        grid.setHorizontalSpacing(10)
        self.meta = {}
        self.meta_keys = {}
        for i, key in enumerate(("Size", "Items", "Where", "Modified")):
            k = QLabel(key, self)
            k.setObjectName("dKey")
            v = QLabel(self)
            v.setObjectName("dVal")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(k, i, 0, Qt.AlignTop)
            grid.addWidget(v, i, 1)
            self.meta[key] = v
            self.meta_keys[key] = k
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        lay.addStretch(1)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_show = QPushButton("Show in Folder", self)
        self.btn_show.setObjectName("btnPrimary")
        self.btn_show.setCursor(Qt.PointingHandCursor)
        self.btn_show.clicked.connect(lambda: self.parent().reveal_current())
        self.btn_open = QPushButton("Open", self)
        self.btn_open.setObjectName("btnSecondary")
        self.btn_open.setCursor(Qt.PointingHandCursor)
        self.btn_open.clicked.connect(lambda: self.parent().open_current())
        self.btn_more = QPushButton("⋯", self)
        self.btn_more.setObjectName("dots")
        self.btn_more.setFixedWidth(34)
        self.btn_more.setCursor(Qt.PointingHandCursor)
        self.btn_more.setToolTip("More actions")
        self.btn_more.clicked.connect(self.more_menu)
        btns.addWidget(self.btn_show, 1)
        btns.addWidget(self.btn_open)
        btns.addWidget(self.btn_more)
        lay.addLayout(btns)

        self._row = None
        self._preview_seq = 0
        self.preview_ready.connect(self.on_preview)
        self.placeholder = "Select an item to see its details"
        self.show_row(None)

    def more_menu(self):
        if getattr(self.parent(), "_ql_selected", None):
            menu = self.parent().build_app_menu(self.parent()._ql_selected)
            menu.exec(self.btn_more.mapToGlobal(self.btn_more.rect().bottomLeft()))
            return
        row = self.parent().current_row()
        if not row:
            menu = self.parent().build_place_menu()
            if menu is not None:
                menu.exec(self.btn_more.mapToGlobal(
                    self.btn_more.rect().bottomLeft()))
            return
        menu = self.parent().build_menu(row)
        menu.exec(self.btn_more.mapToGlobal(self.btn_more.rect().bottomLeft()))

    def show_app(self, path):
        self._row = None
        self._preview_seq += 1
        self.preview_label.hide()
        self.preview_label.clear()
        self.text_preview.hide()
        self.text_preview.clear()
        self.icon_label.show()
        if not path:
            self.show_row(None)
            return
        name = os.path.basename(path)
        stem = os.path.splitext(name)[0]
        folder = os.path.dirname(path)
        ext = ext_of(path)
        pm = app_icon_for(path).pixmap(64, 64)
        self.icon_label.setPixmap(pm)
        fm = QFontMetrics(self.name_label.font())
        self.name_label.setText(fm.elidedText(stem, Qt.ElideMiddle, 230))
        self.name_label.setToolTip(name)
        self.kind_label.setText(f"{ext.upper()} Application" if ext in APP_EXTS else "Application")
        size = 0
        mtime = 0
        try:
            if os.path.isfile(path):
                size = os.path.getsize(path)
                mtime = os.path.getmtime(path)
        except OSError:
            pass
        self.meta["Size"].setText(human_size(size) if size else "—")
        where = folder
        if len(where) == 2:
            where += "\\"
        wfm = QFontMetrics(self.meta["Where"].font())
        self.meta["Where"].setText(wfm.elidedText(where, Qt.ElideMiddle, 190))
        self.meta["Where"].setToolTip(where)
        self.meta["Modified"].setText(fmt_date(mtime) if mtime else "—")
        self.meta["Items"].hide()
        self.meta_keys["Items"].hide()
        self.btn_show.setEnabled(True)
        self.btn_open.setEnabled(True)
        self.btn_more.setEnabled(True)

    def show_row(self, row):
        self._row = row
        self._preview_seq += 1
        self.preview_label.hide()
        self.preview_label.clear()
        self.text_preview.hide()
        self.text_preview.clear()
        self.icon_label.show()
        if not row:
            self.icon_label.hide()
            self.name_label.setText(self.placeholder)
            self.kind_label.setText("")
            for v in self.meta.values():
                v.setText("")
            self.meta_keys["Items"].hide()
            self.meta["Items"].hide()
            self.btn_show.setEnabled(False)
            self.btn_open.setEnabled(False)
            self.btn_more.setEnabled(False)
            return
        pm = category_icon(row["category"]).pixmap(64, 64)
        self.icon_label.setPixmap(pm)
        fm = QFontMetrics(self.name_label.font())
        self.name_label.setText(fm.elidedText(row["name"], Qt.ElideMiddle, 230))
        self.name_label.setToolTip(row["name"])
        self.kind_label.setText(row["kind"])
        self.meta["Size"].setText("—" if row["is_dir"] else human_size(row["size"]))
        where = row["parent"] if row["is_dir"] else row["parent"] + "\\" + row["name"]
        if len(where) == 2:
            where += "\\"
        wfm = QFontMetrics(self.meta["Where"].font())
        self.meta["Where"].setText(wfm.elidedText(where, Qt.ElideMiddle, 190))
        self.meta["Where"].setToolTip(where)
        self.meta["Modified"].setText(fmt_date(row["mtime"]))
        if row["is_dir"]:
            try:
                n = thread_conn().execute(
                    "SELECT COUNT(*) FROM files WHERE parent=? COLLATE NOCASE",
                    (join_path(row["parent"], row["name"]),)).fetchone()[0]
            except sqlite3.Error:
                n = 0
            self.meta["Items"].setText(f"{n:,}" if n else "—")
            self.meta["Items"].show()
            self.meta_keys["Items"].show()
        else:
            self.meta["Items"].hide()
            self.meta_keys["Items"].hide()
            self._start_preview(row)
        self.btn_show.setEnabled(True)
        self.btn_open.setEnabled(True)
        self.btn_more.setEnabled(True)

    # ---- previews -----------------------------------------------------------
    def _start_preview(self, row):
        seq = self._preview_seq
        path = join_path(row["parent"], row["name"])
        if len(path) == 2:
            path += "\\"
        threading.Thread(
            target=self._load_preview,
            args=(seq, path, row["category"], ext_of(row["name"])),
            daemon=True).start()

    def _load_preview(self, seq, path, cat, ext):
        """Runs on a worker thread; emits the result back to the UI thread."""
        payload = None
        try:
            if cat == "img":
                img = QImage(path)
                if img.isNull():
                    img = shell_thumbnail(path, 512)
                if img is not None and not img.isNull():
                    payload = ("image", img)
            elif cat == "vid":
                img = shell_thumbnail(path, 512)
                if img is not None and not img.isNull():
                    payload = ("video", img)
            elif ext in TEXT_EXTS:
                with open(path, "rb") as f:
                    raw = f.read(12000)
                if b"\x00" not in raw:  # skip binary files that look like text
                    payload = ("text", raw.decode("utf-8", "replace"))
        except Exception:
            payload = None
        self.preview_ready.emit(seq, path, payload)

    def on_preview(self, seq, path, payload):
        row = self._row
        if not row or seq != self._preview_seq:
            return
        cur = join_path(row["parent"], row["name"])
        if len(cur) == 2:
            cur += "\\"
        if cur != path:
            return
        if payload is None:
            return  # keep the painted icon
        kind, data = payload
        if kind in ("image", "video"):
            img = data.scaled(236, self.PREVIEW_H - 8,
                              Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pm = rounded_pixmap(QPixmap.fromImage(img), 8)
            if kind == "video":
                pm = with_play_badge(pm)
            self.preview_label.setPixmap(pm)
            self.preview_label.show()
            self.icon_label.hide()
        else:
            self.text_preview.setText(data[:3000] + ("…" if len(data) > 3000 else ""))
            self.text_preview.show()
            self.icon_label.hide()


# ---------------------------------------------------------------------------
# Agent prompt, tools & client
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are EzExp Agent, a desktop assistant with file tools.\n"
    "Available tools:\n"
    "- search_files(query, limit=25): search files in the local PC catalog.\n"
    "- list_folder(path): list direct children in a directory.\n"
    "- open_file(path): open a file using its default application.\n"
    "- create_file(rel_path, content): create a new file in the granted folder.\n"
    "- create_folder(rel_path): create a new folder in the granted folder.\n\n"
    "Rules:\n"
    "1. Grant rule: you may only create files inside the granted folder; if none is granted, say so.\n"
    "2. Honesty rule: never invent a path — call search_files or list_folder first; report tool errors verbatim."
)

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search files in the PC catalog by query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Filename or keyword query to search for",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of search results to return (default: 25)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_folder",
            "description": "List files and folders directly inside a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path of folder to list",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_file",
            "description": "Open a file with the system default application.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to open",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create a file with content inside the granted folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {
                        "type": "string",
                        "description": "Relative path inside the granted folder",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write to the file",
                    },
                },
                "required": ["rel_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_folder",
            "description": "Create a folder inside the granted folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {
                        "type": "string",
                        "description": "Relative path inside the granted folder",
                    },
                },
                "required": ["rel_path"],
            },
        },
    },
]


class ConfirmBridge(QObject):
    _requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending_text = ""
        self._result = False
        self._done = threading.Event()
        self._requested.connect(self._on_requested)

    def _get_parent_widget(self):
        p = self.parent()
        if isinstance(p, QWidget):
            return getattr(p, "main", None) or p
        return None

    def ask_from_worker(self, text: str) -> bool:
        app = QApplication.instance()
        ui_thread = app.thread() if app is not None else self.thread()
        if QThread.currentThread() == ui_thread:
            ret = QMessageBox.question(
                self._get_parent_widget(),
                "EzExp Agent",
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return ret == QMessageBox.StandardButton.Yes

        self._pending_text = text
        self._result = False
        self._done.clear()
        self._requested.emit()
        self._done.wait()
        return self._result

    def _on_requested(self):
        try:
            ret = QMessageBox.question(
                self._get_parent_widget(),
                "EzExp Agent",
                self._pending_text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            self._result = (ret == QMessageBox.StandardButton.Yes)
        finally:
            self._done.set()


class AgentClient(QObject):
    agent_text = Signal(str)
    agent_error = Signal(str)

    def __init__(self, parent=None, config=None, confirmer=None, confirm_bridge=None):
        super().__init__(parent)
        self.nam = QNetworkAccessManager(self)
        self.config = config
        self.confirmer = confirmer
        self.confirm_bridge = confirm_bridge
        self._current_reply = None

    def _confirm_action(self, text: str) -> bool:
        if self.confirmer is not None:
            return bool(self.confirmer(text))

        if self.confirm_bridge is not None:
            return bool(self.confirm_bridge.ask_from_worker(text))

        app = QApplication.instance()
        ui_thread = app.thread() if app is not None else None
        if ui_thread is not None and QThread.currentThread() != ui_thread:
            return False

        ret = QMessageBox.question(
            None,
            "Agent Permission Request",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return ret == QMessageBox.StandardButton.Yes

    def send(self, messages):
        try:
            cfg = self.config if self.config is not None else load_agent_config()
            api_key = (cfg.get("api_key") or "").strip()
            base_url = (cfg.get("base_url") or "").strip().rstrip("/")
            model = (cfg.get("model") or "").strip() or "meta/llama-3.3-70b-instruct"

            if not api_key:
                self.agent_error.emit("Missing API key. Please configure your API key in Endpoint settings.")
                return
            if not base_url:
                self.agent_error.emit("Missing Base URL. Please configure the endpoint in Endpoint settings.")
                return

            if isinstance(messages, str):
                msgs = [{"role": "user", "content": messages}]
            else:
                msgs = list(messages)

            if not msgs or msgs[0].get("role") != "system":
                msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + msgs

            self._send_round(msgs, 0, base_url, model, api_key)
        except Exception as e:
            self.agent_error.emit(f"Send failed: {e}")

    def _send_round(self, messages, round_count, base_url, model, api_key):
        if round_count >= 6:
            self.agent_error.emit("Tool round limit reached (max 6 rounds).")
            return

        payload = {
            "model": model,
            "messages": messages,
            "tools": AGENT_TOOLS,
        }
        body = json.dumps(payload).encode("utf-8")

        url = f"{base_url}/chat/completions"
        req = QNetworkRequest(QUrl(url))
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        req.setRawHeader(b"Authorization", QByteArray(f"Bearer {api_key}".encode("utf-8")))

        reply = self.nam.post(req, body)
        self._current_reply = reply
        reply.finished.connect(
            lambda: self._on_reply(reply, messages, round_count, base_url, model, api_key)
        )

    def _on_reply(self, reply, messages, round_count, base_url, model, api_key):
        try:
            err = reply.error()
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            raw = bytes(reply.readAll())
            reply.deleteLater()
            if self._current_reply == reply:
                self._current_reply = None

            if err != QNetworkReply.NetworkError.NoError or (status is not None and status >= 400):
                body_excerpt = raw[:200].decode("utf-8", "replace")
                if status:
                    self.agent_error.emit(f"HTTP {status}: {body_excerpt}")
                else:
                    self.agent_error.emit(f"Network error: {reply.errorString()} {body_excerpt}")
                return

            try:
                resp = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self.agent_error.emit(f"Bad JSON response: {e}")
                return

            choices = resp.get("choices")
            if not choices or not isinstance(choices, list):
                self.agent_error.emit("No choices returned in API response.")
                return

            choice = choices[0]
            msg = choice.get("message", {})
            tool_calls = msg.get("tool_calls")

            if tool_calls and isinstance(tool_calls, list):
                messages.append(msg)
                for tc in tool_calls:
                    call_id = tc.get("id", "")
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", "{}")
                    tool_result = self.dispatch_tool(fn_name, fn_args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": fn_name,
                        "content": str(tool_result),
                    })
                self._send_round(messages, round_count + 1, base_url, model, api_key)
            else:
                content = msg.get("content") or ""
                self.agent_text.emit(content)
        except Exception as ex:
            self.agent_error.emit(f"Client error: {ex}")

    def dispatch_tool(self, name, args):
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}

        if name == "search_files":
            query = args.get("query", "")
            limit = args.get("limit", 25)
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = 25
            rows, _ = do_search(thread_conn(), query, None, "all", limit)
            if not rows:
                return "No files found."
            return "\n".join(f"{r['name']} — {r['parent']} ({r.get('kind', '')})" for r in rows)
        elif name == "list_folder":
            path = args.get("path", "")
            rows, _ = do_search(thread_conn(), "", place=path, limit=100)
            if not rows:
                return "No files found."
            return "\n".join(f"{r['name']} — {r['parent']} ({r.get('kind', '')})" for r in rows)
        elif name == "open_file":
            path = args.get("path", "")
            try:
                os.startfile(path)
                return "opened"
            except OSError as e:
                return str(e)
        elif name == "create_file":
            return self._tool_create_file(args.get("rel_path", ""), args.get("content", ""))
        elif name == "create_folder":
            return self._tool_create_folder(args.get("rel_path", ""))
        else:
            return f"Error: Unknown tool '{name}'"

    def _tool_create_file(self, rel_path, content):
        cfg = self.config if self.config is not None else load_agent_config()
        grant_dir = (cfg.get("grant_dir") or "").strip()
        if not grant_dir:
            return "Error: grant_dir is not configured."
        if not rel_path or not str(rel_path).strip():
            return "Error: rel_path cannot be empty."
        rel_path = str(rel_path).strip()
        if os.path.isabs(rel_path) or rel_path.startswith("/") or rel_path.startswith("\\") or re.match(r"^[a-zA-Z]:", rel_path):
            return "Error: Path escapes granted folder (absolute paths not allowed)."
        norm_grant = os.path.realpath(os.path.abspath(grant_dir))
        norm_target = os.path.realpath(os.path.abspath(os.path.join(norm_grant, rel_path)))
        try:
            common = os.path.commonpath([norm_grant, norm_target])
        except ValueError:
            return "Error: Path escapes granted folder."
        if common.lower() != norm_grant.lower() or norm_target.lower() == norm_grant.lower():
            return "Error: Path escapes granted folder."
        parts = [p for p in re.split(r"[\\/]", rel_path) if p]
        if ".." in parts:
            return "Error: Path escapes granted folder."
        if os.path.exists(norm_target):
            return f"Error: Target already exists: {norm_target}"

        confirm_text = f"Create file at:\n{norm_target}\nContent length: {len(content)} characters.\nAllow?"
        if not self._confirm_action(confirm_text):
            return "user declined"

        try:
            os.makedirs(os.path.dirname(norm_target), exist_ok=True)
            with open(norm_target, "x", encoding="utf-8") as f:
                f.write(content)
            return norm_target
        except Exception as e:
            return f"Error: Failed to create file: {e}"

    def _tool_create_folder(self, rel_path):
        cfg = self.config if self.config is not None else load_agent_config()
        grant_dir = (cfg.get("grant_dir") or "").strip()
        if not grant_dir:
            return "Error: grant_dir is not configured."
        if not rel_path or not str(rel_path).strip():
            return "Error: rel_path cannot be empty."
        rel_path = str(rel_path).strip()
        if os.path.isabs(rel_path) or rel_path.startswith("/") or rel_path.startswith("\\") or re.match(r"^[a-zA-Z]:", rel_path):
            return "Error: Path escapes granted folder (absolute paths not allowed)."
        norm_grant = os.path.realpath(os.path.abspath(grant_dir))
        norm_target = os.path.realpath(os.path.abspath(os.path.join(norm_grant, rel_path)))
        try:
            common = os.path.commonpath([norm_grant, norm_target])
        except ValueError:
            return "Error: Path escapes granted folder."
        if common.lower() != norm_grant.lower() or norm_target.lower() == norm_grant.lower():
            return "Error: Path escapes granted folder."
        parts = [p for p in re.split(r"[\\/]", rel_path) if p]
        if ".." in parts:
            return "Error: Path escapes granted folder."
        if os.path.exists(norm_target):
            return f"Error: Target already exists: {norm_target}"

        confirm_text = f"Create folder at:\n{norm_target}\nAllow?"
        if not self._confirm_action(confirm_text):
            return "user declined"

        try:
            os.makedirs(norm_target, exist_ok=False)
            return norm_target
        except Exception as e:
            return f"Error: Failed to create folder: {e}"


# ---------------------------------------------------------------------------
# Spotlight-style quick search
# ---------------------------------------------------------------------------

class BridgeWorker(QThread):
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, task, grant_dir, harness_path="", harness_python="py", client=None, bridge=None, parent=None, mode="single", current_folder=None):
        super().__init__(parent)
        self.task = task
        self.grant_dir = grant_dir
        self.harness_path = harness_path
        self.harness_python = harness_python or "py"
        self.client = client
        self.bridge = bridge
        self.mode = mode or "single"
        self.current_folder = current_folder
        self._proc = None

    def __del__(self):
        self._terminate_proc()

    def _terminate_proc(self):
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        proc.kill()
            except Exception:
                pass

    def run(self):
        try:
            cmd = [self.harness_python, "-m", "harness.host_runner"]
            if self.grant_dir:
                cmd.extend(["--workspace", self.grant_dir])

            popen_kwargs = {
                "cwd": self.harness_path if self.harness_path else None,
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

            self._proc = subprocess.Popen(cmd, **popen_kwargs)

            q = queue.Queue()
            recent_stderr = collections.deque(maxlen=30)

            def stdout_reader():
                if self._proc and self._proc.stdout:
                    for line in iter(self._proc.stdout.readline, ""):
                        q.put(line)
                q.put(None)

            def stderr_reader():
                if self._proc and self._proc.stderr:
                    for line in iter(self._proc.stderr.readline, ""):
                        recent_stderr.append(line)

            t_out = threading.Thread(target=stdout_reader, daemon=True)
            t_out.start()
            t_err = threading.Thread(target=stderr_reader, daemon=True)
            t_err.start()

            # 1. Setup line
            tools_manifest = []
            for t_spec in AGENT_TOOLS:
                fn = t_spec["function"]
                tools_manifest.append({
                    "name": fn["name"],
                    "description": fn["description"],
                    "parameters": fn["parameters"],
                })

            setup_msg = {
                "type": "setup",
                "providers": ["groq", "openrouter"],
                "disable": ["shell", "read_file", "write_file", "edit_file", "list_dir"],
                "tools": tools_manifest,
            }
            self._proc.stdin.write(json.dumps(setup_msg) + "\n")
            self._proc.stdin.flush()

            # 2. Read until ready (30s timeout)
            try:
                ready_line = q.get(timeout=30.0)
            except queue.Empty:
                self._terminate_proc()
                self.failed.emit("Harness runner timed out waiting for ready (30s).")
                return

            if ready_line is None:
                ret = self._proc.poll()
                err_detail = "".join(recent_stderr).strip()
                self._terminate_proc()
                suffix = f": {err_detail}" if err_detail else f" (code {ret})"
                self.failed.emit(f"Harness runner exited unexpectedly during setup{suffix}")
                return

            ready_line = ready_line.strip()
            try:
                ready_data = json.loads(ready_line)
            except Exception:
                self._terminate_proc()
                self.failed.emit(f"Protocol error: invalid ready line: {ready_line}")
                return

            if ready_data.get("type") == "error":
                self._terminate_proc()
                self.failed.emit(ready_data.get("message", "Runner initialization error"))
                return

            if ready_data.get("type") != "ready":
                self._terminate_proc()
                self.failed.emit(f"Protocol error: expected 'ready', got '{ready_data.get('type')}'")
                return

            # Fail closed assertion
            expected_names = sorted(["search_files", "list_folder", "open_file", "create_file", "create_folder"])
            actual_names = sorted(ready_data.get("tools", []))
            if actual_names != expected_names:
                msg = f"Refused to run: harness tool set mismatch — expected EzExp tools only, got: {actual_names}."
                self._terminate_proc()
                self.failed.emit(msg)
                return

            # 3. Write task line
            task_msg = {
                "type": "task",
                "text": self.task,
                "mode": self.mode,
                "context": {"current_folder": self.current_folder if self.current_folder else None},
            }
            self._proc.stdin.write(json.dumps(task_msg) + "\n")
            self._proc.stdin.flush()

            # 4. Wire client bridge
            client = self.client if self.client is not None else AgentClient(config=load_agent_config())
            orig_bridge = getattr(client, "confirm_bridge", None)
            if self.bridge is not None:
                client.confirm_bridge = self.bridge

            try:
                while True:
                    try:
                        line = q.get(timeout=180.0)
                    except queue.Empty:
                        self.failed.emit("Harness runner timed out during task execution.")
                        break

                    if line is None:
                        ret = self._proc.poll()
                        err_detail = "".join(recent_stderr).strip()
                        suffix = f": {err_detail}" if err_detail else f" (code {ret})"
                        self.failed.emit(f"Harness runner exited unexpectedly{suffix}")
                        break

                    line = line.strip()
                    if not line:
                        continue

                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        self.failed.emit(f"Protocol error: malformed JSON from runner: {line[:200]}")
                        break

                    m_type = msg.get("type")
                    if m_type == "tool_call":
                        t_id = msg.get("id")
                        t_name = msg.get("name")
                        t_args = msg.get("arguments", {})
                        try:
                            res = client.dispatch_tool(t_name, t_args)
                            if isinstance(res, str) and (res.startswith("Error:") or res == "user declined"):
                                ok = False
                                out = res
                            else:
                                ok = True
                                out = str(res)
                        except Exception as ex:
                            ok = False
                            out = f"Error: {str(ex)}"

                        res_msg = {
                            "type": "tool_result",
                            "id": t_id,
                            "ok": ok,
                            "output": out,
                        }
                        self._proc.stdin.write(json.dumps(res_msg) + "\n")
                        self._proc.stdin.flush()

                    elif m_type == "final":
                        text = msg.get("text", "")
                        parts = []
                        tokens = msg.get("tokens")
                        if tokens is not None:
                            parts.append(f"Tokens: {tokens}")
                        ms = msg.get("ms")
                        if ms is not None:
                            parts.append(f"Time: {ms}ms")
                        if parts:
                            final_text = f"{text}\n[{', '.join(parts)}]"
                        else:
                            final_text = text
                        self.done.emit(final_text)
                        break

                    elif m_type == "error":
                        self.failed.emit(msg.get("message", "Unknown runner error"))
                        break

                    else:
                        self.failed.emit(f"Protocol error: unexpected message '{m_type}'")
                        break
            finally:
                client.confirm_bridge = orig_bridge

        except Exception as e:
            self.failed.emit(str(e))
        finally:
            self._terminate_proc()


HarnessWorker = BridgeWorker


class ProbeWorker(QThread):
    probed = Signal(bool, str)

    def __init__(self, cfg=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg

    def run(self):
        ready, status_msg = agent_status(self.cfg)
        self.probed.emit(ready, status_msg)


class Spotlight(FramelessWindow):
    W, H = 660, 470

    def __init__(self, worker: SearchWorker):
        super().__init__()
        self.setWindowTitle("EzExp Quick Search")
        self.main = None  # MainWindow, wired in main()
        self.setMinimumSize(self.W, 300)
        self.worker = worker
        self.worker.have.connect(self.on_results)
        self.spot_seq = 0

        panel = QFrame(self)
        panel.setObjectName("spotPanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Mode selector chips & Endpoint button
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(18, 12, 18, 0)
        mode_row.setSpacing(6)

        self.btn_search = QPushButton("Search", panel)
        self.btn_search.setCheckable(True)
        self.btn_search.setChecked(True)
        self.btn_search.setObjectName("spotModeBtn")
        self.btn_search.clicked.connect(lambda: self.set_mode("search"))
        mode_row.addWidget(self.btn_search)

        self.btn_agent = QPushButton("Agent", panel)
        self.btn_agent.setCheckable(True)
        self.btn_agent.setChecked(False)
        self.btn_agent.setObjectName("spotModeBtn")
        self.btn_agent.clicked.connect(lambda: self.set_mode("agent"))
        mode_row.addWidget(self.btn_agent)

        mode_row.addStretch(1)

        self.btn_toggle_workspace = QPushButton("Workspace", panel)
        self.btn_toggle_workspace.setCheckable(True)
        self.btn_toggle_workspace.setChecked(True)
        self.btn_toggle_workspace.setObjectName("spotWorkspaceBtn")
        self.btn_toggle_workspace.clicked.connect(lambda: self.toggle_workspace_pane())
        self.btn_toggle_workspace.hide()
        mode_row.addWidget(self.btn_toggle_workspace)

        self.btn_endpoint = QPushButton("Endpoint…", panel)
        self.btn_endpoint.setObjectName("spotEndpointBtn")
        self.btn_endpoint.clicked.connect(self._open_endpoint)
        self.btn_endpoint.hide()
        mode_row.addWidget(self.btn_endpoint)

        lay.addLayout(mode_row)

        srow = QHBoxLayout()
        srow.setContentsMargins(18, 14, 18, 14)
        icon = QLabel(panel)
        icon.setPixmap(search_glyph().pixmap(22, 22))
        srow.addWidget(icon)
        srow.addSpacing(10)
        self.edit = QLineEdit(panel)
        self.edit.setObjectName("spotInput")
        self.edit.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.edit.setPlaceholderText("Search your entire PC")
        self.edit.textChanged.connect(self.on_text)
        srow.addWidget(self.edit, 1)
        lay.addLayout(srow)

        self.tree = QTreeWidget(panel)
        self.tree.setColumnCount(1)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setFocusPolicy(Qt.NoFocus)
        self.tree.setItemDelegateForColumn(0, SpotDelegate(self.tree))
        lay.addWidget(self.tree, 1)

        # Agent view: horizontal splitter (conversation on left, workspace pane on right)
        self.agent_splitter = QSplitter(Qt.Orientation.Horizontal, panel)
        self.agent_splitter.setObjectName("spotAgentSplitter")
        self.agent_splitter.hide()

        self.transcript = QPlainTextEdit(self.agent_splitter)
        self.transcript.setObjectName("spotTranscript")
        self.transcript.setReadOnly(True)
        self.transcript.hide()
        self.agent_splitter.addWidget(self.transcript)

        # Workspace pane (viewer only, bounded scope)
        self.workspace_pane = QFrame(self.agent_splitter)
        self.workspace_pane.setObjectName("spotWorkspacePane")
        ws_lay = QVBoxLayout(self.workspace_pane)
        ws_lay.setContentsMargins(8, 6, 8, 6)
        ws_lay.setSpacing(6)

        ws_header_row = QHBoxLayout()
        ws_title = QLabel("Workspace", self.workspace_pane)
        ws_title.setObjectName("wsTitle")
        ws_title.setStyleSheet("font-weight: 600; font-size: 12px; color: #1d1d1f;")
        ws_header_row.addWidget(ws_title)
        ws_header_row.addStretch(1)

        self.ws_btn_refresh = QPushButton("Refresh", self.workspace_pane)
        self.ws_btn_refresh.setObjectName("wsBtnRefresh")
        self.ws_btn_refresh.clicked.connect(self.refresh_workspace_pane)
        ws_header_row.addWidget(self.ws_btn_refresh)

        self.ws_btn_collapse = QPushButton("✕", self.workspace_pane)
        self.ws_btn_collapse.setObjectName("wsBtnCollapse")
        self.ws_btn_collapse.setFixedSize(20, 20)
        self.ws_btn_collapse.clicked.connect(lambda: self.toggle_workspace_pane(False))
        ws_header_row.addWidget(self.ws_btn_collapse)
        ws_lay.addLayout(ws_header_row)

        self.ws_lbl_context = QLabel("Context: (none)", self.workspace_pane)
        self.ws_lbl_context.setObjectName("wsLblContext")
        self.ws_lbl_context.setStyleSheet("font-size: 11px; color: #6e6e73;")
        self.ws_lbl_context.setWordWrap(True)
        ws_lay.addWidget(self.ws_lbl_context)

        self.ws_lbl_grant = QLabel("Grant: (none)", self.workspace_pane)
        self.ws_lbl_grant.setObjectName("wsLblGrant")
        self.ws_lbl_grant.setStyleSheet("font-size: 11px; color: #6e6e73;")
        self.ws_lbl_grant.setWordWrap(True)
        ws_lay.addWidget(self.ws_lbl_grant)

        self.ws_file_list = QTableWidget(self.workspace_pane)
        self.ws_file_list.setObjectName("wsFileList")
        self.ws_file_list.setColumnCount(3)
        self.ws_file_list.setHorizontalHeaderLabels(["Name", "Size", "Modified"])
        self.ws_file_list.horizontalHeader().setStretchLastSection(True)
        self.ws_file_list.verticalHeader().setVisible(False)
        self.ws_file_list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ws_file_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.ws_file_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ws_file_list.itemSelectionChanged.connect(self._on_ws_file_selected)
        ws_lay.addWidget(self.ws_file_list, 1)

        self.ws_preview = QPlainTextEdit(self.workspace_pane)
        self.ws_preview.setObjectName("wsPreview")
        self.ws_preview.setReadOnly(True)
        self.ws_preview.setPlaceholderText("Select a file to preview…")
        self.ws_preview.setMaximumHeight(140)
        ws_lay.addWidget(self.ws_preview, 1)

        self.agent_splitter.addWidget(self.workspace_pane)
        self.agent_splitter.setSizes([420, 240])

        lay.addWidget(self.agent_splitter, 1)

        self.foot = QLabel("  ⏎  Open (folders in EzExp)      Ctrl ⏎  Show in Folder      Alt ⏎  In EzExp      Esc  Close  ", panel)
        self.foot.setObjectName("spotFooter")
        self.foot.setFixedHeight(34)
        lay.addWidget(self.foot)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)
        self.resize(self.W, self.H)

        self.mode = "search"
        self.confirm_bridge = ConfirmBridge(self)
        self.agent_client = AgentClient(self)
        self.agent_client.agent_text.connect(self.on_agent_text)
        self.agent_client.agent_error.connect(self.on_agent_error)
        self.agent_messages = []
        self._bridge_worker = None
        self._harness_banner_shown = False
        self._probe_worker = None
        self._probe_in_progress = False

    def _open_endpoint(self):
        dlg = AgentSettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.agent_client.config = load_agent_config()
            clear_harness_probe_cache()
            if self.mode == "agent":
                self.set_mode("agent")

    def _on_probed(self, ready, status_msg):
        self._probe_in_progress = False
        if self.mode == "agent":
            cur = self.transcript.toPlainText().strip()
            if cur == "Organism Harness: checking…" or not self.agent_messages:
                if status_msg:
                    self.transcript.setPlainText(status_msg)
                else:
                    self.transcript.clear()
            else:
                if status_msg:
                    self._append_status_dedup(status_msg)

    def _append_status_dedup(self, msg):
        msg = msg.strip()
        if not msg:
            return
        current_text = self.transcript.toPlainText().strip()
        if not current_text:
            self.transcript.setPlainText(msg)
            return
        last_block = self.transcript.document().lastBlock().text().strip()
        lines = [l.strip() for l in current_text.splitlines() if l.strip()]
        last_line = lines[-1] if lines else ""
        if last_block == msg or last_line == msg:
            return
        self.transcript.appendPlainText(msg + "\n")

    def set_mode(self, mode):
        self.mode = mode
        if mode == "agent":
            self.btn_search.setChecked(False)
            self.btn_agent.setChecked(True)
            self.tree.hide()
            self.agent_splitter.show()
            self.transcript.show()
            self.btn_endpoint.show()
            self.btn_toggle_workspace.show()
            self.foot.setText("  ⏎  Send      Esc  Close  ")
            self.edit.setPlaceholderText("Ask the agent...")
            self.refresh_workspace_pane()
            cfg = self.agent_client.config if self.agent_client.config is not None else load_agent_config()
            backend = (cfg.get("backend") or "endpoint").lower()
            if backend == "harness":
                hp = (cfg.get("harness_path") or "").strip()
                python_bin = (cfg.get("harness_python") or "py").strip() or "py"
                cache_key = (backend, hp, python_bin)
                if cache_key in _harness_probe_cache:
                    ready, status_msg = agent_status(cfg)
                    if not self.agent_messages:
                        self.transcript.clear()
                        if status_msg:
                            self.transcript.setPlainText(status_msg)
                else:
                    self._probe_in_progress = True
                    if not self.agent_messages:
                        self.transcript.clear()
                        self.transcript.setPlainText("Organism Harness: checking…")
                    if self._probe_worker is not None and self._probe_worker.isRunning():
                        self._probe_worker.quit()
                        self._probe_worker.wait()
                    self._probe_worker = ProbeWorker(cfg, self)
                    self._probe_worker.probed.connect(self._on_probed)
                    self._probe_worker.start()
            else:
                ready, status_msg = agent_status(cfg)
                if not self.agent_messages:
                    self.transcript.clear()
                    if status_msg:
                        self.transcript.setPlainText(status_msg)
        else:
            self.mode = "search"
            self.btn_search.setChecked(True)
            self.btn_agent.setChecked(False)
            self.agent_splitter.hide()
            self.transcript.hide()
            self.tree.show()
            self.btn_toggle_workspace.hide()
            self.btn_endpoint.hide()
            self.foot.setText("  ⏎  Open (folders in EzExp)      Ctrl ⏎  Show in Folder      Alt ⏎  In EzExp      Esc  Close  ")
            self.edit.setPlaceholderText("Search your entire PC")

    def on_text(self, text):
        if self.mode == "search":
            self.spot_seq = self.worker.submit(text, None, "all")

    def on_agent_text(self, text):
        if text:
            self.transcript.appendPlainText(text)
            self.agent_messages.append({"role": "assistant", "content": text})
            self.refresh_workspace_pane()

    def on_agent_error(self, err):
        if err:
            self.transcript.appendPlainText(f"Error: {err}")

    def send_agent_message(self):
        if self._harness_worker is not None and self._harness_worker.isRunning():
            return
        text = self.edit.text().strip()
        if not text:
            return
        if self._probe_in_progress:
            self._append_status_dedup("Organism Harness: checking…")
            return
        cfg = self.agent_client.config if self.agent_client.config is not None else load_agent_config()
        ready, status_msg = agent_status(cfg)
        if not ready:
            if status_msg:
                self._append_status_dedup(status_msg)
            return

        backend = (cfg.get("backend") or "endpoint").lower()
        if backend == "harness":
            self.edit.clear()
            if not self._harness_banner_shown:
                self.transcript.appendPlainText(
                    "Tools: EzExp catalog + grant tools via the harness (native harness tools disabled).\n"
                )
                self._harness_banner_shown = True
            self.transcript.appendPlainText(f"You: {text}\n")
            self.agent_messages.append({"role": "user", "content": text})
            grant_dir = (cfg.get("grant_dir") or "").strip()
            hp = (cfg.get("harness_path") or "").strip()
            hpython = (cfg.get("harness_python") or "py").strip() or "py"
            cur_folder = self.get_current_folder()
            self._start_harness_task(text, grant_dir, hp, hpython, mode="single", current_folder=cur_folder)
        else:
            self.edit.clear()
            self.transcript.appendPlainText(f"You: {text}\n")
            self.agent_messages.append({"role": "user", "content": text})
            self.agent_client.send(self.agent_messages)

    def get_current_folder(self):
        """Returns the absolute path of the folder currently open/navigated in EzExp, or None."""
        if hasattr(self, "_current_folder_override") and self._current_folder_override is not None:
            return os.path.abspath(self._current_folder_override) if self._current_folder_override else None
        if self.main is not None and getattr(self.main, "current_place", None):
            p = self.main.current_place
            return os.path.abspath(p) if p else None
        cf = getattr(self, "current_folder", None)
        return os.path.abspath(cf) if cf else None

    @property
    def _harness_worker(self):
        return self._bridge_worker

    @_harness_worker.setter
    def _harness_worker(self, val):
        self._bridge_worker = val

    def _start_harness_task(self, text, grant_dir, harness_path="", harness_python="py", mode="single", current_folder=None):
        self.edit.setEnabled(False)
        self.foot.setText("  Agent working…      Esc  Close  ")
        if current_folder is None:
            current_folder = self.get_current_folder()
        self._bridge_worker = BridgeWorker(
            text, grant_dir, harness_path, harness_python, client=self.agent_client, bridge=self.confirm_bridge, parent=self, mode=mode, current_folder=current_folder
        )
        self._bridge_worker.done.connect(self._on_harness_done)
        self._bridge_worker.failed.connect(self._on_harness_failed)
        self._bridge_worker.start()

    def _on_harness_done(self, final_text):
        self.edit.setEnabled(True)
        self.foot.setText("  ⏎  Send      Esc  Close  ")
        self.edit.setFocus()
        if final_text:
            self.transcript.appendPlainText(final_text + "\n")
            self.agent_messages.append({"role": "assistant", "content": final_text})
        self.refresh_workspace_pane()

    def _on_harness_failed(self, err_text):
        self.edit.setEnabled(True)
        self.foot.setText("  ⏎  Send      Esc  Close  ")
        self.edit.setFocus()
        self.transcript.appendPlainText(f"Error: {err_text}\n")
        self.refresh_workspace_pane()

    def toggle_workspace_pane(self, checked=None):
        """Toggle visibility of the IDE-style workspace pane."""
        if checked is None:
            new_vis = self.workspace_pane.isHidden()
        else:
            new_vis = bool(checked)
        self.workspace_pane.setVisible(new_vis)
        self.btn_toggle_workspace.setChecked(new_vis)
        if new_vis:
            self.refresh_workspace_pane()
            sizes = self.agent_splitter.sizes()
            if len(sizes) == 2 and sizes[1] == 0:
                tot = sum(sizes) or self.width()
                self.agent_splitter.setSizes([int(tot * 0.6), int(tot * 0.4)])

    def refresh_workspace_pane(self):
        """Update context/grant labels and refresh file list from grant_dir."""
        cur_folder = self.get_current_folder()
        self.ws_lbl_context.setText(f"Context: {cur_folder}" if cur_folder else "Context: (none)")

        cfg = self.agent_client.config if self.agent_client.config is not None else load_agent_config()
        grant_dir = (cfg.get("grant_dir") or "").strip() or getattr(self, "grant_dir", None) or ""
        self.ws_lbl_grant.setText(f"Grant: {grant_dir}" if grant_dir else "Grant: (none)")

        self.ws_file_list.setRowCount(0)
        self.ws_preview.clear()

        if not grant_dir or not os.path.isdir(grant_dir):
            return

        try:
            entries = []
            for entry in os.scandir(grant_dir):
                entries.append(entry)
            entries.sort(key=lambda e: (not e.is_dir(), e.name.lower()))

            self.ws_file_list.setRowCount(len(entries))
            for row_idx, entry in enumerate(entries):
                try:
                    stat = entry.stat()
                    size_str = f"{stat.st_size:,} B" if not entry.is_dir() else "<DIR>"
                    mtime_str = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    size_str = "—"
                    mtime_str = "—"

                item_name = QTableWidgetItem(entry.name)
                item_name.setData(Qt.ItemDataRole.UserRole, entry.path)
                item_size = QTableWidgetItem(size_str)
                item_mtime = QTableWidgetItem(mtime_str)

                self.ws_file_list.setItem(row_idx, 0, item_name)
                self.ws_file_list.setItem(row_idx, 1, item_size)
                self.ws_file_list.setItem(row_idx, 2, item_mtime)
        except Exception:
            pass

    def _on_ws_file_selected(self):
        """Read-only preview of selected workspace file (capped at 200 KB; binary note on non-text)."""
        items = self.ws_file_list.selectedItems()
        if not items:
            self.ws_preview.clear()
            return
        row = items[0].row()
        name_item = self.ws_file_list.item(row, 0)
        if not name_item:
            self.ws_preview.clear()
            return
        path = name_item.data(Qt.ItemDataRole.UserRole)
        if not path or not os.path.isfile(path):
            self.ws_preview.clear()
            return

        try:
            sz = os.path.getsize(path)
            if sz > 200 * 1024:
                self.ws_preview.setPlainText("binary or too large to preview")
                return

            with open(path, "rb") as fh:
                data = fh.read(200 * 1024 + 1)
            if len(data) > 200 * 1024:
                self.ws_preview.setPlainText("binary or too large to preview")
                return

            if b"\x00" in data:
                self.ws_preview.setPlainText("binary or too large to preview")
                return

            try:
                text = data.decode("utf-8")
                self.ws_preview.setPlainText(text)
            except UnicodeDecodeError:
                self.ws_preview.setPlainText("binary or too large to preview")
        except Exception:
            self.ws_preview.setPlainText("binary or too large to preview")

    def on_results(self, seq, q, rows, note):
        if seq != self.spot_seq:
            return
        self.tree.clear()
        if not q.strip():
            manual = get_quicklaunch()
            existing = set()
            candidates = []
            for p in manual:
                np = os.path.normpath(p)
                if np.lower() not in existing:
                    existing.add(np.lower())
                    candidates.append(np)
            counts = get_launch_counts()
            frequent = sorted(
                (p for p in counts.keys() if os.path.normpath(p).lower() not in existing),
                key=lambda p: -counts[p],
            )
            for p in frequent:
                np = os.path.normpath(p)
                if np.lower() not in existing:
                    existing.add(np.lower())
                    candidates.append(np)

            ql_rows = []
            conn = thread_conn()
            for p in candidates:
                p_lookup = p
                if len(p_lookup) == 3 and p_lookup[1:3] == ":\\":
                    p_lookup = p_lookup[:2]
                parent, name = split_path(p_lookup)
                cur = conn.execute(
                    "SELECT " + ROW_FIELDS + " FROM files WHERE parent = ? COLLATE NOCASE AND name = ? COLLATE NOCASE LIMIT 1",
                    (parent, name),
                )
                r = cur.fetchone()
                if r:
                    ql_rows.append(_row_dict(r))
                else:
                    try:
                        st = os.stat(p)
                        is_dir = 1 if os.path.isdir(p) else 0
                        ql_rows.append({
                            "parent": parent,
                            "name": name,
                            "is_dir": is_dir,
                            "size": st.st_size if not is_dir else 0,
                            "mtime": st.st_mtime,
                            "category": category_for(name, bool(is_dir)),
                            "kind": kind_for(name, bool(is_dir)),
                        })
                    except OSError:
                        pass

            if ql_rows:
                self.tree.addTopLevelItem(self._header("Quick Launch"))
                for r in ql_rows:
                    self.tree.addTopLevelItem(self._item(r))

            recent_rows = rows[:8]
            if recent_rows:
                self.tree.addTopLevelItem(self._header("Recent"))
                for r in recent_rows:
                    self.tree.addTopLevelItem(self._item(r))

            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                data = item.data(0, Qt.UserRole)
                if data and not data.get("__header__"):
                    self.tree.setCurrentItem(item)
                    break
            return
        if not rows:
            web_item = QTreeWidgetItem([f"Search the web for '{q}'"])
            web_item.setData(0, Qt.UserRole, {"__web__": True, "name": q})
            self.tree.addTopLevelItem(web_item)
            self.tree.setCurrentItem(web_item)
            return

        def row_path_norm(r):
            p = join_path(r["parent"], r["name"])
            if len(p) == 2 and p[1] == ":":
                p += "\\"
            return os.path.normpath(p).lower()

        pins_set = {os.path.normpath(p).lower() for p in get_quicklaunch()}
        counts_map = {os.path.normpath(p).lower(): c for p, c in get_launch_counts().items()}

        launchables = []
        launchable_paths = set()
        for r in rows:
            p_norm = row_path_norm(r)
            if p_norm in launchable_paths:
                continue
            name_lower = r["name"].lower()
            if name_lower.endswith(".lnk") or name_lower.endswith(".exe"):
                parent_norm = "\\" + r["parent"].strip("\\").lower() + "\\"
                if ("\\start menu\\" in parent_norm or "\\programs\\" in parent_norm or
                        p_norm in pins_set or counts_map.get(p_norm, 0) > 0):
                    launchable_paths.add(p_norm)
                    launchables.append(r)
                    if len(launchables) == 5:
                        break

        if launchables:
            self.tree.addTopLevelItem(self._header("Apps"))
            for r in launchables:
                self.tree.addTopLevelItem(self._item(r))

        remaining = [r for r in rows if row_path_norm(r) not in launchable_paths]
        if remaining:
            self.tree.addTopLevelItem(self._header("Top Hit"))
            self.tree.addTopLevelItem(self._item(remaining[0]))
            groups = [("folder", "Folders"), ("doc", "Documents"), ("img", "Images"),
                      ("vid", "Video"), ("aud", "Audio"), ("zip", "Archives"),
                      ("code", "Code"), ("other", "Other")]
            rest = remaining[1:]
            for cat, label in groups:
                hits = [r for r in rest if r["category"] == cat][:3]
                if hits:
                    self.tree.addTopLevelItem(self._header(label))
                    for r in hits:
                        self.tree.addTopLevelItem(self._item(r))

        web_item = QTreeWidgetItem([f"Search the web for '{q}'"])
        web_item.setData(0, Qt.UserRole, {"__web__": True, "name": q})
        self.tree.addTopLevelItem(web_item)
        self.tree.setCurrentItem(self.tree.topLevelItem(1))

    def _header(self, text):
        item = QTreeWidgetItem([text])
        item.setFlags(Qt.ItemIsEnabled)
        item.setData(0, Qt.UserRole, {"__header__": True, "text": text})
        return item

    def _item(self, row):
        item = QTreeWidgetItem([row["name"]])
        item.setData(0, Qt.UserRole, row)
        return item

    def current_row(self):
        item = self.tree.currentItem()
        if not item:
            return None
        row = item.data(0, Qt.UserRole)
        if not row or row.get("__header__"):
            return None
        return row

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self.mode == "agent":
                self.send_agent_message()
                return
            row = self.current_row()
            if row:
                if row.get("__web__"):
                    url_str = "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(row.get("name", ""))
                    QDesktopServices.openUrl(QUrl(url_str))
                    self.hide()
                    return
                path = join_path(row["parent"], row["name"])
                if len(path) == 2:
                    path += "\\"
                if e.modifiers() & Qt.ControlModifier:
                    reveal_in_explorer(path)
                    self.hide()
                elif e.modifiers() & Qt.AltModifier and row["is_dir"]:
                    if self.main is not None:
                        self.main.show()
                        self.main.raise_()
                        self.main.activateWindow()
                        self.main.drill_to_path(path, row["name"])
                    self.hide()
                elif row["is_dir"]:
                    if self.main is not None:
                        self.main.show()
                        self.main.raise_()
                        self.main.activateWindow()
                        self.main.drill_to_path(path, row["name"])
                    else:
                        try:
                            os.startfile(path)
                        except OSError:
                            pass
                    self.hide()
                else:
                    try:
                        os.startfile(path)
                    except OSError:
                        pass
                    self.hide()
            return
        if e.key() == Qt.Key_Down:
            if self.mode == "search":
                self._move_sel(1)
                return
        if e.key() == Qt.Key_Up:
            if self.mode == "search":
                self._move_sel(-1)
                return
        super().keyPressEvent(e)

    def _move_sel(self, delta):
        cur = self.tree.currentItem()
        if cur is None:
            i = 0
        else:
            i = self.tree.indexOfTopLevelItem(cur) + delta
        i = max(0, min(self.tree.topLevelItemCount() - 1, i))
        item = self.tree.topLevelItem(i)
        while item and item.data(0, Qt.UserRole) and item.data(0, Qt.UserRole).get("__header__"):
            i += delta
            if i < 0 or i >= self.tree.topLevelItemCount():
                return
            item = self.tree.topLevelItem(i)
        if item:
            self.tree.setCurrentItem(item)

    def open_spotlight(self):
        self.set_mode("search")
        self.edit.clear()
        self.spot_seq = self.worker.submit("", None, "all")
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.move(scr.x() + (scr.width() - self.W) // 2,
                  scr.y() + int(scr.height() * 0.16))
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()

    def keyReleaseEvent(self, e):
        super().keyReleaseEvent(e)

    def hideEvent(self, e):
        self.edit.clear()
        super().hideEvent(e)


class SpotDelegate(QStyledItemDelegate):
    ROW_H = 40
    HEAD_H = 28

    def sizeHint(self, option, index):
        row = index.data(Qt.UserRole)
        head = row and row.get("__header__")
        return QSize(200, self.HEAD_H if head else self.ROW_H)

    def paint(self, p, option, index):
        row = index.data(Qt.UserRole)
        if not row:
            return super().paint(p, option, index)
        p.save()
        r = option.rect
        if row.get("__header__"):
            f = QFont(option.font)
            f.setPointSizeF(7.5)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(SUBTLE))
            p.drawText(r.adjusted(20, 0, -12, -4), Qt.AlignLeft | Qt.AlignBottom, row["text"])
            p.restore()
            return
        selected = bool(option.state & QStyle.State_Selected)
        if selected:
            p.fillRect(r.adjusted(8, 1, -8, -1), QColor(ACCENT))
        if row.get("__web__"):
            icon = search_glyph()
            p.drawPixmap(r.left() + 16, r.top() + (r.height() - 22) // 2, icon.pixmap(22, 22))
            f = QFont(option.font)
            f.setPointSizeF(10.0)
            p.setFont(f)
            p.setPen(QColor("#ffffff") if selected else QColor(TEXT))
            fm = QFontMetrics(f)
            label = f"Search the web for '{row.get('name', '')}'"
            name = fm.elidedText(label, Qt.ElideRight, r.width() - 80)
            p.drawText(r.left() + 54, r.top(), r.width() - 80, r.height(),
                       Qt.AlignLeft | Qt.AlignVCenter, name)
            p.restore()
            return

        icon = spot_row_icon(row)
        p.drawPixmap(r.left() + 16, r.top() + (r.height() - 26) // 2, icon.pixmap(26, 26))
        f = QFont(option.font)
        f.setPointSizeF(row["category"] == "folder" and 10.5 or 10.0)
        p.setFont(f)
        p.setPen(QColor("#ffffff") if selected else QColor(TEXT))
        fm = QFontMetrics(f)
        name = fm.elidedText(row["name"], Qt.ElideRight, r.width() - 320)
        p.drawText(r.left() + 54, r.top(), r.width() - 320, r.height(),
                   Qt.AlignLeft | Qt.AlignVCenter, name)
        small = QFont(option.font)
        small.setPointSizeF(8.0)
        p.setFont(small)
        p.setPen(QColor(255, 255, 255, 217) if selected else QColor(SUBTLE))
        right = row["kind"] + ("" if row["is_dir"] else "  ·  " + human_size(row["size"]))
        p.drawText(r.right() - 260, r.top(), 248, r.height(),
                   Qt.AlignRight | Qt.AlignVCenter,
                   QFontMetrics(small).elidedText(right, Qt.ElideLeft, 248))
        p.restore()


# ---------------------------------------------------------------------------
# Global hotkey (Alt+Space)
# ---------------------------------------------------------------------------

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
HOTKEY_ID = 0xB00B


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        try:
            if event_type == b"windows_generic_MSG":
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.callback()
        except Exception:
            try:
                with open(os.path.join(APP_DIR, "error.log"), "a",
                          encoding="utf-8") as fh:
                    fh.write("\n=== %s ===\n" % time.strftime(
                        "%Y-%m-%d %H:%M:%S"))
                    fh.write(traceback.format_exc())
            except Exception:
                pass
        return False, 0


def register_hotkey():
    user32 = ctypes.windll.user32
    if user32.RegisterHotKey(None, HOTKEY_ID, MOD_ALT | MOD_NOREPEAT, 0x20):
        return "Alt+Space"
    if user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x20):
        return "Ctrl+Alt+Space"
    if user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_NOREPEAT, 0x20):
        return "Ctrl+Space"
    return None


def unregister_hotkey():
    try:
        ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_single_instance_lock = None

def acquire_single_instance():
    """Hold a lock file so a second launch exits politely. QLockFile
    detects locks left behind by a crashed process and takes them over.
    Returns the held QLockFile, or None if another instance holds it."""
    global _single_instance_lock
    os.makedirs(APP_DIR, exist_ok=True)
    lock = QLockFile(os.path.join(APP_DIR, "ezexp.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        return None
    _single_instance_lock = lock
    return lock

def install_crash_log():
    """A windowed exe dies silently on an unhandled exception. Append
    the traceback to APP_DIR/error.log so it can be sent for support."""
    def hook(exc_type, exc, tb):
        try:
            with open(os.path.join(APP_DIR, "error.log"), "a",
                      encoding="utf-8") as fh:
                fh.write("\n=== %s ===\n" % time.strftime(
                    "%Y-%m-%d %H:%M:%S"))
                traceback.print_exception(exc_type, exc, tb, file=fh)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = hook

def main():
    install_crash_log()
    QApplication.setStyle("Fusion")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("ezexp")
    app.setWindowIcon(QIcon(app_logo_pixmap()))
    app.setStyleSheet(QSS)

    if acquire_single_instance() is None:
        QMessageBox.information(None, APP_NAME,
                                "EzExp is already running.")
        return
    worker = SearchWorker()
    worker.start()

    win = MainWindow(worker)
    win.resize(1180, 720)
    scr = app.primaryScreen().availableGeometry()
    win.move(scr.x() + (scr.width() - 1180) // 2, scr.y() + (scr.height() - 720) // 2)
    win.show()

    spot = Spotlight(worker)
    spot.main = win

    def toggle_spotlight():
        if spot.isVisible():
            spot.hide()
        else:
            spot.open_spotlight()

    filt = HotkeyFilter(toggle_spotlight)
    app.installNativeEventFilter(filt)
    hotkey_name = register_hotkey()
    if not hotkey_name:
        win.status_right.setText("Quick search hotkey unavailable (Alt+Space busy)")
    QShortcut(QKeySequence("Ctrl+Space"), win, toggle_spotlight)
    QShortcut(QKeySequence("Alt+Space"), win, toggle_spotlight)

    if "--agent" in sys.argv:
        spot.open_spotlight()
        spot.set_mode("agent")
    elif "--spotlight" in sys.argv:
        spot.open_spotlight()

    # quietly refresh the index every 10 minutes
    auto = QTimer()
    auto.setInterval(10 * 60 * 1000)
    auto.timeout.connect(lambda: win.start_indexer(False))
    auto.start()

    rc = app.exec()

    worker.stop()
    if win._indexer and win._indexer.isRunning():
        win._indexer.stop()
        win._indexer.wait(3000)
    unregister_hotkey()
    sys.exit(rc)


if __name__ == "__main__":
    main()
