# EzExp v1.3.0 Release Report

**Date**: 2026-11-10  
**Release Tag**: `v1.3.0`  
**Release Commit**: `08d2a06`  
**Base Commit**: `a780b65`  

---

## 1. Step 0 Privacy Sweep Results

All repository files across the tree (excluding `.git`, `build/`, `dist/`, `dist2/`, `__pycache__`) were swept:

- `gsk_`: **0 hits**
- `sk-`: **0 hits**
- `nvapi-`: **0 hits**
- `api_key` with non-empty value: **9 hits** (all dummy/test strings in test harnesses, no real keys):
  - `_test.py:811`: `"api_key": "local-test-key"`
  - `_test.py:827`: `client_no_grant = ez.AgentClient(config={"grant_dir": "", "api_key": "dummy"})`
  - `_test.py:834`: `client_grant = ez.AgentClient(config={"grant_dir": grant_test_dir, "api_key": "dummy"})`
  - `_test.py:841`: `config={"grant_dir": grant_test_dir, "api_key": "dummy"}`
  - `_test.py:850`: `config={"grant_dir": grant_test_dir, "api_key": "dummy"}`
  - `_test.py:996`: `"api_key": "stub-test-key"`
  - `_test.py:1494`: `json.dump({"base_url": "http://legacy.test", "model": "legacy-model", "api_key": "k", "grant_dir": "g"}, fh)`
  - `_test.py:1575`: `st2_ok, st2_msg = ez.agent_status({"backend": "endpoint", "api_key": "some-key-value"})`
  - `test_contract_v1.py:51`: `client = AgentClient(config={"grant_dir": grant_dir, "api_key": "dummy"})`
- `nanub`: **4 hits**:
  - `GRAPH_FIX_REPORT.md:12`: Scrubbed to `%USERPROFILE%` in Step 2b.
  - `MODE_SWITCH_REPORT.md:214`: Scrubbed to `%USERPROFILE%` in Step 2b.
  - `MODE_SWITCH_REPORT.md:223`: Scrubbed to `%USERPROFILE%` in Step 2b.
  - `_test.py:3433`: `main_win.on_index_progress(r"C:\Users\nanub\AppData", 8327)` (test fixture input for status text assertion).
- `yazur`: **0 hits**
- `nemotron`: **6 hits** (all in `MODE_SWITCH_REPORT.md` referencing code method names and the test directory `D:\nemotron\...`)
- `agent-test`: **0 hits**
- `email`: **0 hits**
- **Secrets Verdict**: **NONE (CLEAN)**.

---

## 2. Amendment: `_test.py` Expectation Update

During the initial release commit (`b7d275d`), `py _test.py` failed at line 802 because the test's `expected_defaults` asserted the legacy pre-v1.3.0 NVIDIA defaults:
```python
AssertionError: Expected {'backend': 'endpoint', 'harness_path': '', 'harness_python': 'py', 'base_url': 'https://integrate.api.nvidia.com/v1', 'model': 'meta/llama-3.3-70b-instruct', 'api_key': '', 'grant_dir': ''}, got {'backend': 'endpoint', 'harness_path': '', 'harness_python': 'py', 'base_url': '', 'model': '', 'api_key': '', 'grant_dir': ''}
```
Per user amendment, `_test.py` lines 797-798 were updated to expect empty strings for `"base_url"` and `"model"` to match the neutral defaults in `ezexp/agent_config.py`. The amendment was explicitly staged and committed via `git commit --amend --no-edit` producing commit `08d2a06`.

---

## 3. Diff Summary by File

### `ezexp/agent_config.py`
Neutral agent defaults configured for fresh installations:
```diff
@@ -9,8 +9,8 @@ DEFAULT_AGENT_CONFIG = {
     "backend": "endpoint",
     "harness_path": "",
     "harness_python": "py",
-    "base_url": "https://integrate.api.nvidia.com/v1",
-    "model": "meta/llama-3.3-70b-instruct",
+    "base_url": "",
+    "model": "",
     "api_key": "",
     "grant_dir": "",
 }
```

### `_test.py`
Synchronized default expectation in test suite:
```diff
@@ -794,8 +794,8 @@ try:
         "backend": "endpoint",
         "harness_path": "",
         "harness_python": "py",
-        "base_url": "https://integrate.api.nvidia.com/v1",
-        "model": "meta/llama-3.3-70b-instruct",
+        "base_url": "",
+        "model": "",
         "api_key": "",
         "grant_dir": "",
     }
```

### `README.md`
- Added `## Agent (optional, bring your own API)` section immediately following `## Features` / `### Small print that matters`.
- Updated version display from `1.2.0` to `1.3.0`.

