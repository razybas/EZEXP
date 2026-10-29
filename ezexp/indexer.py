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

BATCH = 4000

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
