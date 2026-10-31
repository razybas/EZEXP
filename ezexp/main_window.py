import os
import sys
import json
import time
import shutil
import sqlite3
import threading
import urllib.parse

from PySide6.QtCore import (
    Qt, QSize, QTimer, QSettings, QUrl, QEvent, Signal, QMimeData
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QImage, QKeySequence, QPixmap, QShortcut,
    QDesktopServices, QGuiApplication
)
import datetime
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QVBoxLayout, QGridLayout, QLabel,
    QLineEdit, QPushButton, QMenu, QMessageBox, QFileDialog, QWidget,
    QInputDialog, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QSplitter, QPlainTextEdit, QDialog
)
from ezexp.agent import AgentClient, BridgeWorker, ConfirmBridge, AgentSettingsDialog
from ezexp.agent_config import load_agent_config, agent_status

from ezexp import APP_NAME
from ezexp.fsutil import (
    fixed_drives, drive_label, shell_folders, join_path, split_path, ext_of,
    human_size, fmt_date, real_dir, category_for, kind_for, TEXT_EXTS, like_escape
)
from ezexp.shell_win import (
    reveal_in_explorer, recycle_path, show_properties, shell_thumbnail
)
from ezexp.db import (
    db_connect, thread_conn, get_meta, set_meta, update_total,
    insert_rows, delete_tree, index_add_path, ROW_FIELDS,
    _row_dict, _unique_dest, fts_rename
)
from ezexp.indexer import _make_row
from ezexp.search_engine import sort_rows, do_search
from ezexp.indexer import SearchWorker, IndexerThread
from ezexp.settings_store import (
    get_vaults, set_vaults, get_quicklaunch, set_quicklaunch,
    get_launch_counts, bump_launch_count, remove_launch_count, APP_EXTS
)
from ezexp.vault_graph import (
    active_vault_for, build_vault_graph, build_structure_graph,
    pending_link_count, analyze_pending_links
)
from ezexp.widgets import (
    TitleBar, SearchField, ResultsTree, NameDelegate, make_row_item,
    FramelessWindow, place_icon, category_icon, app_icon, spot_row_icon,
    real_file_icon, app_icon_for, resolve_lnk_target, app_logo_pixmap,
    rounded_pixmap, with_play_badge
)
from ezexp.graph_view import GraphView, QLaunchButton
from ezexp.agent import AgentSettingsDialog

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
        self._current_place = None
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

        self.btn_toggle_agent = QPushButton("Agent", self.titlebar)
        self.btn_toggle_agent.setObjectName("btnToggleAgent")
        self.btn_toggle_agent.setCheckable(True)
        self.btn_toggle_agent.setCursor(Qt.PointingHandCursor)
        self.btn_toggle_agent.setFixedHeight(28)
        self.btn_toggle_agent.setStyleSheet(
            "QPushButton#btnToggleAgent { background: #e8e8ed; border: none; border-radius: 6px; padding: 0 10px; font-size: 12px; font-weight: 500; color: #1d1d1f; }"
            "QPushButton#btnToggleAgent:checked { background: #0a84ff; color: #ffffff; }"
            "QPushButton#btnToggleAgent:hover:!checked { background: #dedee3; }"
        )
        self.btn_toggle_agent.clicked.connect(self.toggle_agent_dock)
        self.titlebar.layout().addWidget(self.btn_toggle_agent)

        self.agent_client = AgentClient(self)
        self.agent_client.agent_text.connect(self._on_agent_text)
        self.agent_client.agent_error.connect(self._on_agent_error)
        self.confirm_bridge = ConfirmBridge(self)
        self.agent_messages = []
        self._bridge_worker = None
        self._harness_banner_shown = False

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

        # ---- agent dock (extreme right) ----
        self.agent_dock = QFrame(self)
        self.agent_dock.setObjectName("agentDock")
        self.agent_dock.setFixedWidth(340)
        self.agent_dock.setStyleSheet("#agentDock { background: #fafafc; border-left: 1px solid #ececf0; }")
        dock_lay = QVBoxLayout(self.agent_dock)
        dock_lay.setContentsMargins(8, 8, 8, 8)
        dock_lay.setSpacing(6)

        dock_header = QHBoxLayout()
        self.agent_status_lbl = QLabel(self.agent_dock)
        self.agent_status_lbl.setObjectName("agentStatusLbl")
        self.agent_status_lbl.setStyleSheet("font-size: 11px; color: #6e6e73;")
        dock_header.addWidget(self.agent_status_lbl, 1)

        self.btn_endpoint = QPushButton("Endpoint…", self.agent_dock)
        self.btn_endpoint.setObjectName("btnEndpoint")
        self.btn_endpoint.setCursor(Qt.PointingHandCursor)
        self.btn_endpoint.setStyleSheet(
            "QPushButton { background: #f1f1f4; border: none; border-radius: 6px; padding: 3px 8px; font-size: 11px; color: #6e6e73; }"
            "QPushButton:hover { background: #e8e8ed; color: #1d1d1f; }"
        )
        self.btn_endpoint.clicked.connect(self._open_endpoint)
        dock_header.addWidget(self.btn_endpoint)
        dock_lay.addLayout(dock_header)

        self.agent_splitter = QSplitter(Qt.Orientation.Vertical, self.agent_dock)
        self.agent_splitter.setObjectName("agentSplitter")

        chat_container = QWidget(self.agent_splitter)
        chat_lay = QVBoxLayout(chat_container)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_lay.setSpacing(6)

        self.dock_transcript = QPlainTextEdit(chat_container)
        self.dock_transcript.setObjectName("dockTranscript")
        self.dock_transcript.setReadOnly(True)
        self.dock_transcript.setStyleSheet(
            "background: #ffffff; color: #1d1d1f; border: 1px solid #e6e6ea; border-radius: 8px; padding: 8px 10px; font-size: 13px;"
        )
        chat_lay.addWidget(self.dock_transcript, 1)

        input_row = QHBoxLayout()
        self.dock_input = QLineEdit(chat_container)
        self.dock_input.setObjectName("dockInput")
        self.dock_input.setPlaceholderText("Ask the agent…")
        self.dock_input.setStyleSheet(
            "background: #ffffff; color: #1d1d1f; border: 1px solid #e6e6ea; border-radius: 8px; padding: 6px 10px; font-size: 13px;"
        )
        self.dock_input.returnPressed.connect(self.send_agent_message)
        input_row.addWidget(self.dock_input, 1)

        self.dock_btn_send = QPushButton("Send", chat_container)
        self.dock_btn_send.setObjectName("dockBtnSend")
        self.dock_btn_send.setStyleSheet(
            "QPushButton { background: #0a84ff; color: #ffffff; border: none; border-radius: 8px; padding: 6px 12px; font-weight: 500; font-size: 13px; }"
            "QPushButton:hover { background: #0071e3; }"
            "QPushButton:disabled { background: #b7d7ff; }"
        )
        self.dock_btn_send.clicked.connect(self.send_agent_message)
        input_row.addWidget(self.dock_btn_send)
        chat_lay.addLayout(input_row)

        self.agent_splitter.addWidget(chat_container)

        self.workspace_pane = QFrame(self.agent_splitter)
        self.workspace_pane.setObjectName("workspacePane")
        ws_lay = QVBoxLayout(self.workspace_pane)
        ws_lay.setContentsMargins(0, 6, 0, 0)
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
        ws_lay.addLayout(ws_header_row)

        self.ws_lbl_context = QLabel("Context: (no folder open)", self.workspace_pane)
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
        self.agent_splitter.setSizes([260, 260])

        dock_lay.addWidget(self.agent_splitter, 1)
        body.addWidget(self.agent_dock)

        # Convenient aliases on self.agent_dock
        self.agent_dock.ws_lbl_context = self.ws_lbl_context
        self.agent_dock.ws_lbl_grant = self.ws_lbl_grant
        self.agent_dock.ws_file_list = self.ws_file_list
        self.agent_dock.ws_preview = self.ws_preview
        self.agent_dock.ws_btn_refresh = self.ws_btn_refresh
        self.agent_dock.dock_transcript = self.dock_transcript
        self.agent_dock.transcript = self.dock_transcript
        self.agent_dock.dock_input = self.dock_input
        self.agent_dock.input = self.dock_input
        self.agent_dock.dock_btn_send = self.dock_btn_send

        # Restore visibility from settings (default hidden on fresh install)
        saved_vis = QSettings("ezexp", "EzExp").value("agent_dock_visible", "false")
        init_vis = (saved_vis is True or str(saved_vis).lower() in ("true", "1"))
        self.agent_dock.setVisible(init_vis)
        self.btn_toggle_agent.setChecked(init_vis)
        if init_vis:
            self.refresh_workspace_pane()
            self.update_agent_status()

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
            self.update_dock_context()
        else:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
            self.update_dock_context()

    def on_selection(self):
        if self._ql_selected:
            return
        item = self.tree.currentItem()
        self.details.show_row(item.data(0, Qt.UserRole) if item else None)
        self.update_dock_context()

    def on_tree_clicked(self, item, col=0):
        self._ql_selected = None
        for b in self._ql_buttons:
            b.setChecked(False)
        self.details.show_row(item.data(0, Qt.UserRole) if item else None)
        self.update_dock_context()

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
                fts_rename(conn, idr[0], row["name"], new)
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
        # WAIT for the analyzer thread to actually exit (its current pass
        # can take a few more seconds and it writes note_links): a scan
        # starting under it dies on 'database is locked' and the indexer
        # retries the WHOLE rebuild from zero
        t0 = time.time()
        while self._analyzer_running and time.time() - t0 < 15:
            time.sleep(0.2)
        self._indexer = IndexerThread(rebuild=rebuild)
        self._indexer.progress.connect(self.on_index_progress)
        self._indexer.done.connect(self.on_index_done)
        self._indexer.usn_status.connect(self.on_usn_status)
        self._indexer.root_deferred.connect(self.on_root_deferred)
        self._deferred_notes = []
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

    def on_root_deferred(self, text):
        """Fix 2: the quarantine flagged a virtual root (e.g. G: made no
        progress for 120 s). The spinner keeps overwriting status_right
        while the scan runs, so keep the note for the final line."""
        notes = getattr(self, "_deferred_notes", None)
        if notes is None:
            notes = self._deferred_notes = []
        if text not in notes:
            notes.append(text)

    def _append_defer_notes(self, line):
        for note in getattr(self, "_deferred_notes", []):
            line += f" · {note}"
        return line

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
            line = f"{total:,} items indexed · updated {stamp}"
            if getattr(self, "_usn_live", ""):
                line += f" · {self._usn_live}"
            line = self._append_defer_notes(line)
            self.status_right.setText(line)
            set_meta(thread_conn(), "total", total)
        else:
            self.status_right.setText("Indexing was interrupted")
        self.refresh_view()
        if self.view_mode == "graph":
            self.refresh_graph()
        self.start_link_analyzer()
        self._consume_pending_index()

    def on_usn_status(self, text):
        """Per-drive live mode from the USN watcher, shown in the
        existing status label when no scan is running."""
        self._usn_live = text
        if not (self._indexer and self._indexer.isRunning()):
            self.status_right.setText(text)

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

    @property
    def current_place(self):
        return self._current_place

    @current_place.setter
    def current_place(self, val):
        self._current_place = val
        self.update_dock_context()

    def update_dock_context(self):
        if hasattr(self, "ws_lbl_context"):
            cur = self.get_current_folder()
            self.ws_lbl_context.setText(f"Context: {cur}" if cur else "Context: (no folder open)")

    def toggle_agent_dock(self, checked=None):
        if checked is None:
            new_vis = self.agent_dock.isHidden()
        else:
            new_vis = bool(checked)
        self.agent_dock.setVisible(new_vis)
        self.btn_toggle_agent.setChecked(new_vis)
        QSettings("ezexp", "EzExp").setValue("agent_dock_visible", "true" if new_vis else "false")
        if new_vis:
            self.refresh_workspace_pane()
            self.update_agent_status()

    def update_agent_status(self):
        cfg = self.agent_client.config if self.agent_client.config is not None else load_agent_config()
        ready, status_msg = agent_status(cfg)
        self.agent_status_lbl.setText(status_msg or "")

    def _open_endpoint(self):
        dlg = AgentSettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.agent_client.config = load_agent_config()
            self.update_agent_status()
            self.refresh_workspace_pane()

    def get_current_folder(self):
        if hasattr(self, "_current_folder_override") and self._current_folder_override is not None:
            return os.path.abspath(self._current_folder_override) if self._current_folder_override else None
        if getattr(self, "_current_place", None):
            return os.path.abspath(self._current_place)
        row = self.current_row()
        if not row and hasattr(self, "details"):
            row = getattr(self.details, "_row", None)
        if row and row.get("is_dir"):
            p = self.row_path(row)
            if p:
                return os.path.abspath(p)
        cf = getattr(self, "current_folder", None)
        return os.path.abspath(cf) if cf else None

    @property
    def _harness_worker(self):
        return self._bridge_worker

    @_harness_worker.setter
    def _harness_worker(self, val):
        self._bridge_worker = val

    def send_agent_message(self):
        if self._harness_worker is not None and self._harness_worker.isRunning():
            return
        text = self.dock_input.text().strip()
        if not text:
            return
        cfg = self.agent_client.config if self.agent_client.config is not None else load_agent_config()
        ready, status_msg = agent_status(cfg)
        if not ready:
            if status_msg:
                self.dock_transcript.appendPlainText(status_msg + "\n")
            return

        backend = (cfg.get("backend") or "endpoint").lower()
        if backend == "harness":
            self.dock_input.clear()
            if not self._harness_banner_shown:
                self.dock_transcript.appendPlainText(
                    "Tools: EzExp catalog + grant tools via the harness (native harness tools disabled).\n"
                )
                self._harness_banner_shown = True
            self.dock_transcript.appendPlainText(f"You: {text}\n")
            self.agent_messages.append({"role": "user", "content": text})
            grant_dir = (cfg.get("grant_dir") or "").strip()
            hp = (cfg.get("harness_path") or "").strip()
            hpython = (cfg.get("harness_python") or "py").strip() or "py"
            cur_folder = self.get_current_folder()
            self._start_harness_task(text, grant_dir, hp, hpython, mode="single", current_folder=cur_folder)
        else:
            self.dock_input.clear()
            self.dock_transcript.appendPlainText(f"You: {text}\n")
            self.agent_messages.append({"role": "user", "content": text})
            self.agent_client.send(self.agent_messages)

    def _start_harness_task(self, text, grant_dir, harness_path="", harness_python="py", mode="single", current_folder=None):
        self.dock_input.setEnabled(False)
        self.dock_btn_send.setEnabled(False)
        if current_folder is None:
            current_folder = self.get_current_folder()
        self._bridge_worker = BridgeWorker(
            text, grant_dir, harness_path, harness_python, client=self.agent_client, bridge=self.confirm_bridge, parent=self, mode=mode, current_folder=current_folder
        )
        self._bridge_worker.done.connect(self._on_harness_done)
        self._bridge_worker.failed.connect(self._on_harness_failed)
        self._bridge_worker.start()

    def _on_harness_done(self, final_text):
        self.dock_input.setEnabled(True)
        self.dock_btn_send.setEnabled(True)
        self.dock_input.setFocus()
        if final_text:
            self.dock_transcript.appendPlainText(final_text + "\n")
            self.agent_messages.append({"role": "assistant", "content": final_text})
        self.refresh_workspace_pane()

    def _on_harness_failed(self, err_text):
        self.dock_input.setEnabled(True)
        self.dock_btn_send.setEnabled(True)
        self.dock_input.setFocus()
        self.dock_transcript.appendPlainText(f"Error: {err_text}\n")
        self.refresh_workspace_pane()

    def _on_agent_text(self, text):
        if text:
            self.dock_transcript.appendPlainText(text)
            self.agent_messages.append({"role": "assistant", "content": text})
            self.refresh_workspace_pane()

    def _on_agent_error(self, err):
        if err:
            self.dock_transcript.appendPlainText(f"Error: {err}\n")

    def refresh_workspace_pane(self):
        """Update context/grant labels and refresh file list from grant_dir."""
        self.update_dock_context()

        cfg = self.agent_client.config if self.agent_client.config is not None else load_agent_config()
        grant_dir = (cfg.get("grant_dir") or "").strip() or getattr(self, "grant_dir", None) or ""
        self.ws_lbl_grant.setText(f"Grant: {grant_dir}" if grant_dir else "Grant: (none)")

        self.ws_file_list.setRowCount(0)
        self.ws_preview.clear()

        if not grant_dir or not os.path.isdir(grant_dir):
            return

        try:
            entries = list(os.scandir(grant_dir))
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
