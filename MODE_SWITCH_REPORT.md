# Mode Switch & Organism Local-State Report

## Phase 0 — organism_ai Local-State Check (Report Only)

### 0.1 Git State & Local HEAD vs Reference Verdict
- **Target Repo**: `D:\nemotron\phage internship\organism_ai`
- **Reference GitHub HEAD**: `84b7a2ec3a9615d110f703849f49674095e28479` (H3, 27 Oct)

**Verbatim command outputs**:
- `git -C "D:\nemotron\phage internship\organism_ai" log --oneline -5`
  ```
  70111b6 fix(chat): clean host chat extraction, no double pathway badge
  4776243 feat(cli): full interactive CLI in-repo, fixed entry points, packaging, README
  ac1bffd fix(harness): agent budget starvation, snapshot pollution, decomposition crash
  84b7a2e Phase H3: host runner uses real LLM; chat defaults to single mode.
  969a687 Phase H2: clean answer extraction for host chat surfaces
  ```
- `git -C "D:\nemotron\phage internship\organism_ai" status --porcelain`
  ```
  (clean - no output)
  ```
- `git -C "D:\nemotron\phage internship\organism_ai" rev-parse HEAD`
  ```
  70111b6ea9e973d41096531a8e2003985feae951
  ```

**Verdict**:
Local HEAD (`70111b6ea9e973d41096531a8e2003985feae951`) is **ahead** of origin/main (`84b7a2e`) by 3 unpushed commits:
- `ac1bffd` fix(harness): agent budget starvation, snapshot pollution, decomposition crash
- `4776243` feat(cli): full interactive CLI in-repo, fixed entry points, packaging, README
- `70111b6` fix(chat): clean host chat extraction, no double pathway badge

Working directory is clean (not dirty).

---

### 0.2 Mode-Dispatch & Accepted Mode Values in `host_runner.py`

**Verbatim quoted lines from `harness/host_runner.py`**:
- Reading `mode` from task payload:
  Line 224:
  ```python
  mode = task_data.get("mode", "single")
  ```
- Dispatching `mode` to harness execution:
  Line 251:
  ```python
  resp = h.run(task_text, mode=mode, **llm_kwargs)
  ```

**Verbatim quoted forwarding from `harness/__init__.py`**:
- Line 108:
  ```python
  return self.orchestrator.run(
      task=task,
      mode=mode,
      steps=steps,
      subtasks=subtasks,
      tools=self.tools,
      llm_client=llm_client,
      **kwargs,
  )
  ```

**Accepted mode values and what each constructs**:
The runner accepts 3 modes: `single`, `pipeline`, and `fanout`. (If any other mode is passed, `orchestrator.py` raises `ValueError(f"Unknown execution mode '{mode}'. Choose 'single', 'pipeline', or 'fanout'.")`).

1. `single`:
   - Single-agent loop.
   - Evaluates task against admission gate. Looks up proven patterns or matches single capability (`_match_capability_for_text(task)`). Allocates 8000 tokens / 60.0s budget slice to a single `Agent(...)`, runs `agent.run(task, blackboard=blackboard)`. Records proven pattern in muscle memory upon completion.
2. `pipeline`:
   - Full sequential multi-agent pipeline.
   - Decomposes task into ordered steps (explicit `steps`, newline splits, ` then ` splits, or default `Investigate: {task}` followed by `Synthesize: {task}`).
   - Iterates sequentially through steps, instantiating `PipelineAgent_{i+1}_{cap.locus_id}` agents communicating through the shared `Blackboard`. Chains output from step $i$ as context into step $i+1$ (`Previous step result:\n{prev_output}\n\nCurrent Step: {step_text}`).
3. `fanout`:
   - Independent parallel multi-agent subtasks.
   - Decomposes task into sub-tasks (explicit `subtasks`, newline splits, or `Perspective A` / `Perspective B`).
   - Runs independent `FanoutWorker_{i+1}_{cap.locus_id}` agents across subtasks, then merges all agent results with surfaced disagreements via `self.golgi.merge_fanout_results(agent_results)`.

---

