"""Pure-stdlib NTFS USN journal layer.

This module must stay import-clean for the elevated helper program
(usn_helper_main.py): it imports ONLY the standard library — no other
ezexp module, no PySide6, no sqlite. Its entire capability is opening
journals, reading records, and enumerating the MFT.

Extracted verbatim from usn.py (U1) except where stated; usn.py imports
these names back so its public surface is unchanged.
"""
import ctypes
import ctypes.wintypes as wintypes
import struct
from collections import namedtuple

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

ERROR_ACCESS_DENIED = 5
ERROR_HANDLE_EOF = 38
ERROR_JOURNAL_NOT_ACTIVE = 1176
ERROR_JOURNAL_DELETE_IN_PROGRESS = 1178
ERROR_JOURNAL_ENTRY_DELETED = 1181

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

ENUM_BATCH_ROWS = 2000  # helper streams MFT passes in rows-per-message caps

QueryInfo = namedtuple("QueryInfo", "journal_id first_usn next_usn")
USNRecord = namedtuple("USNRecord", "frn parent_frn usn reason attrs name")


def _read_request(start_usn, journal_id):
    """READ_USN_JOURNAL_DATA_V0 wire layout: StartUsn FIRST, UsnJournalID
    LAST (StartUsn; ReasonMask; ReturnOnlyOnClose; Timeout; BytesToWaitFor;
    UsnJournalID — 40 bytes packed). StartUsn is the explicit resume
    cursor; a nonzero value below the journal's FirstUsn fails with
    ERROR_JOURNAL_ENTRY_DELETED, which the caller treats as a wrap."""
    return struct.pack("<qIIqqQ", start_usn, 0xFFFFFFFF, 1, 0, 0, journal_id)


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
        empty buffer (next_usn == start_usn) means no records are
        available right now."""
        if self._jid is None:
            self._jid = self.query().journal_id
        inbuf = _read_request(start_usn, self._jid)
        try:
            raw = self._ioctl(FSCTL_READ_USN_JOURNAL, inbuf, max_bytes)
        except OSError as exc:
            if getattr(exc, "winerror", None) == ERROR_HANDLE_EOF:
                return start_usn, b""
            raise
        next_usn = struct.unpack_from("<Q", raw, 0)[0]
        return next_usn, raw[8:]

    def iter_enum(self):
        """One MFT pass via FSCTL_ENUM_USN_DATA, streamed record by
        record: yields (frn, parent_frn, name). The volume root arrives
        as frn 5. HighUsn must be a REAL USN bound — the journal's
        current NextUsn; a saturated 0xFFFF...F range returns zero
        records on real volumes (live-verified in the U2 battery)."""
        high = self.query().next_usn
        start = 0
        while True:
            inbuf = struct.pack("<QQQ", start, 0, high)
            try:
                raw = self._ioctl(FSCTL_ENUM_USN_DATA, inbuf, 1 << 16)
            except OSError as exc:
                if getattr(exc, "winerror", None) == ERROR_HANDLE_EOF:
                    break
                raise
            start = struct.unpack_from("<Q", raw, 0)[0]
            for rec in parse_records(raw[8:]):
                yield (rec.frn, rec.parent_frn, rec.name)
            if start == 0:
                break  # no forward progress; do not spin

    def enum(self):
        """The whole MFT pass as a list (U1 seam)."""
        return list(self.iter_enum())


def probe(drive):
    """Whether a drive's USN journal is usable from this token.

    Returns one of: 'ok', 'not_ntfs', 'no_journal', 'access_denied',
    'error:<detail>'. access_denied is a legitimate outcome for an
    unelevated process — callers must degrade to the delta scan. Lives
    here (not usn.py) so the helper can probe without importing the
    catalog side."""
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
