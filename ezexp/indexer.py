import os
import sys
import time
import sqlite3
import threading
from PySide6.QtCore import QThread, Signal

from ezexp import DB_PATH
from ezexp.fsutil import (
    join_path, split_path, real_dir, ext_of, category_for, kind_for,
    SKIP_DIR_NAMES, SKIP_FILE_NAMES, fixed_drives, REPARSE_ATTR
)
from ezexp.settings_store import get_vaults
from ezexp.db import (
    db_connect, thread_conn, insert_rows, delete_tree,
    set_meta, get_meta, update_total, fts_touch,
    ensure_indexes, fts_drop_ids, rebuild_fts
)
from ezexp.search_engine import do_search
from ezexp.usn import (
    QueryInfo, read_checkpoint, checkpoint_valid, apply_records,
    write_checkpoint, _write_frn_rows,
)
from ezexp.usn_client import UsnClient, UsnUnavailable

BATCH = 4000

MTIME_EPS = 1e-6


def _locked_retry(conn, fn, attempts=12, rollback=True):
    """Run fn(); on a transient 'database is locked' retry the WHOLE fn
    after a growing backoff. fn must be idempotent (the scanners'
    batches are: INSERT OR IGNORE + commit). A single lock collision
    must never abort a multi-minute scan — IndexerThread's retry loop
    restarts the ENTIRE rebuild from zero otherwise (live-caught in the
    G4 rebuild). rollback=True rolls the txn back before retrying (for
    re-executable batches); for a failed COMMIT pass rollback=False —
    sqlite keeps the txn open and the retry just commits it again."""
    for i in range(attempts):
        try:
            fn()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or i == attempts - 1:
                raise
            if rollback:
                try:
                    conn.rollback()
                except Exception:
                    pass
            time.sleep(1.0 + i)

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
        def _wipe():
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM note_links")
            rebuild_fts(conn)
            conn.commit()
        _locked_retry(conn, _wipe)
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
                        b = list(batch)
                        _locked_retry(conn, lambda: (
                            insert_rows(conn, b, sync_fts=False),
                            conn.commit()))
                        if progress:
                            progress(d, total)
                        batch = []
        if batch:
            total += len(batch)
            b = list(batch)
            _locked_retry(conn, lambda: (
                insert_rows(conn, b, sync_fts=False),))
            batch = []
        _locked_retry(conn, conn.commit)
        if progress:
            progress(root, total)
    if should_run():
        def _finalize():
            rebuild_fts(conn)  # index all names in one pass
            set_meta(conn, "phase", "full_done")
            conn.commit()
        _locked_retry(conn, _finalize)

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
            # a lock hit at this commit must not drop the dir's txn:
            # sqlite keeps it open, the retry just commits it again
            _locked_retry(conn, conn.commit, rollback=False)
            if progress:
                progress(d, changed_items)

USN_RETRY_SECS = 30

# Set while IndexerThread._run_once is scanning: the watcher defers its
# enum bootstrap and delta-once until the scan finishes. Without this a
# first bootstrap after a catalog wipe launches a FULL scan per drive in
# parallel with the user's rebuild (live-caught: the rebuild crawled).
_INDEXER_BUSY = threading.Event()

