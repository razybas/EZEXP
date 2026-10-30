"""App-side named-pipe client for the EzExp USN helper service.

Talks to usn_helper_main.py over the usn_proto pipe. Everything that can
fail because the helper is absent, dropped, or refused raises a single
UsnUnavailable — the indexer treats that as "stay on the delta scan".

The transport is fakeable: tests inject a transport_factory (or subclass
and replace next_message) so session flows run headless.
"""
import ctypes
import ctypes.wintypes as wt
import struct
import threading
import time
from collections import deque

from ezexp.usn_proto import (
    PIPE_NAME, FrameDecoder, frame, m_enum, m_subscribe,
)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = ctypes.c_void_p
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID,
                             wt.DWORD, wt.DWORD, wt.LPVOID]
_k32.ReadFile.argtypes = [ctypes.c_void_p, wt.LPVOID, wt.DWORD,
                          ctypes.POINTER(wt.DWORD), wt.LPVOID]
_k32.WriteFile.argtypes = [ctypes.c_void_p, wt.LPCVOID, wt.DWORD,
                           ctypes.POINTER(wt.DWORD), wt.LPVOID]
_k32.PeekNamedPipe.argtypes = [ctypes.c_void_p, wt.LPVOID, wt.DWORD, wt.LPVOID,
                               ctypes.POINTER(wt.DWORD), wt.LPVOID]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

GENERIC_READ_WRITE = 0xC0000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class UsnUnavailable(Exception):
    """Helper pipe absent / dropped / refused — fall back to delta."""


class PipeClientTransport:
    """One helper connection, byte mode, framed per usn_proto."""

    def __init__(self, pipe_name):
        handle = _k32.CreateFileW(
            ctypes.c_wchar_p(pipe_name), GENERIC_READ_WRITE, 0, None,
            OPEN_EXISTING, 0, None)
        if handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        self._h = handle
        self._dec = FrameDecoder()
        self._pending = []

    def send_msg(self, msg):
        data = frame(msg)
        off = 0
        while off < len(data):
            got = wt.DWORD(0)
            if not _k32.WriteFile(self._h, ctypes.c_char_p(data[off:]),
                                  len(data) - off, ctypes.byref(got), None):
                raise ConnectionError("pipe write failed: %d"
                                      % ctypes.get_last_error())
            if got.value == 0:
                raise ConnectionError("pipe write returned 0 bytes")
            off += got.value

    def recv_msg(self, timeout=0.0):
        """One message when available, else None after timeout.
        Raises ConnectionError when the pipe is gone."""
        deadline = time.time() + timeout if timeout else None
        while True:
            if self._pending:
                return self._pending.pop(0)
            avail = wt.DWORD(0)
            if not _k32.PeekNamedPipe(self._h, None, 0, None,
                                      ctypes.byref(avail), None):
                raise ConnectionError("pipe gone: %d" % ctypes.get_last_error())
            if avail.value:
                (n,) = struct.unpack("<I", self._read_exact(4))
                if n == 0:
                    raise ConnectionError("empty frame from helper")
                # feed prefix + payload: the decoder expects whole frames
                self._dec.feed(struct.pack("<I", n))
                self._dec.feed(self._read_exact(n))
                self._pending = self._dec.pop_messages()
                continue
            if deadline is None or time.time() >= deadline:
                return None
            time.sleep(0.05)

    def _read_exact(self, n):
        chunks = []
        remaining = n
        while remaining:
            buf = ctypes.create_string_buffer(remaining)
            got = wt.DWORD(0)
            if not _k32.ReadFile(self._h, buf, remaining,
                                 ctypes.byref(got), None):
                raise ConnectionError("pipe read failed: %d"
                                      % ctypes.get_last_error())
            if got.value == 0:
                raise ConnectionError("helper closed the pipe")
            chunks.append(buf.raw[:got.value])
            remaining -= got.value
        return b"".join(chunks)

    def close(self):
        if self._h is not None:
            _k32.CloseHandle(self._h)
            self._h = None


