import os
import sys
import re
import json
import queue
import time
import collections
import threading
import subprocess
import datetime
from collections import deque

from PySide6.QtCore import Qt, QThread, Signal, QByteArray, QObject, QUrl
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QButtonGroup, QMessageBox, QFrame, QFileDialog,
    QApplication, QComboBox, QWidget
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from ezexp.agent_config import (
    load_agent_config, save_agent_config, clear_harness_probe_cache,
    harness_available, agent_status
)
from ezexp.fsutil import ext_of, join_path, real_dir
from ezexp.db import db_connect, _row_dict, thread_conn
from ezexp.search_engine import do_search
from ezexp.indexer import IndexerThread
from ezexp.settings_store import get_quicklaunch

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
