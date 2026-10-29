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
import urllib.parse
from collections import deque
from ctypes import wintypes

from PySide6.QtCore import (
    Qt, QFileInfo, QTimer, QThread, QUrl, QSize, QPointF, QRect, QRectF, QLineF,
    QMimeData, QEvent, QSettings, Signal, QAbstractNativeEventFilter, QLockFile,
    QByteArray, QObject,
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
    QSplitter, QTableWidget, QTableWidgetItem, QAbstractItemView,
)
from PySide6.QtNetwork import (
    QNetworkAccessManager, QNetworkRequest, QNetworkReply,
)

if sys.platform != "win32":
    print("EzExp requires Windows.")
    sys.exit(1)

# Re-exports from ezexp package
from ezexp import APP_NAME, APP_VERSION, APP_DIR, DB_DIR, DB_PATH
from ezexp.settings_store import (
    get_vaults, set_vaults, APP_EXTS, get_quicklaunch, set_quicklaunch,
    get_launch_counts, bump_launch_count, remove_launch_count,
)
from ezexp.agent_config import (
    DEFAULT_AGENT_CONFIG, _harness_probe_cache, clear_harness_probe_cache,
    harness_available, agent_config_path, load_agent_config, save_agent_config,
    agent_status,
)
from ezexp.fsutil import (
    ACCENT, TEXT, SUBTLE, CAT_EXTS, KIND_NAMES, ext_of, TEXT_EXTS,
    category_for, kind_for, human_size, fmt_date, join_path, split_path,
    real_dir, like_escape, SKIP_DIR_NAMES, SKIP_FILE_NAMES, REPARSE_ATTR,
    fixed_drives, drive_label, shell_folders,
)
from ezexp.shell_win import (
    reveal_in_explorer, _SHFILEOPSTRUCTW, recycle_path, show_properties,
    _hbitmap_to_qimage, shell_thumbnail,
)
from ezexp.db import (
    _tls, db_connect, thread_conn, _ensure_schema, ensure_indexes,
    rebuild_fts, fts_touch, fts_drop_ids, get_meta, set_meta, update_total,
    insert_rows, delete_tree, ROW_FIELDS, ROW_FIELDS_F, _row_dict,
    _unique_dest, index_add_path,
)
from ezexp.search_engine import sort_rows, do_search
from ezexp.indexer import (
    BATCH, MTIME_EPS, _entry_skipped, _make_row, scan_full, scan_delta,
    SearchWorker, IndexerThread,
)
from ezexp.vault_graph import (
    WIKILINK_RE, parse_wikilinks, active_vault_for, pending_link_count,
    analyze_pending_links, build_vault_graph, build_structure_graph,
)
from ezexp.widgets import (
    CAT_COLORS, _icon_cache, _painter_on, category_icon, place_icon,
    search_glyph, app_icon, resolve_lnk_target, _icon_provider,
    real_file_icon, app_icon_for, _spot_icon_cache, spot_row_icon,
    app_logo_pixmap, rounded_pixmap, with_play_badge, RESIZE_MARGIN,
    FramelessWindow, TitleBar, SearchField, NameDelegate, ResultsTree,
    make_row_item, QSS,
)
from ezexp.graph_view import GraphNode, GraphView, QLaunchButton
from ezexp.agent import (
    AgentSettingsDialog, SYSTEM_PROMPT, AGENT_TOOLS, ConfirmBridge,
    AgentClient, BridgeWorker, HarnessWorker, ProbeWorker,
)
from ezexp.main_window import MainWindow, ClickableLabel, DetailsPane
from ezexp.spotlight import Spotlight, SpotDelegate
from ezexp.hotkey import (
    WM_HOTKEY, MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, HOTKEY_ID,
    HotkeyFilter, register_hotkey, unregister_hotkey,
)

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

import types as __types

class __AppModule(__types.ModuleType):
    def __getattribute__(self, name):
        if name == "FTS_OK":
            import ezexp.db
            return ezexp.db.FTS_OK
        return super().__getattribute__(name)

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        import sys
        for mod_name, mod in list(sys.modules.items()):
            if mod_name == "ezexp" or mod_name.startswith("ezexp."):
                if hasattr(mod, name):
                    try:
                        setattr(mod, name, value)
                    except Exception:
                        pass

    def __dir__(self):
        return sorted(list(self.__dict__.keys()) + ['FTS_OK'])

sys.modules[__name__].__class__ = __AppModule

if __name__ == "__main__":
    main()
