"""NTFS USN change journal support: real-time index freshness.

The USN change journal is the mechanism Everything (voidtools) uses:
NTFS appends one record per file-system change to a per-volume journal,
so reading it beats walking directories for both speed and completeness.

Everything USN lives in this module except the watcher wiring in
indexer.py, the frn_map table in db.py, and the pure journal layer now
in usn_journal.py (extracted so the elevated helper program can import
it without pulling in PySide6/db). This module re-exports the journal
layer, so its public surface is unchanged.

Reading the journal needs an elevated token on most systems. Unelevated,
probe() returns access_denied and the caller must keep the mtime delta
scan running for that drive — never crash, never prompt for elevation.
The elevated-helper route (U2) reads the journals in a LocalSystem
service and streams records over a named pipe; this module stays the
catalog side: checkpoints, frn map, and all writes.
"""
import ctypes
import os
import sqlite3
from collections import namedtuple

from ezexp import DB_PATH
from ezexp import db as db_mod
from ezexp import usn_journal as _uj
from ezexp.fsutil import (
    join_path, split_path, like_escape,
    SKIP_DIR_NAMES, SKIP_FILE_NAMES, REPARSE_ATTR,
)
from ezexp.db import (
    insert_rows, delete_tree, get_meta, set_meta,
)

# Re-export the pure journal layer: usn.py's public U1 surface is unchanged.
from ezexp.usn_journal import (  # noqa: F401
    UsnJournal, parse_records, QueryInfo, USNRecord, probe,
    GENERIC_READ, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
    INVALID_HANDLE_VALUE,
    FSCTL_QUERY_USN_JOURNAL, FSCTL_READ_USN_JOURNAL, FSCTL_ENUM_USN_DATA,
    ERROR_ACCESS_DENIED, ERROR_HANDLE_EOF, ERROR_JOURNAL_NOT_ACTIVE,
    ERROR_JOURNAL_DELETE_IN_PROGRESS, ERROR_JOURNAL_ENTRY_DELETED,
    USN_REASON_DATA_OVERWRITE, USN_REASON_DATA_EXTEND,
    USN_REASON_DATA_TRUNCATION, USN_REASON_FILE_CREATE,
    USN_REASON_FILE_DELETE, USN_REASON_RENAME_OLD_NAME,
    USN_REASON_RENAME_NEW_NAME, USN_REASON_BASIC_INFO_CHANGE,
    USN_REASON_CLOSE, REASON_MASK, FILE_ATTRIBUTE_DIRECTORY,
    ROOT_FRN, READ_CHUNK, MAX_CHUNKS_PER_POLL, ENUM_BATCH_ROWS,
    _read_request, _k32,
)

Checkpoint = namedtuple("Checkpoint", "journal_id next_usn")


def read_checkpoint(conn, drive):
    jid = get_meta(conn, "usn.%s.journal_id" % drive)
    nxt = get_meta(conn, "usn.%s.next_usn" % drive)
    if jid is None or nxt is None:
        return None
    try:
        return Checkpoint(int(jid), int(nxt))
    except (TypeError, ValueError):
        return None


def checkpoint_valid(cp, info):
    """False when the journal wrapped (saved USN below FirstUsn), when
    the journal id changed, or when there is nothing saved — the caller
    must then fall back to the delta scan and re-bootstrap."""
    return (cp is not None and info is not None
            and cp.journal_id == info.journal_id
            and cp.next_usn >= info.first_usn)


def bootstrap_volume(conn, journal, drive):
    """Populate frn_map for a volume with one MFT pass and reset the
    per-drive checkpoint. The checkpoint records the journal's NextUsn
    BEFORE the pass so the replay window covers the bootstrap itself.
    Returns the captured NextUsn."""
    info = journal.query()
    bootstrap_rows(conn, drive, journal.enum(), info.journal_id,
                   info.next_usn)
    return info.next_usn


def bootstrap_rows(conn, drive, rows, journal_id, next_usn):
    """The pipe form of bootstrap_volume: consume an iterator of
    (frn, parent_frn, name) rows with an explicit pre-pass journal_id /
    next_usn (captured by the helper BEFORE its MFT pass). Same frn_map
    and checkpoint writes as the U1 journal-object form."""
    all_rows = [(drive, ROOT_FRN, 0, drive.rstrip("\\"))]
    for frn, pfrn, name in rows:
        if frn == ROOT_FRN or not name or name in (".", ".."):
            continue
        all_rows.append((drive, frn, pfrn, name))
    for i in range(0, len(all_rows), 50000):
        conn.executemany(
            "INSERT OR REPLACE INTO frn_map(drive,frn,parent_frn,name)"
            " VALUES(?,?,?,?)", all_rows[i:i + 50000])
    set_meta(conn, "usn.%s.journal_id" % drive, str(journal_id))
    set_meta(conn, "usn.%s.next_usn" % drive, str(next_usn))
    conn.commit()
    return next_usn