class UsnWatcher(QThread):
    """Live tail of the USN helper, one client session at a time.

    Per fixed drive the helper reports ok: subscribe from the saved
    checkpoint (replaying everything that changed while EzExp was
    closed — no rescan), bootstrap via the helper's MFT enum when the
    checkpoint is missing/invalid/stale (plus one delta scan, since the
    frn map alone does not refresh the catalog), and consume live
    record batches afterwards. Every other drive stays on today's delta
    behaviour; the periodic/startup delta scan is untouched and remains
    the safety net for ALL drives."""
    usn_status = Signal(str)

    def __init__(self, client_factory=None, conn_factory=None, delta_fn=None):
        super().__init__()
        self._stop = False
        self._modes = {}
        self.last_status = ""
        self._make_client = client_factory or UsnClient
        self._make_conn = conn_factory or db_connect
        self._delta_once = delta_fn or scan_delta

    def stop(self):
        self._stop = True

    def _emit_status(self):
        line = "Live: " + " · ".join(
            "%s %s" % (d, m) for d, m in sorted(self._modes.items()))
        self.last_status = line
        self.usn_status.emit(line)

    def _set_mode(self, drive, mode):
        if self._modes.get(drive) != mode:
            self._modes[drive] = mode
            self._emit_status()

    def _sleep(self, secs):
        end = time.time() + secs
        while not self._stop and time.time() < end:
            time.sleep(0.25)

    def run(self):
        try:
            while not self._stop:
                client = None
                while not self._stop and client is None:
                    try:
                        client = self._make_client()
                        client.connect()
                    except UsnUnavailable:
                        client = None
                        for root in fixed_drives():
                            self._set_mode(root[:2], "delta")
                        self._sleep(USN_RETRY_SECS)  # reconnect replays
                if self._stop:
                    break
                try:
                    self._session(client)
                finally:
                    client.close()
        except Exception:
            pass  # the watcher must never take the app down; delta net holds

    def _session(self, client):
        ok = {d for d, p in (client.ready or {}).items() if p == "ok"}
        for root in fixed_drives():
            d = root[:2]
            if d not in ok:
                self._set_mode(d, "delta")  # USN is set by the worker
                # only once it is actually live, never optimistically
        workers = []
        for d in sorted(ok):
            t = threading.Thread(target=self._drive_worker,
                                 args=(client, d), daemon=True)
            t.start()
            workers.append(t)
        for t in workers:
            while t.is_alive() and not self._stop:
                t.join(0.2)

    def _drive_worker(self, client, drive):
        """One drive's tail. Order: replay from the checkpoint; enum
        bootstrap (with one delta scan) when missing/invalid; consume
        live batches until stop, stale (re-bootstrap), or a per-drive
        error (delta until the next reconnect). Each worker owns its
        sqlite connection — sqlite objects must not cross threads."""
        conn = None
        first = True
        try:
            conn = self._make_conn()
            while not self._stop:
                while _INDEXER_BUSY.is_set() and not self._stop:
                    time.sleep(0.5)  # a scan owns the catalog right now
                if first:
                    state = self._try_live_from_checkpoint(client, conn, drive)
                    first = False
                    if state == "dead" or self._stop:
                        return
                else:
                    state = "invalid"  # stale: go straight to the enum
                if state != "live":
                    if not self._bootstrap_via_pipe(client, conn, drive):
                        self._set_mode(drive, "delta")
                        return
                self._set_mode(drive, "USN")
                outcome = self._consume(client, conn, drive)
                if outcome == "stale":
                    continue  # journal fell away: re-bootstrap via enum
                if outcome == "error":
                    self._set_mode(drive, "delta")
                return
        except UsnUnavailable:
            pass  # pipe dropped: the session ends and reconnects
        except sqlite3.Error:
            # a transient lock/failure must not kill the thread silently:
            # mark the drive delta; the ~30 s reconnect re-bootstraps it
            self._set_mode(drive, "delta")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _try_live_from_checkpoint(self, client, conn, drive):
        """subscribe(cp.next_usn) and validate against the helper's
        fresh journal_info. Returns 'live' | 'invalid' | 'dead'."""
        cp = read_checkpoint(conn, drive)
        if cp is None:
            return "invalid"
        if conn.execute("SELECT 1 FROM frn_map WHERE drive=? LIMIT 1",
                        (drive,)).fetchone() is None:
            return "invalid"
        client.send_subscribe(drive, cp.next_usn)
        while not self._stop:
            try:
                msg = client.next_message(drive, timeout=1.0)
            except UsnUnavailable:
                return "dead"
            if msg is None:
                continue
            if msg["type"] == "journal_info":
                info = QueryInfo(msg["journal_id"], msg["first_usn"],
                                 msg["next_usn"])
                return "live" if checkpoint_valid(cp, info) else "invalid"
            return "invalid"  # stale/error, or records before validation
        return "dead"

    def _bootstrap_via_pipe(self, client, conn, drive):
        """Helper enum -> frn_map + checkpoint, one delta scan (the map
        alone does not refresh the catalog), then subscribe from the
        enum's next_usn. Returns False on any failure."""
        try:
            def rows():
                for r in client.enum_rows(drive):
                    if self._stop:
                        raise UsnUnavailable("stopped")
                    yield r
            _write_frn_rows(conn, drive, rows())
            le = client.last_enum
            if le is None or le[0] != drive:
                return False
            write_checkpoint(conn, drive, le[1], le[2])
            nxt = le[2]
        except UsnUnavailable:
            return False
        if self._stop:
            return False
        try:
            self._delta_once(conn, [drive + "\\"], None,
                             lambda: not self._stop)
        except Exception:
            pass  # a failing delta must not kill the watcher
        client.send_subscribe(drive, nxt)
        return True

    def _consume(self, client, conn, drive):
        """Apply live record batches with poll()'s commit discipline:
        apply batch -> advance checkpoint -> commit. Returns
        'stale' | 'error' | 'dead' | 'stop'.

        While _INDEXER_BUSY is set (a scan owns the catalog) this worker
        STOPS READING entirely: the helper blocks on its pipe write, the
        records accumulate in the kernel journal, and the checkpoint
        replays them after the scan. Without this, the watcher's small
        commits interleave with the scan's batches and scan_full dies on
        'database is locked' — IndexerThread then restarts the whole
        rebuild from zero, over and over (live-caught)."""
        while not self._stop:
            while _INDEXER_BUSY.is_set() and not self._stop:
                time.sleep(0.5)
            try:
                msg = client.next_message(drive, timeout=0.5)
            except UsnUnavailable:
                return "dead"
            if msg is None:
                continue
            if msg["type"] == "records":
                while _INDEXER_BUSY.is_set() and not self._stop:
                    time.sleep(0.5)  # scan started between read and apply
                try:
                    apply_records(conn, drive, msg["records"])
                    set_meta(conn, "usn.%s.next_usn" % drive,
                             str(msg["next_usn"]))
                    conn.commit()
                except sqlite3.Error:
                    # the checkpoint did not advance, so the reconnect
                    # replays this batch — no loss
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                continue
            if msg["type"] == "stale":
                return "stale"
            if msg["type"] == "error":
                return "error"
            # a fresh journal_info (re-subscribe): nothing to apply
        return "stop"

