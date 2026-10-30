"""NTFS USN change journal support: real-time index freshness.

The USN change journal is the mechanism Everything (voidtools) uses:
NTFS appends one record per file-system change to a per-volume journal,
so reading it beats walking directories for both speed and completeness.

Everything USN lives in this module except the watcher wiring in
indexer.py and the frn_map table in db.py. The ctypes volume layer is
isolated in UsnJournal so tests can substitute any object exposing the
same query()/read()/enum() shape; the applier works against any sqlite
connection plus such a journal-like object.

Reading the journal needs an elevated token on most systems. Unelevated,
probe() returns access_denied and the caller must keep the mtime delta
scan running for that drive — never crash, never prompt for elevation.
"""
import ctypes
import ctypes.wintypes as wintypes
import os
import sqlite3
import struct
from collections import namedtuple

from ezexp import DB_PATH
from ezexp import db as db_mod
from ezexp.fsutil import (
    join_path, split_path, like_escape,
    SKIP_DIR_NAMES, SKIP_FILE_NAMES, REPARSE_ATTR,
)
from ezexp.db import (
    insert_rows, delete_tree, get_meta, set_meta,
)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = ctypes.c_void_p
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB
FSCTL_ENUM_USN_DATA = 0x000900B3

USN_VERSION_ID = 2

ERROR_ACCESS_DENIED = 5
ERROR_HANDLE_EOF = 38
ERROR_JOURNAL_NOT_ACTIVE = 1176
ERROR_JOURNAL_DELETE_IN_PROGRESS = 1178

USN_REASON_DATA_OVERWRITE = 0x00000001
USN_REASON_DATA_EXTEND = 0x00000002
USN_REASON_DATA_TRUNCATION = 0x00000004
USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000
USN_REASON_BASIC_INFO_CHANGE = 0x00008000
USN_REASON_CLOSE = 0x80000000

REASON_MASK = (USN_REASON_DATA_OVERWRITE | USN_REASON_DATA_EXTEND |
               USN_REASON_DATA_TRUNCATION | USN_REASON_FILE_CREATE |
               USN_REASON_FILE_DELETE | USN_REASON_RENAME_OLD_NAME |
               USN_REASON_RENAME_NEW_NAME | USN_REASON_BASIC_INFO_CHANGE)

FILE_ATTRIBUTE_DIRECTORY = 0x10

ROOT_FRN = 5

READ_CHUNK = 1 << 18  # bytes per FSCTL_READ_USN_JOURNAL (~2.5k records)
MAX_CHUNKS_PER_POLL = 64  # ~16M bytes drained per poll cycle, then reschedule

QueryInfo = namedtuple("QueryInfo", "journal_id first_usn next_usn")
Checkpoint = namedtuple("Checkpoint", "journal_id next_usn")
USNRecord = namedtuple("USNRecord", "frn parent_frn usn reason attrs name")


def parse_records(buf):
    """Parse a byte buffer of consecutive USN_RECORD_V2 records (the
    READ_USN_JOURNAL / ENUM_USN_DATA payload after its leading USN or
    StartFileReferenceNumber). Stops cleanly at any truncation."""
    out = []
    off = 0
    n = len(buf)
    while off + 60 <= n:
        rec_len = struct.unpack_from("<I", buf, off)[0]
        if rec_len < 60 or off + rec_len > n:
            break
        major, = struct.unpack_from("<H", buf, off + 4)
        if major != 2:
            break  # only V2 records are understood here
        frn, pfrn, usn, _ts, reason, _src, _sec, attrs = struct.unpack_from(
            "<QQqQIIII", buf, off + 8)
        fn_len, fn_off = struct.unpack_from("<HH", buf, off + 56)
        if fn_off + fn_len > rec_len or off + fn_off + fn_len > n:
            break
        name = buf[off + fn_off: off + fn_off + fn_len].decode("utf-16-le", "replace")
        out.append(USNRecord(frn, pfrn, usn, reason, attrs, name))
        off += rec_len
    return out