def resolve_frn_path(conn, drive, frn):
    """Absolute path for an FRN by walking parent FRNs up to the volume
    root. None when any ancestor is missing from the map — callers must
    skip the record, never guess a path."""
    parts = []
    seen = set()
    cur = frn
    while True:
        if cur in seen:
            return None
        seen.add(cur)
        row = conn.execute(
            "SELECT parent_frn, name FROM frn_map WHERE drive=? AND frn=?",
            (drive, cur)).fetchone()
        if row is None:
            return None
        pfrn, name = row
        parts.append(name)
        if pfrn == 0:
            break
        cur = pfrn
    return "\\".join(reversed(parts))


def _forget_frn_subtree(conn, drive, frn):
    conn.execute(
        "WITH RECURSIVE d(frn) AS ("
        " SELECT ? UNION"
        " SELECT f.frn FROM frn_map f, d WHERE f.parent_frn = d.frn AND f.drive = ?)"
        " DELETE FROM frn_map WHERE drive = ? AND frn IN (SELECT frn FROM d)",
        (frn, drive, drive))


def _scanner_skip(name, is_dir):
    # lazy: indexer.py imports this module, so import-time would cycle
    from ezexp.indexer import _entry_skipped
    return _entry_skipped(name, is_dir)


def _row_for(parent, name, is_dir, st, drive):
    from ezexp.indexer import _make_row
    return _make_row(parent, name, is_dir, st, drive)


def apply_records(conn, drive, records):
    """Apply parsed USN records (in Usn order) to the catalog and the
    frn map. Only close records carrying the REASON_MASK reasons do
    anything. Returns (applied, skipped); skipped counts records dropped
    for an unknown parent FRN or an unpaired rename — the delta net
    catches those later. records may be USNRecord namedtuples or
    [frn, parent_frn, usn, reason, attrs, name] lists from the pipe."""
    applied = 0
    skipped = 0
    pending_renames = {}  # frn -> (old_parent_frn, old_name)
    for rec in records:
        if not isinstance(rec, USNRecord):
            rec = USNRecord(*rec)
        if not (rec.reason & USN_REASON_CLOSE):
            continue
        reason = rec.reason & ~USN_REASON_CLOSE & 0xFFFFFFFF
        if not (reason & REASON_MASK):
            continue
        if reason & USN_REASON_RENAME_OLD_NAME:
            pending_renames[rec.frn] = (rec.parent_frn, rec.name)
            continue
        if reason & USN_REASON_RENAME_NEW_NAME:
            old = pending_renames.pop(rec.frn, None)
            if old is None:
                skipped += 1
                continue
            ok, unk = _apply_rename(conn, drive, old[0], old[1], rec)
        elif reason & USN_REASON_FILE_DELETE:
            ok, unk = _apply_delete(conn, drive, rec)
        elif reason & USN_REASON_FILE_CREATE:
            ok, unk = _apply_create(conn, drive, rec)
        elif reason & (USN_REASON_DATA_OVERWRITE | USN_REASON_DATA_EXTEND |
                       USN_REASON_DATA_TRUNCATION | USN_REASON_BASIC_INFO_CHANGE):
            ok, unk = _apply_data_change(conn, drive, rec)
        else:
            continue
        applied += ok
        skipped += unk
    return applied, skipped


def _apply_create(conn, drive, rec):
    parent_path = resolve_frn_path(conn, drive, rec.parent_frn)
    if parent_path is None:
        return 0, 1
    conn.execute(
        "INSERT OR REPLACE INTO frn_map(drive,frn,parent_frn,name) VALUES(?,?,?,?)",
        (drive, rec.frn, rec.parent_frn, rec.name))
    is_dir = bool(rec.attrs & FILE_ATTRIBUTE_DIRECTORY)
    if _scanner_skip(rec.name, is_dir):
        return 1, 0
    path = parent_path + "\\" + rec.name
    if path.lower() == DB_PATH.lower():
        return 1, 0  # never index the index
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return 1, 0  # created and already gone; delta net settles it
    if is_dir and getattr(st, "st_file_attributes", 0) & REPARSE_ATTR:
        return 1, 0  # junction / symlink dir: never recursed, like the scanners
    insert_rows(conn, [_row_for(parent_path, rec.name, is_dir, st, drive)])
    return 1, 0


