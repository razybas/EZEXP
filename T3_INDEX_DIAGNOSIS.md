# T3 — Diagnosis: "Index error: database disk image is malformed"

**Date**: 2026-10-30 20:25 IST  
**Target File**: `%LOCALAPPDATA%\EzExp\index.db` (Owner's live index)  
**Status**: DIAGNOSIS ONLY — Zero code changes, zero modifications to live database.

---

## 1. Code Path Surfacing the Error

When the status bar displays `"Index error: database disk image is malformed"`, the exact execution trace is:

1. **Origin of Error**:
   - File: [`ezexp/indexer.py:280-305`](file:///D:/exp/ezexp/ezexp/indexer.py#L280-L305) (inside `IndexerThread._run_once()`).
   - `IndexerThread` acquires a dedicated connection via `conn = db_connect()` and calls `scan_delta(conn, roots, prog, alive)` or `scan_full(conn, roots, prog, alive)`.
   - During scanning, SQLite executes batch inserts/deletions/updates or B-Tree node lookups (`files`, `fts`, `idx_files_key`).
   - SQLite encounters error code `SQLITE_CORRUPT` (11), raising `sqlite3.DatabaseError: database disk image is malformed` (or `sqlite3.OperationalError`).

2. **Exception Catch & Emission**:
   - File: [`ezexp/indexer.py:322-330`](file:///D:/exp/ezexp/ezexp/indexer.py#L322-L330) (inside `IndexerThread.run()`):
     ```python
     except sqlite3.OperationalError as exc:
         if "locked" in str(exc) and attempt < 4:
             time.sleep(3)
             continue
         self.done.emit(-1, str(exc))
         return
     except Exception as exc:
         self.done.emit(-1, str(exc))
         return
     ```
   - Because `"locked"` is not in `"database disk image is malformed"`, the exception is immediately captured and emitted as:
     `self.done.emit(-1, "database disk image is malformed")`.

3. **Status Bar Surfacing**:
   - File: [`ezexp/main_window.py:1264, 1288-1300`](file:///D:/exp/ezexp/ezexp/main_window.py#L1288-L1300) (inside `MainWindow.on_index_done(self, total, err)`):
     ```python
     if err and err != "stopped":
         if "locked" in err and not getattr(self, "_index_lock_retried", False):
             ...
         self.status_right.setText(f"Index error: {err}")
         self._consume_pending_index()
         return
     ```
   - Line 1298 directly sets `self.status_right.setText("Index error: database disk image is malformed")`, rendering the red/grey warning on the bottom-right of the MainWindow.

---

## 2. Integrity Check on Database Copy (Verbatim Output)

A byte-for-byte copy of `%LOCALAPPDATA%\EzExp\index.db*` was copied to isolated scratch directory `D:\exp\scratch_db_diag\`:
- `index.db`: 1,934,032,896 bytes (1.93 GB)
- `index.db-shm`: 32,768 bytes
- `index.db-wal`: 19,602,992 bytes (19.6 MB)

### Verbatim Output from Python SQLite3

```
Connecting to copy at D:\exp\scratch_db_diag\index.db...

--- Running PRAGMA quick_check ---
quick_check completed in 6.66s (1 rows):
  ok

--- Running PRAGMA integrity_check ---
integrity_check completed in 0.28s (1 rows):
  ok

--- FTS5 Integrity Check ---
INSERT INTO fts(fts) VALUES('integrity-check');
  fts integrity-check: OK

--- Page & Freelist Analysis ---
page_size: 4096
page_count: 472176
freelist_count: 462012
files min, max, count: (1, 52135, 52135)
Drive counts:
  Drive C: 134
  Drive D: 52000
  Drive E: 1

--- Meta Table Contents ---
meta: total = 2157625
meta: indexed_at = 1793087808.870228
meta: phase = full_done
```

### Critical Finding from Page Analysis
While the file is **1.93 GB** (`472,176` pages of 4KB), **`462,012` pages (1.89 GB, 97.8%) reside in SQLite's freelist**.
Only **`52,135`** rows currently exist in `files`, despite `meta.total` reading `2,157,625`.

---

## 3. Concurrency & Process Analysis

1. **Running Processes**:
   - Restart Manager analysis on `index.db` showed active locks held by `ezexp.exe` process (e.g. PID `6968`).
   - Multiple instances of `ezexp.exe` have previously run concurrently when launched across testing/desktop sessions (e.g. PID `9468`, `24796`, `6968`).
   - While `app.py` has `acquire_single_instance()` using `QLockFile("ezexp.lock")`, if a process hangs or is killed abruptly without `QLockFile` cleanup, subsequent instances can contend on WAL locks.

2. **Cross-Thread Connection Architecture**:
   - `IndexerThread` opens its own separate connection via `db_connect()` at [`ezexp/indexer.py:280`](file:///D:/exp/ezexp/ezexp/indexer.py#L280).
   - Concurrently, `SearchWorker`, `MainWindow` UI thread, `AnalyzerThread`, and `BridgeWorker` (Agent RPC) use separate connections from thread-local storage via `thread_conn()` at [`ezexp/db.py:35`](file:///D:/exp/ezexp/ezexp/db.py#L35).
   - In [`ezexp/db.py:24`](file:///D:/exp/ezexp/db.py#L24), `db_connect()` executes `_ensure_schema(conn)` on **every new connection**, running `conn.executescript("CREATE TABLE IF NOT EXISTS...")`. In SQLite, `executescript()` automatically issues a `COMMIT` and requests exclusive DDL schema locks, causing contention if an indexing transaction is actively writing.

---

## 4. Ranked Hypotheses

### Hypothesis 1 (Rank 1 — Primary Cause): Disk Full (ENOSPC) / Torn WAL Page Write During Scan
- **Mechanism**: The system C: drive repeatedly reached 0 bytes free (exhausted) during heavy PyInstaller unpacks and desktop activity. SQLite writing to `index.db-wal` or appending pages during batch indexing received `ERROR_DISK_FULL`.
- **Evidence For**:
  - `error.log` in `%LOCALAPPDATA%\EzExp\error.log` records repeated historical `sqlite3.OperationalError: disk I/O error`.
  - When disk space ran out mid-write, SQLite threw `disk I/O error` or `database disk image is malformed` on incomplete page sync.
  - On the subsequent launch, SQLite WAL auto-recovery replayed valid frames and safely returned the uncommitted/rolled-back transaction pages to the **freelist** (explaining why `integrity_check` currently passes cleanly as `ok`, but 462,012 pages are sitting in the freelist and row count dropped to 52,135).
- **Evidence Against**: None. This matches both the filesystem history and the page statistics.

### Hypothesis 2 (Rank 2): Multi-Process WAL Contention / Abrupt Termination Mid-Rebuild
- **Mechanism**: An instance running a full rescan (`scan_full` with `wipe=True`) truncated rows and began repopulating, but was killed or collided with another running instance sharing `%LOCALAPPDATA%\EzExp\index.db`.
- **Evidence For**:
  - `ezexp.lock` had lingering stale lock entries from multiple PIDs.
  - Exactly 52,135 rows were re-inserted before the process stopped (52,000 on drive D:, 134 on drive C:).
- **Evidence Against**: `QLockFile` usually prevents two normal UI launches from opening the main window simultaneously unless one crashed without releasing the lock.

### Hypothesis 3 (Rank 3): FTS5 Virtual Table Corruption
- **Mechanism**: FTS5 index segments becoming out-of-sync with the underlying `files` table content.
- **Evidence For**: In v1.1.0 and prior, FTS joins were slow on 2.18M rows.
- **Evidence Against**: Verified directly on copy via `INSERT INTO fts(fts) VALUES('integrity-check')` — returned `OK`. FTS5 shadow tables (`fts_data`, `fts_idx`, `fts_docsize`) are structurally consistent.

---

## 5. Proposed Repair Plan & Risk Assessment

### Option A: Clean Rebuild via EzExp UI (RECOMMENDED)
- **Action**:
  1. Ensure no background `ezexp.exe` instances are running (`taskkill /F /IM ezexp.exe`).
  2. Launch EzExp and trigger "Rescan now" / Full Rebuild.
- **Why this is best**:
  - The database currently has 1.89 GB of empty freelist pages and is missing ~2.1M file entries that were rolled back during the disk full event.
  - Rebuilding will repopulate all 2.1M+ items cleanly from fixed drives into a fresh index.
- **Risks**: Takes ~5–10 minutes of disk traversal. 0 data loss risk (the index is a read-only catalog of files on disk).

### Option B: VACUUM the Database Copy First
- **Action**: Run `VACUUM;` on `index.db`.
- **Why**: Reclaims the 1.89 GB of dead freelist space immediately, reducing `index.db` from 1.93 GB to ~20 MB.
- **Risks**: Leaves the database with only 52,135 indexed items until a full rescan is performed anyway.

---

**Next Steps**: Awaiting owner decision. No destructive operations or code modifications have been or will be applied to `%LOCALAPPDATA%\EzExp\index.db`.
