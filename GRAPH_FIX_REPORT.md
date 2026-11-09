# EzExp Fix Report: Launch Scan Cost + Graph Gating (F1, F2, F3)

**Repository**: `D:\exp\ezexp`  
**Base Commit**: `1d3d23a` (`feat: harness mode from settings (default single)`)  
**Date**: November 9, 2026  

---

## 1. Phase 0 Diagnostics (Owner Machine Verbatim)

### 0.1 Launched Executable Verification
- **Desktop Shortcut**: `%USERPROFILE%\Desktop\ezexp - Shortcut.lnk`
- **Shortcut Target**: `D:\exp\ezexp\dist\ezexp.exe`
- **File Size**: `48,084,107 bytes`
- **Comparison to Reference**: Matches the `1d3d23a` reference build (`48,084,107 bytes`) byte-for-byte. The owner is running the latest reference build.

### 0.2 Local Database Diagnostics (`%LOCALAPPDATA%\EzExp\index.db`)
Queried against an isolated temporary copy (`-wal` and `-shm` included):
- `phase`: `full_done`
- `full_done_roots`: `None` (NULL)
- Total indexed files (`SELECT COUNT(*) FROM files`): `1,883,124`
- Google DriveFS cache rows (`SELECT COUNT(*) FROM files WHERE parent LIKE 'C:\Users\%\AppData\Local\Google\DriveFS%'`): **`2,394` rows**
- *Finding*: Strong concrete evidence supporting F1's premise. The local Google DriveFS cache store was heavily indexed under `C:`, causing repeated long walk degradations during delta scans.

### 0.3 Tail of `%LOCALAPPDATA%\EzExp\scan.log`
Last 30 lines contained exactly 3 entries:
```text
2026-11-03 17:58:02  'C:\' deferred (no progress for 120s)
2026-11-05 19:44:01  'C:\' deferred (no progress for 120s)
2026-11-05 21:17:55  'C:\' deferred (no progress for 120s)
```

---

## 2. Fix 1 (F1): Exclude Google DriveFS Cache from Scan Walks

- **Commit**: `6201cba` (`fix F1: exclude Google DriveFS cache from scan walks`)
- **Anchors Verified**:
  - `def scan_full(` at `ezexp/indexer.py:177`
  - `def scan_delta(` at `ezexp/indexer.py:338`
- **Changes**:
  - Dynamically derived `%LOCALAPPDATA%\Google\DriveFS` at runtime using `os.environ.get("LOCALAPPDATA")` (no hardcoded usernames).
  - Added helper predicates `_is_drivefs_cache_path(path, dfs_cache)` and `_cache_exclusion_applies(dfs_cache, roots)`.
  - Added `_purge_drivefs_cache(conn, dfs_cache)` to prune pre-existing rows from `files`, `files_fts`, and `note_links` in a bounded batch transaction on first scan.
  - In both `scan_full` and `scan_delta`, skipped entering the DriveFS cache root or descending into any child directory under it for any scanned root.
- **Red-First Test Failure Evidence**:
  ```text
  AssertionError: F1(a) failed: DriveFS subtree was indexed (4 rows)
  ```
- **Post-Fix Pass Evidence**:
  - Test (a): Full scan with fixture tree containing `Google\DriveFS` indexed all other files, leaving `0` DriveFS rows in catalog.
  - Test (b): Delta scan with churned mtimes inside `Google\DriveFS` completed with `0` visits to the subtree.
  - Test (c): Pre-seeded catalog rows under DriveFS prefix were completely purged after one scan (`0` rows remaining).
- **Test Suite Tail**:
  ```text
  ALL TESTS PASSED
  ```

---

## 3. Fix 2 (F2): Link Analyzer Survives Delta Scans; Stopped Only for Full/Rebuild

- **Commit**: `ac271a8` (`fix F2: link analyzer survives delta scans; stopped only for full/rebuild`)
- **Anchor Verified**:
  - Single occurrence of `self._analyzer_stop.set()` inside `start_indexer` in `ezexp/main_window.py` (~L1306).
- **Changes**:
  - Imported `launch_needs_full` from `ezexp.indexer`.
  - Read `phase` and `files_count` via `thread_conn()` using existing DB metadata helpers.
  - Evaluated `launch_needs_full(rebuild, phase, files_count)`:
    - Delta scans: link analyzer survives uninterrupted (`_analyzer_stop` remains unset).
    - Full scans / Rebuilds: `_analyzer_stop.set()` is triggered and waits up to 15s for the analyzer thread to quiesce before wiping and starting `IndexerThread`.
- **Red-First Test Failure Evidence**:
  ```text
  AssertionError: delta scan must leave analyzer running
  ```
- **Post-Fix Pass Evidence**:
  - Delta scan `start_indexer(False)` with populated catalog and `phase='full_done'` verified `_analyzer_stop.is_set() is False`.
  - Rebuild `start_indexer(True)` verified `_analyzer_stop.is_set() is True`.
  - Existing analyzer-quiesce tests pass.
- **Test Suite Tail**:
  ```text
  ALL TESTS PASSED
  ```

---

## 4. Fix 3 (F3): Vault Graph Renders and Grows During Analysis

- **Commit**: `eae167e` (`fix F3: vault graph renders from existing links and refreshes during analysis`)
- **Anchor Verified**:
  - `ezexp/main_window.py:847`, inside `on_links_progress`: `self.graph_view.show_message(f"Analyzing vault links… {remaining} notes left")`
- **Changes**:
  - In `refresh_graph()`, protected the canvas from being wiped by guarding `self.graph_view.show_message()` with `if not (self.graph_view.nodes or self.graph_view._items):`.
  - In `on_links_progress()`, removed the canvas-wiping message, updated `status_left` with `Analyzing vault links… {remaining} notes left`, and added throttled `refresh_graph()` (15s throttle and only when `remaining` decreased).
  - In `on_graph_ready()`, rendered nodes/links from existing partial DB data and preserved the analysis countdown in the status line when `rem > 0`.
- **Red-First Test Failure Evidence**:
  ```text
  AssertionError: Graph must have items!
  (len(graph_view._items) == 0 due to show_message wiping the canvas)
  ```
- **Post-Fix Pass Evidence**:
  - Vault graph renders nodes immediately (`len(win.graph_view._items) > 0`) without waiting for `links_done`.
  - Status line contains the countdown (`"2 notes left"`).
- **Visual Evidence**:
  - `screenshot_before.png` (48,927 bytes): Blank canvas displaying only the centered message *"Analyzing vault links… 3 notes left"*.
  - `screenshot_after.png` (90,807 bytes): Rendered interactive radial graph with nodes visible and status line *"3 notes · 1 links · MyVault · Analyzing vault links… 2 notes left"*.
- **Test Suite Tail**:
  ```text
  ALL TESTS PASSED
  ```

---

## 5. Rebuilt Executable Metadata

Built via PyInstaller using the repository configuration (`ezexp.spec` / `version_info.txt` / `ezexp.ico`):

- **Path**: `D:\exp\ezexp\dist\ezexp.exe`
- **Size**: `48,087,055 bytes`
- **SHA256**: `34f66637b40d29c919d527e96ed83a968036b186038c82a3dde9e42eac6adbd9`

---

## 6. Git Commit Sequence

1. `6201cba`: `fix F1: exclude Google DriveFS cache from scan walks`
2. `ac271a8`: `fix F2: link analyzer survives delta scans; stopped only for full/rebuild`
3. `eae167e`: `fix F3: vault graph renders from existing links and refreshes during analysis`