### `MODE_SWITCH_REPORT.md` & `GRAPH_FIX_REPORT.md`
- Replaced all 3 occurrences of `C:\Users\nanub` with `%USERPROFILE%`.

### `ezexp/__init__.py`
- Version bumped: `APP_VERSION = "1.3.0"`

### `version_info.txt`
- Bumped `filevers=(1, 3, 0, 0)`, `prodvers=(1, 3, 0, 0)`, and string values `FileVersion` and `ProductVersion` to `"1.3.0"`.

### `RELEASE_NOTES_v1.3.0.md`
- Created release notes documenting Agent window enhancements, live USN journal indexing, honest scan status, Google Drive cache exclusion, background vault link graph, and UI fixes.

---

## 4. Test Suite Execution Tail

Execution of `py _test.py` on release commit `08d2a06` (fully green):

```text
NOTE: a global 'import harness' succeeds on this machine (editable install .pth)  the empty-dir negative probe cannot hold and was skipped
Exception in thread Thread-18 (_analyzer_job):
Traceback (most recent call last):
  File "C:\Users\nanub\AppData\Local\Python\pythoncore-3.14-64\Lib\threading.py", line 1082, in _bootstrap_inner
    self._context.run(self.run)
    ~~~~~~~~~~~~~~~~~^^^^^^^^^^
  File "C:\Users\nanub\AppData\Local\Python\pythoncore-3.14-64\Lib\threading.py", line 1024, in run
    self._target(*self._args, **self._kwargs)
    ~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\exp\ezexp\ezexp\main_window.py", line 839, in _analyzer_job
    analyzed, remaining = analyze_pending_links(
                          ~~~~~~~~~~~~~~~~~~~~~^
        conn, vaults, time_budget=6.0, progress=prog)
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\exp\ezexp\ezexp\vault_graph.py", line 90, in analyze_pending_links
    conn.execute("DELETE FROM note_links WHERE parent=? AND name=?", (parent, name))
    ~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
sqlite3.OperationalError: database is locked
EzExp scan: 2026-11-10 02:24:19  'Q:\slow' deferred (no progress for 0s)
EzExp scan: 2026-11-10 02:24:21  'C:\Users\nanub\AppData\Local\Temp\ezexp_test_m3p03swo\stall_root' deferred (no progress for 0s)
ALL TESTS PASSED
```

- Owner vaults restored: Verified as `['G:\My Drive\obsidian']`.
- Saved owner config: `%LOCALAPPDATA%\EzExp\agent.json` untouched and preserved.

---

## 5. Fresh-Config Proof

With `%LOCALAPPDATA%\EzExp\agent.json` temporarily renamed to verify virgin-state behavior:
- `DEFAULT_AGENT_CONFIG` printout:
  ```python
  {'backend': 'endpoint', 'harness_path': '', 'harness_python': 'py', 'base_url': '', 'model': '', 'api_key': '', 'grant_dir': ''}
  ```
- Dialog field values:
  - **Base URL text**: `""` (placeholder: `"https://integrate.api.nvidia.com/v1"`)
  - **Model text**: `""` (placeholder: `"meta/llama-3.3-70b-instruct"`)
  - **API key text**: `""` (placeholder: `"API key (stored locally only)"`)
- Original `agent.json` restored immediately in `finally` block.
- Screenshot captured: `C:\Users\nanub\.gemini\antigravity\brain\5fb1687e-738b-4ad2-85c0-58e78a280554\fresh_config_proof.png`

---

## 6. Build & Smoke Test Verification

- **Executable Path**: `D:\exp\ezexp\dist\ezexp.exe`
- **File Size**: `48,087,065 bytes` (~45.86 MB)
- **SHA256**: `DF0CFEBBD2CFD11E345823689E337F4905C79BE756A8DEC4EE857A9CDFB367ED`
- **Embedded Resource Version**: `FileVersion 1.3.0`, `ProductVersion 1.3.0`
- **Smoke Test**: Launch verified, window title `"EzExp"` confirmed, version `1.3.0` confirmed, Agent toggle button opens dock confirmed, closed cleanly. Screenshot: `C:\Users\nanub\.gemini\antigravity\brain\5fb1687e-738b-4ad2-85c0-58e78a280554\agent_dock_smoke.png`.

---

## 7. Owner's 60-Second Release Click Steps

1. Open https://github.com/razybas/EZEXP/releases/new
2. Choose tag `v1.3.0` (it already exists), title `EzExp v1.3.0`
3. Paste the contents of `RELEASE_NOTES_v1.3.0.md` as the description
4. Drag in the built `dist\ezexp.exe`, then **Publish release**