class UsnJournal:
    """ctypes wrapper over one NTFS volume's USN change journal.

    drive is a letter with optional trailing backslash ('C:' / 'C:\\').
    """

    def __init__(self, drive):
        self.drive = drive.rstrip("\\")
        self._handle = None
        self._jid = None
        handle = _k32.CreateFileW(
            ctypes.c_wchar_p("\\\\.\\%s" % self.drive),
            GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, 0, None)
        if handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle

    def close(self):
        if self._handle is not None:
            _k32.CloseHandle(self._handle)
            self._handle = None

    def _ioctl(self, code, inbuf, out_size):
        out = ctypes.create_string_buffer(out_size)
        returned = wintypes.DWORD(0)
        ok = _k32.DeviceIoControl(self._handle, code, inbuf,
                                  len(inbuf) if inbuf else 0,
                                  out, out_size, ctypes.byref(returned), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return out.raw[:returned.value]

    def query(self):
        """FSCTL_QUERY_USN_JOURNAL -> QueryInfo(journal_id, first, next)."""
        raw = self._ioctl(FSCTL_QUERY_USN_JOURNAL, None, 56)
        jid, first, nxt = struct.unpack_from("<QQQ", raw, 0)
        return QueryInfo(jid, first, nxt)

    def read(self, start_usn, max_bytes=READ_CHUNK):
        """One FSCTL_READ_USN_JOURNAL call from start_usn, close records
        only. Returns (next_usn_after_this_buffer, records_bytes); an
        empty buffer means no records are available right now."""
        if self._jid is None:
            self._jid = self.query().journal_id
        inbuf = struct.pack("<QIIQQI", self._jid, 0xFFFFFFFF, 1, 0, 0,
                            USN_VERSION_ID)
        try:
            raw = self._ioctl(FSCTL_READ_USN_JOURNAL, inbuf, max_bytes)
        except OSError as exc:
            if getattr(exc, "winerror", None) == ERROR_HANDLE_EOF:
                return start_usn, b""
            raise
        next_usn = struct.unpack_from("<Q", raw, 0)[0]
        return next_usn, raw[8:]

    def enum(self):
        """One MFT pass via FSCTL_ENUM_USN_DATA: every file-name record
        as (frn, parent_frn, name). The volume root arrives as frn 5."""
        out = []
        start = 0
        while True:
            inbuf = struct.pack("<QQQ", start, 0, 0xFFFFFFFFFFFFFFFF)
            try:
                raw = self._ioctl(FSCTL_ENUM_USN_DATA, inbuf, 1 << 16)
            except OSError as exc:
                if getattr(exc, "winerror", None) == ERROR_HANDLE_EOF:
                    break
                raise
            start = struct.unpack_from("<Q", raw, 0)[0]
            for rec in parse_records(raw[8:]):
                out.append((rec.frn, rec.parent_frn, rec.name))
            if start == 0:
                break  # no forward progress; do not spin
        return out


def probe(drive):
    """Whether a drive's USN journal is usable from this token.

    Returns one of: 'ok', 'not_ntfs', 'no_journal', 'access_denied',
    'error:<detail>'. access_denied is a legitimate outcome for an
    unelevated process — callers must degrade to the delta scan."""
    root = drive if drive.endswith("\\") else drive + "\\"
    fs = ctypes.create_unicode_buffer(64)
    if not _k32.GetVolumeInformationW(ctypes.c_wchar_p(root), None, 0,
                                      None, None, None, fs, 64):
        return "error:GetVolumeInformationW:%d" % ctypes.get_last_error()
    if fs.value.lower() != "ntfs":
        return "not_ntfs"
    try:
        journal = UsnJournal(drive)
    except OSError as exc:
        if getattr(exc, "winerror", None) == ERROR_ACCESS_DENIED:
            return "access_denied"
        return "error:open:%s" % exc
    try:
        journal.query()
        return "ok"
    except OSError as exc:
        we = getattr(exc, "winerror", None)
        if we in (ERROR_JOURNAL_NOT_ACTIVE, ERROR_JOURNAL_DELETE_IN_PROGRESS):
            return "no_journal"
        if we == ERROR_ACCESS_DENIED:
            return "access_denied"
        return "error:query:%s" % exc
    finally:
        journal.close()


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
    rows = [(drive, ROOT_FRN, 0, drive.rstrip("\\"))]
    for frn, pfrn, name in journal.enum():
        if frn == ROOT_FRN or not name or name in (".", ".."):
            continue
        rows.append((drive, frn, pfrn, name))
    for i in range(0, len(rows), 50000):
        conn.executemany(
            "INSERT OR REPLACE INTO frn_map(drive,frn,parent_frn,name)"
            " VALUES(?,?,?,?)", rows[i:i + 50000])
    set_meta(conn, "usn.%s.journal_id" % drive, str(info.journal_id))
    set_meta(conn, "usn.%s.next_usn" % drive, str(info.next_usn))
    conn.commit()
    return info.next_usn


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
    catches those later."""
    applied = 0
    skipped = 0
    pending_renames = {}  # frn -> (old_parent_frn, old_name)
    for rec in records:
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