### 0.3 H4 Fallback Presence in `harness/runtime/agent_pool.py` (Headline Fact)
- `_generate_biomimetic_nemotron_analysis`:
  - Defined at `harness/runtime/agent_pool.py:1966`
  - Called at `harness/runtime/agent_pool.py:1646`
- `_generate_first_principles_synthesis`:
  - Defined at `harness/runtime/agent_pool.py:2005`
  - Called at `harness/runtime/agent_pool.py:1694`
- **Call context in `CellularEnsemble`**:
  YES, `CellularEnsemble`'s provider-failure fallback paths call them directly. In `_invoke_agent` (lines 1643-1706), when external providers (Groq/OpenRouter) fail or encounter exceptions / quota limits:
  - If the agent ID/provider contains `nemotron` or role is `SPECIALIST`, it invokes `self._generate_biomimetic_nemotron_analysis(prompt)` (line 1646).
  - If the fallback cascades past Groq and OpenRouter swarm resilience, it invokes `self._generate_first_principles_synthesis(prompt)` (line 1694).

---

## Phase 1 — EzExp: Mode from Settings

### 1.0 Gate Verification
- Initial HEAD: `098f34b940660fdf519a7d50ff109a6bf54103c8`
- Initial status: ` M ezexp.spec`, `?? _dbg_vaults.py`, `?? _probe_gdrive_stall.py`
- Anchors verified:
  - `ezexp/main_window.py`: **YES** (verbatim match at line 1532)
    `self._start_harness_task(text, grant_dir, hp, hpython, mode="single", current_folder=cur_folder)`
  - `ezexp/spotlight.py`: **YES** (verbatim match at line 273)
    `self._start_harness_task(text, grant_dir, hp, hpython, mode="single", current_folder=cur_folder)`

---

### 1.1 Exact Diffs Per File (Closed Edit Set)

#### `ezexp/agent.py`
```diff
diff --git a/ezexp/agent.py b/ezexp/agent.py
index 5307091..f06c96c 100644
--- a/ezexp/agent.py
+++ b/ezexp/agent.py
@@ -85,6 +85,28 @@ class AgentSettingsDialog(QDialog):
         self.edit_python.setPlaceholderText("py")
         lay.addWidget(self.edit_python)
 
+        self.lbl_mode = QLabel("Harness mode:", self)
+        lay.addWidget(self.lbl_mode)
+        self.combo_mode = QComboBox(self)
+        self.combo_mode.setStyleSheet(
+            light_field_style(padding="4px 8px")
+            + "QComboBox QAbstractItemView { background: #ffffff; color: %s; "
+              "selection-background-color: #0a84ff; selection-color: #ffffff; }" % TEXT
+        )
+        self.combo_mode.addItem("single")
+        self.combo_mode.addItem("pipeline")
+        self.combo_mode.addItem("fanout")
+        curr_mode = (self.cfg.get("harness_mode") or "single").strip().lower()
+        idx = self.combo_mode.findText(curr_mode)
+        if idx >= 0:
+            self.combo_mode.setCurrentIndex(idx)
+        else:
+            self.combo_mode.addItem(curr_mode)
+            self.combo_mode.setCurrentIndex(self.combo_mode.count() - 1)
+        self.lbl_harness_mode = self.lbl_mode
+        self.combo_harness_mode = self.combo_mode
+        lay.addWidget(self.combo_mode)
+
         lbl_url = QLabel("Base URL:", self)
         self.edit_url = QLineEdit(self)
         self.edit_url.setText(self.cfg.get("base_url", ""))
@@ -148,11 +170,15 @@ class AgentSettingsDialog(QDialog):
         self.btn_choose_harness.setEnabled(is_harness)
         self.lbl_python.setEnabled(is_harness)
         self.edit_python.setEnabled(is_harness)
+        self.lbl_mode.setEnabled(is_harness)
+        self.combo_mode.setEnabled(is_harness)
         self.lbl_harness.setVisible(is_harness)
         self.edit_harness.setVisible(is_harness)
         self.btn_choose_harness.setVisible(is_harness)
         self.lbl_python.setVisible(is_harness)
         self.edit_python.setVisible(is_harness)
+        self.lbl_mode.setVisible(is_harness)
+        self.combo_mode.setVisible(is_harness)
 
     def _choose_harness(self):
         curr = self.edit_harness.text().strip() or os.path.expanduser("~")
@@ -171,6 +197,7 @@ class AgentSettingsDialog(QDialog):
             "backend": "harness" if self.combo_backend.currentIndex() == 1 else "endpoint",
             "harness_path": self.edit_harness.text().strip(),
             "harness_python": self.edit_python.text().strip() or "py",
+            "harness_mode": self.combo_mode.currentText().strip() or "single",
             "base_url": self.edit_url.text().strip(),
             "model": self.edit_model.text().strip(),
             "api_key": self.edit_key.text().strip(),
```

