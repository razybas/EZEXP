import os
import sys
import json
import time
import datetime

import urllib.parse

from PySide6.QtCore import Qt, QSize, QRect, QTimer, Signal, QUrl
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QPixmap, QKeySequence, QShortcut,
    QGuiApplication, QDesktopServices
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QStyledItemDelegate,
    QPlainTextEdit, QSplitter, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QDialog, QStyle
)

from ezexp import APP_NAME
from ezexp.fsutil import (
    join_path, real_dir, split_path, human_size, ext_of,
    category_for, kind_for, ACCENT, TEXT, SUBTLE
)
from ezexp.shell_win import reveal_in_explorer
from ezexp.settings_store import (
    get_quicklaunch, bump_launch_count, get_launch_counts
)
from ezexp.agent_config import (
    load_agent_config, save_agent_config, clear_harness_probe_cache,
    harness_available, agent_status, _harness_probe_cache
)
from ezexp.agent import (
    AgentClient, BridgeWorker, ProbeWorker, ConfirmBridge,
    AgentSettingsDialog
)
from ezexp.widgets import (
    FramelessWindow, search_glyph, spot_row_icon, real_file_icon,
    app_logo_pixmap
)
from ezexp.indexer import SearchWorker
from ezexp.db import db_connect, thread_conn, ROW_FIELDS, _row_dict

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