_USN_WATCHER = None

def _usn_watcher():
    """The watcher is a singleton so the 10-minute indexer restarts do
    not churn the helper connection; IndexerThread.stop() ends it."""
    global _USN_WATCHER
    if _USN_WATCHER is None:
        _USN_WATCHER = UsnWatcher()
    return _USN_WATCHER

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
    usn_status = Signal(str)  # per-drive live mode line, e.g. "Live: C: USN · D: delta"

    def __init__(self, rebuild=False):
        super().__init__()
        self.rebuild = rebuild
        self._stop = False

    def stop(self):
        self._stop = True
        # the watcher is app-lifetime: the existing indexer stop path
        # ends it too (app.py calls stop()+wait() on shutdown)
        global _USN_WATCHER
        w = _USN_WATCHER
        if w is not None:
            w.stop()
            w.wait(2000)

    def _ensure_usn_watcher(self):
        if os.environ.get("EZEXP_NO_USN"):
            return  # hermetic test runs; the battery exercises the live path
        try:
            w = _usn_watcher()
            if not w.isRunning():
                w.usn_status.connect(self.usn_status)
                w.start(QThread.LowPriority)
        except Exception:
            pass  # the watcher must never block indexing; delta net holds

    def _run_once(self):
        conn = None
        _INDEXER_BUSY.set()
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
            _INDEXER_BUSY.clear()
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def run(self):
        self._ensure_usn_watcher()
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