#### `ezexp/main_window.py`
```diff
diff --git a/ezexp/main_window.py b/ezexp/main_window.py
index c9e382c..29fb6f7 100644
--- a/ezexp/main_window.py
+++ b/ezexp/main_window.py
@@ -1528,8 +1528,9 @@ class MainWindow(FramelessWindow):
             grant_dir = (cfg.get("grant_dir") or "").strip()
             hp = (cfg.get("harness_path") or "").strip()
             hpython = (cfg.get("harness_python") or "py").strip() or "py"
+            hmode = (cfg.get("harness_mode") or "single").strip() or "single"
             cur_folder = self.get_current_folder()
-            self._start_harness_task(text, grant_dir, hp, hpython, mode="single", current_folder=cur_folder)
+            self._start_harness_task(text, grant_dir, hp, hpython, mode=hmode, current_folder=cur_folder)
         else:
             self.dock_input.clear()
             self.dock_transcript.appendPlainText(f"You: {text}\n")
```

#### `ezexp/spotlight.py`
```diff
diff --git a/ezexp/spotlight.py b/ezexp/spotlight.py
index 1ae1b11..3860ca5 100644
--- a/ezexp/spotlight.py
+++ b/ezexp/spotlight.py
@@ -269,8 +269,9 @@ class Spotlight(FramelessWindow):
             grant_dir = (cfg.get("grant_dir") or "").strip()
             hp = (cfg.get("harness_path") or "").strip()
             hpython = (cfg.get("harness_python") or "py").strip() or "py"
+            hmode = (cfg.get("harness_mode") or "single").strip() or "single"
             cur_folder = self.get_current_folder()
-            self._start_harness_task(text, grant_dir, hp, hpython, mode="single", current_folder=cur_folder)
+            self._start_harness_task(text, grant_dir, hp, hpython, mode=hmode, current_folder=cur_folder)
         else:
             self.edit.clear()
             self.transcript.appendPlainText(f"You: {text}\n")
```

---

### 1.2 Configured Mode String
- **Mode string persisted in `agent.json`**: `"pipeline"`
- Path: `C:\Users\nanub\AppData\Local\EzExp\agent.json`

---

### 1.3 Verification Test Suite (`py _test.py`)
**Suite tail (verbatim)**:
```
NOTE: a global 'import harness' succeeds on this machine (editable install .pth)  the empty-dir negative probe cannot hold and was skipped
EzExp scan: 2026-11-05 18:39:16  'Q:\slow' deferred (no progress for 0s)
EzExp scan: 2026-11-05 18:39:18  'C:\Users\nanub\AppData\Local\Temp\ezexp_test_drzj5sgm\stall_root' deferred (no progress for 0s)
ALL TESTS PASSED
```
Exit code: `0` (Fully Green).

---

### 1.4 Built Executable Metadata
- **Build route**: `py make_icon.py` + `py -m PyInstaller --noconfirm --onefile --windowed --name ezexp --icon ezexp.ico --version-file version_info.txt app.py`
- **Path**: `D:\exp\ezexp\dist\ezexp.exe`
- **Size (bytes)**: `48084107`
- **SHA256**: `0e30ae2fff6b62907415ead66137bca4f0b4892865933889bde857d28c25d811`