class UsnClient:
    """Session API over the helper pipe. Thread-safe: per-drive watcher
    workers share one client; messages are multiplexed and buffered
    per drive."""

    def __init__(self, pipe_name=PIPE_NAME, connect_wait=2.0,
                 transport_factory=None):
        self.pipe_name = pipe_name
        self.connect_wait = connect_wait
        self.ready = None  # {drive: probe} once connected
        self.last_enum = None  # (drive, journal_id, next_usn) after enum
        self._t = None
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self._queues = {}  # drive -> deque of messages not yet consumed
        self._make_transport = transport_factory or PipeClientTransport

    def connect(self):
        deadline = time.time() + self.connect_wait
        while True:
            try:
                self._t = self._make_transport(self.pipe_name)
                break
            except OSError:
                if time.time() >= deadline:
                    raise UsnUnavailable(
                        "helper pipe %s not available" % self.pipe_name)
                time.sleep(0.2)
        msg = self.next_message(None, timeout=5.0)
        if msg is None or msg.get("type") != "ready":
            self.close()
            raise UsnUnavailable("helper did not announce ready")
        self.ready = msg["drives"]
        return self.ready

    def close(self):
        if self._t is not None:
            try:
                self._t.close()
            except Exception:
                pass
            self._t = None

    def send_subscribe(self, drive, start_usn):
        with self._send_lock:
            self._t.send_msg(m_subscribe(drive, start_usn))

    def send_enum(self, drive):
        with self._send_lock:
            self._t.send_msg(m_enum(drive))

    def next_message(self, drive=None, timeout=0.0):
        """Next message, pumping the shared pipe. Messages for other
        drives are buffered for their workers. drive=None accepts any.
        Returns None after timeout; raises UsnUnavailable when the pipe
        is gone."""
        deadline = time.time() + timeout if timeout else None
        while True:
            if drive is not None:
                q = self._queues.get(drive)
                if q:
                    return q.popleft()
            try:
                with self._recv_lock:
                    msg = self._t.recv_msg(timeout=0.05)
            except ConnectionError as exc:
                raise UsnUnavailable(str(exc))
            if msg is not None:
                if drive is None or msg.get("drive") in (None, drive):
                    return msg
                self._queues.setdefault(msg["drive"], deque()).append(msg)
                continue
            if deadline is not None and time.time() >= deadline:
                return None

    # --- session conveniences (used by tests and simple scripts; the
    # indexer worker uses send_subscribe/send_enum + next_message so that
    # multiplexed drives never lose messages) -------------------------------

    def journal_info(self, drive, start_usn=0):
        """Subscribe at start_usn and return the helper's fresh
        journal_info dict. Further messages stay queued for subscribe()."""
        self.send_subscribe(drive, start_usn)
        msg = self.next_message(drive, timeout=5.0)
        if msg is None:
            raise UsnUnavailable("no journal_info reply for %s" % drive)
        if msg["type"] == "error":
            raise UsnUnavailable(msg["error"])
        if msg["type"] != "journal_info":
            raise UsnUnavailable("expected journal_info, got %r" % msg["type"])
        return msg

    def enum_rows(self, drive):
        """Iterator over (frn, parent_frn, name) rows of one MFT pass.
        On exhaustion self.last_enum holds (drive, journal_id, next_usn)
        as captured by the helper BEFORE the pass."""
        self.send_enum(drive)
        while True:
            msg = self.next_message(drive, timeout=10.0)
            if msg is None:
                raise UsnUnavailable("no enum reply for %s" % drive)
            if msg["type"] == "enum_batch":
                for row in msg["rows"]:
                    yield tuple(row)
            elif msg["type"] == "enum_done":
                self.last_enum = (drive, msg["journal_id"], msg["next_usn"])
                return
            elif msg["type"] == "error":
                raise UsnUnavailable(msg["error"])
            elif msg["type"] == "stale":
                raise UsnUnavailable("journal went stale during enum")

    def subscribe(self, drive, start_usn):
        """Generator: yields the journal_info dict first, then
        ('records', rows, next_usn) tuples, then 'stale' at the end.
        Raises UsnUnavailable on any transport failure."""
        self.send_subscribe(drive, start_usn)
        while True:
            msg = self.next_message(drive, timeout=5.0)
            if msg is None:
                continue
            if msg["type"] == "journal_info":
                yield msg
            elif msg["type"] == "records":
                yield ("records", msg["records"], msg["next_usn"])
            elif msg["type"] == "stale":
                yield "stale"
                return
            elif msg["type"] == "error":
                raise UsnUnavailable(msg["error"])