def _apply_delete(conn, drive, rec):
    path = resolve_frn_path(conn, drive, rec.frn)
    if path is None:
        return 0, 1
    _forget_frn_subtree(conn, drive, rec.frn)
    delete_tree(conn, path)  # file or directory subtree; commits
    return 1, 0


def _fts_rename(conn, rowid, old_name, new_name):
    """Re-index one renamed row. fts_touch() cannot be used here: its
    DELETE reads the row's CURRENT content from files, which after the
    rename no longer matches the indexed tokens and surfaces as
    'database disk image is malformed'. The explicit 'delete' command
    unindexes the supplied old value without touching the content table,
    so it is safe on either side of the row move."""
    if not db_mod.FTS_OK:
        return
    conn.execute("INSERT INTO fts(fts, rowid, name) VALUES('delete', ?, ?)",
                 (rowid, old_name))
    conn.execute("INSERT INTO fts(rowid, name) VALUES(?,?)", (rowid, new_name))


def _apply_rename(conn, drive, old_pfrn, old_name, rec):
    old_parent = resolve_frn_path(conn, drive, old_pfrn)
    new_parent = resolve_frn_path(conn, drive, rec.parent_frn)
    if old_parent is None or new_parent is None:
        return 0, 1
    old_path = join_path(old_parent, old_name)
    new_path = join_path(new_parent, rec.name)
    row = conn.execute("SELECT id, is_dir FROM files WHERE parent=? AND name=?",
                       (old_parent, old_name)).fetchone()
    if row is None:
        return 0, 1  # not indexed (skipped name / pre-bootstrap); delta net
    rid, is_dir = row
    try:
        if is_dir:
            conn.execute("UPDATE files SET name=? WHERE id=?", (rec.name, rid))
            n = len(old_path) + 1
            esc = like_escape(old_path)
            conn.execute(
                "UPDATE files SET parent = ? || substr(parent, ?)"
                " WHERE parent = ? OR parent LIKE ? || '\\%' ESCAPE '#'",
                (new_path, n, old_path, esc))
            conn.execute(
                "UPDATE note_links SET parent = ? || substr(parent, ?)"
                " WHERE parent = ? OR parent LIKE ? || '\\%' ESCAPE '#'",
                (new_path, n, old_path, esc))
        else:
            conn.execute("UPDATE files SET parent=?, name=? WHERE id=?",
                         (new_parent, rec.name, rid))
            conn.execute("UPDATE note_links SET name=? WHERE parent=? AND name=?",
                         (rec.name, old_parent, old_name))
    except sqlite3.IntegrityError:
        return 0, 1  # destination collision; delta net settles it
    _fts_rename(conn, rid, old_name, rec.name)  # descendants' names unchanged
    conn.execute(
        "INSERT OR REPLACE INTO frn_map(drive,frn,parent_frn,name) VALUES(?,?,?,?)",
        (drive, rec.frn, rec.parent_frn, rec.name))
    return 1, 0


def _apply_data_change(conn, drive, rec):
    path = resolve_frn_path(conn, drive, rec.frn)
    if path is None:
        return 0, 1
    parent, name = split_path(path)
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        _forget_frn_subtree(conn, drive, rec.frn)
        delete_tree(conn, path)  # gone between change and stat; commits
        return 1, 0
    is_dir = bool(rec.attrs & FILE_ATTRIBUTE_DIRECTORY)
    conn.execute("UPDATE files SET size=?, mtime=? WHERE parent=? AND name=?",
                 (0 if is_dir else st.st_size, st.st_mtime, parent, name))
    return 1, 0


def poll(conn, journal, drive):
    """Drain the currently-available close records of one volume and
    apply them, advancing the per-drive checkpoint with each applied
    batch. Returns a dict(applied, skipped, next_usn) or None when the
    checkpoint is missing or invalid — the caller must then bootstrap
    the volume or fall back to the delta scan."""
    info = journal.query()
    cp = read_checkpoint(conn, drive)
    if not checkpoint_valid(cp, info):
        return None
    usn = cp.next_usn
    applied = 0
    skipped = 0
    for _ in range(MAX_CHUNKS_PER_POLL):
        next_usn, buf = journal.read(usn, READ_CHUNK)
        if not buf:
            break
        recs = parse_records(buf)
        if not recs:
            # unparsable block: consume it and let the delta net settle
            usn = next_usn
            set_meta(conn, "usn.%s.next_usn" % drive, str(usn))
            conn.commit()
            break
        a, s = apply_records(conn, drive, recs)
        applied += a
        skipped += s
        usn = next_usn
        set_meta(conn, "usn.%s.next_usn" % drive, str(usn))
        conn.commit()
    return {"applied": applied, "skipped": skipped, "next_usn": usn}
