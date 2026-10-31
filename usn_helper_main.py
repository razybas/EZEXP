"""EzExp USN journal helper — the elevated half of real-time indexing.

A tiny LocalSystem service (or a foreground console process, for the
battery and debugging) whose ENTIRE serving capability is: open USN
journals, read records, and serve one named pipe. The unelevated EzExp
app keeps owning the catalog, the checkpoints, and all writes; the
helper never touches the database, never writes files, never runs
commands.

PURITY (grep-audited): this program imports ONLY the standard library
plus ezexp.usn_journal / ezexp.usn_proto. It must not import sqlite3,
ezexp.db, ezexp.fsutil, PySide6 or ezexp.usn. The only file/command
activity in the whole program is the owner-run --install/--uninstall
(self-copy into %ProgramFiles% and sc.exe calls); the serving path does
neither.

Modes:
  --foreground   serve the pipe from a console (battery / debugging)
  --serve        run as a Windows service (SCM dispatcher; the binPath
                 carries --allow-sid)
  --install      OWNER-RUN, elevated: self-copy to
                 %ProgramFiles%\\EzExp\\EzExpUsnHelper.exe, register via
                 sc.exe with a failure-restart policy, start it
  --uninstall    OWNER-RUN, elevated: stop + delete the service, remove
                 the folder
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import shutil
import struct
import subprocess
import sys
import threading
import time

from ezexp.usn_journal import (
    UsnJournal, parse_records, probe, ERROR_JOURNAL_ENTRY_DELETED,
    READ_CHUNK, ENUM_BATCH_ROWS,
)
from ezexp.usn_proto import (
    PIPE_NAME, FrameDecoder, frame,
    m_ready, m_journal_info, m_enum_batch,
    m_enum_done, m_records, m_stale, m_error,
)

SERVICE_NAME = "EzExpUsn"
SERVICE_DISPLAY = "EzExp USN Journal Helper"

PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_BUFSZ = 1 << 20
ERROR_PIPE_CONNECTED = 535
ERROR_BROKEN_PIPE = 109
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063

SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_RUNNING = 0x00000004
SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_SHUTDOWN = 0x00000002
SERVICE_CONTROL_STOP = 0x00000001
SERVICE_CONTROL_SHUTDOWN = 0x00000005

TOKEN_QUERY = 0x0008
TOKEN_USER = 1
SDDL_REVISION_1 = 1

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)

_k32.CreateNamedPipeW.restype = ctypes.c_void_p
_k32.CreateNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD,
                                  wt.DWORD, wt.DWORD, wt.DWORD, wt.LPVOID]
_k32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, wt.LPVOID]
_k32.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]
_k32.ReadFile.argtypes = [ctypes.c_void_p, wt.LPVOID, wt.DWORD,
                          ctypes.POINTER(wt.DWORD), wt.LPVOID]
_k32.WriteFile.argtypes = [ctypes.c_void_p, wt.LPCVOID, wt.DWORD,
                           ctypes.POINTER(wt.DWORD), wt.LPVOID]
_k32.PeekNamedPipe.argtypes = [ctypes.c_void_p, wt.LPVOID, wt.DWORD, wt.LPVOID,
                               ctypes.POINTER(wt.DWORD), wt.LPVOID]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
# GetCurrentProcess returns a 64-bit PSEUDO-handle (-1); without a restype
# ctypes passes it as a 32-bit int and OpenProcessToken rejects the
# truncated value with ERROR_INVALID_HANDLE (live-caught in --install).
_k32.GetCurrentProcess.restype = ctypes.c_void_p
# RegisterServiceCtrlHandlerW returns a 64-bit SERVICE_STATUS_HANDLE; an
# unprototyped call truncates it to 32 bits and EVERY SetServiceStatus
# fails silently (live-caught: the service sat in START_PENDING forever)
_adv.RegisterServiceCtrlHandlerW.restype = ctypes.c_void_p
_adv.OpenProcessToken.argtypes = [ctypes.c_void_p, wt.DWORD,
                                  ctypes.POINTER(wt.HANDLE)]
_adv.OpenProcessToken.restype = wt.BOOL
_adv.GetTokenInformation.argtypes = [ctypes.c_void_p, wt.DWORD, wt.LPVOID,
                                     wt.DWORD, ctypes.POINTER(wt.DWORD)]
_adv.GetTokenInformation.restype = wt.BOOL
_adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(wt.LPWSTR)]
_adv.ConvertSidToStringSidW.restype = wt.BOOL


def log(text):
    print("[EzExpUsn] %s" % text, flush=True)


def fixed_drives():
    """Fixed drive letters ('C:') from GetLogicalDrives/GetDriveTypeW."""
    mask = _k32.GetLogicalDrives()
    out = []
    for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if mask >> i & 1:
            root = letter + ":"
            if _k32.GetDriveTypeW(ctypes.c_wchar_p(root + "\\")) == 3:
                out.append(root)
    return out


# --- injectable session (the headless-tested core) ---------------------------

class HelperSession:
    """Serves ONE connected client.

    transport: send_msg(dict) + recv_msg() -> dict | None (non-blocking;
               raises ConnectionError when the client is gone).
    journal_factory: drive -> journal-like object with query(),
               read(start_usn, max_bytes) -> (next_usn, raw_bytes) and
               iter_enum(). Defaults to the real UsnJournal.
    """

    def __init__(self, transport, journal_factory=None, drives=None,
                 idle_wait=0.3, stop_event=None, max_bytes=READ_CHUNK,
                 probe_fn=None):
        self.transport = transport
        self.make_journal = journal_factory or (lambda drive: UsnJournal(drive))
        self.probe_fn = probe_fn or probe
        self.drives = drives if drives is not None else fixed_drives()
        self.idle_wait = idle_wait
        self.stop_event = stop_event
        self.max_bytes = max_bytes
        self._journals = {}  # drive -> open journal object (handles)
        self._subs = {}      # drive -> {"usn": resume cursor}

    def send(self, msg):
        self.transport.send_msg(msg)

    def start(self):
        """On connect: probe every fixed drive and announce readiness."""
        probes = {d: self.probe_fn(d) for d in self.drives}
        self.send(m_ready(probes))

    def handle_command(self, msg):
        t = msg.get("type")
        if t == "subscribe":
            drive = msg["drive"]
            self._subs[drive] = {"usn": int(msg["start_usn"])}
            j = self._journal(drive)
            if j is None:
                return
            try:
                self.send(m_journal_info(drive, j.query()))
            except OSError as exc:
                self.send(m_error(drive, "query: %s" % exc))
        elif t == "enum":
            self._run_enum(msg["drive"])
        elif t == "ping":
            probes = {d: self.probe_fn(d) for d in self.drives}
            self.send(m_ready(probes))
        else:
            self.send(m_error(msg.get("drive", ""), "unknown command %r" % t))

    def pump_tails(self):
        """One service cycle over all subscribed drives: read what is
        available and forward it. Returns True if anything was sent."""
        sent = False
        for drive in list(self._subs):
            j = self._journals.get(drive)
            if j is None:
                j = self._journal(drive)
                if j is None:
                    del self._subs[drive]  # cannot open: drop this tail
                    continue
            st = self._subs[drive]
            try:
                next_usn, buf = j.read(st["usn"], self.max_bytes)
            except OSError as exc:
                if getattr(exc, "winerror", None) == ERROR_JOURNAL_ENTRY_DELETED:
                    self.send(m_stale(drive))
                    del self._subs[drive]
                else:
                    self.send(m_error(drive, "read: %s" % exc))
                sent = True
                continue
            recs = parse_records(buf)
            if recs:
                self.send(m_records(drive, recs, next_usn))
                st["usn"] = next_usn
                sent = True
        return sent

    def _journal(self, drive):
        j = self._journals.get(drive)
        if j is None:
            try:
                j = self.make_journal(drive)
            except OSError as exc:
                self.send(m_error(drive, "open: %s" % exc))
                return None
            self._journals[drive] = j
        return j

    def _run_enum(self, drive):
        j = self._journal(drive)
        if j is None:
            return
        try:
            info = j.query()  # captured BEFORE the pass, so the app's
            # replay window covers everything the pass itself changes
            batch = []
            for row in j.iter_enum():
                batch.append(row)
                if len(batch) >= ENUM_BATCH_ROWS:
                    self.send(m_enum_batch(drive, batch))
                    batch = []
            if batch:
                self.send(m_enum_batch(drive, batch))
            self.send(m_enum_done(drive, info.journal_id, info.next_usn))
        except OSError as exc:
            self.send(m_error(drive, "enum: %s" % exc))

    def close(self):
        """Client disconnect: close journal handles, forget all state
        (positions always come from the next client's subscribe)."""
        for j in self._journals.values():
            try:
                j.close()
            except Exception:
                pass
        self._journals.clear()
        self._subs.clear()

    def _stopping(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def run(self):
        """Command loop until the client disconnects or the service
        stops: commands when available, tails otherwise, ~idle_wait
        sleep when everything is quiet."""
        self.start()
        while not self._stopping():
            msg = self.transport.recv_msg()
            if msg is not None:
                self.handle_command(msg)
                continue
            if not self.pump_tails():
                time.sleep(self.idle_wait)


# --- real pipe transport (server side) ---------------------------------------

class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD),
                ("lpSecurityDescriptor", wt.LPVOID),
                ("bInheritHandle", wt.BOOL)]


def _security_attributes(allow_sid):
    """DACL granting GENERIC_ALL to LocalSystem and to the one --allow-sid
    user (or Authenticated Users in developer mode)."""
    if allow_sid:
        if not allow_sid.startswith("S-1-"):
            raise ValueError("--allow-sid must be a SID string (S-1-...)")
        sddl = "D:(A;;GA;;;SY)(A;;GA;;;%s)" % allow_sid
    else:
        sddl = "D:(A;;GA;;;SY)(A;;GA;;;AU)"
    psd = ctypes.c_void_p()
    if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            ctypes.c_wchar_p(sddl), SDDL_REVISION_1, ctypes.byref(psd), None):
        raise ctypes.WinError(ctypes.get_last_error())
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.lpSecurityDescriptor = psd.value
    sa.bInheritHandle = False
    return sa, psd


class PipeServerTransport:
    """One connected named-pipe client, byte mode, framed per usn_proto."""

    def __init__(self, handle):
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

    def recv_msg(self):
        if self._pending:
            return self._pending.pop(0)
        avail = wt.DWORD(0)
        if not _k32.PeekNamedPipe(self._h, None, 0, None,
                                  ctypes.byref(avail), None):
            raise ConnectionError("pipe gone: %d" % ctypes.get_last_error())
        if avail.value == 0:
            return None
        (n,) = struct.unpack("<I", self._read_exact(4))
        if n == 0:
            raise ConnectionError("empty frame from client")
        # feed prefix + payload: the decoder expects whole frames
        self._dec.feed(struct.pack("<I", n))
        self._dec.feed(self._read_exact(n))
        self._pending = self._dec.pop_messages()
        return self._pending.pop(0) if self._pending else None

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
                raise ConnectionError("client closed the pipe")
            chunks.append(buf.raw[:got.value])
            remaining -= got.value
        return b"".join(chunks)


def _create_pipe(allow_sid):
    sa, psd = _security_attributes(allow_sid)
    try:
        handle = _k32.CreateNamedPipeW(
            ctypes.c_wchar_p(PIPE_NAME),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
            0 | PIPE_REJECT_REMOTE_CLIENTS,  # byte type, byte read, wait
            1, PIPE_BUFSZ, PIPE_BUFSZ, 0, ctypes.byref(sa))
    finally:
        _k32.LocalFree(psd)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _unblock_pipe():
    """Open and close a dummy client so a blocked ConnectNamedPipe
    returns; used by the service control handler on stop."""
    GENERIC_WRITE = 0x40000000
    h = _k32.CreateFileW(ctypes.c_wchar_p(PIPE_NAME),
                         0x80000000 | GENERIC_WRITE, 0, None, 3, 0, None)
    if h != ctypes.c_void_p(-1).value:
        _k32.CloseHandle(h)


def serve_foreground(journal_factory=None, allow_sid=None, stop_event=None):
    """Accept clients one at a time until stopped. No state survives a
    disconnect: handles close, positions come from the next client."""
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            handle = _create_pipe(allow_sid)
        except OSError as exc:
            log("cannot create pipe (another helper running?): %s" % exc)
            time.sleep(2)
            continue
        connected = _k32.ConnectNamedPipe(handle, None)
        if not connected and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
            if stop_event is None or not stop_event.is_set():
                log("ConnectNamedPipe failed: %d" % ctypes.get_last_error())
            _k32.DisconnectNamedPipe(handle)
            _k32.CloseHandle(handle)
            continue
        session = HelperSession(PipeServerTransport(handle), journal_factory,
                                stop_event=stop_event)
        try:
            session.run()
        except (ConnectionError, OSError) as exc:
            log("client session ended: %s" % exc)
        finally:
            session.close()
            _k32.DisconnectNamedPipe(handle)
            _k32.CloseHandle(handle)


# --- Windows service plumbing (--serve) --------------------------------------

_svc_status_handle = None
_svc_stop = None  # threading.Event


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", wt.DWORD), ("dwCurrentState", wt.DWORD),
                ("dwControlsAccepted", wt.DWORD), ("dwWin32ExitCode", wt.DWORD),
                ("dwServiceSpecificExitCode", wt.DWORD),
                ("dwCheckPoint", wt.DWORD), ("dwWaitHint", wt.DWORD)]


# SetServiceStatus takes the 64-bit SERVICE_STATUS_HANDLE from
# RegisterServiceCtrlHandlerW (prototyped above) — without argtypes the
# struct POINTER would not bind correctly either
_adv.SetServiceStatus.argtypes = [ctypes.c_void_p,
                                  ctypes.POINTER(SERVICE_STATUS)]
_adv.SetServiceStatus.restype = wt.BOOL

_SERVICE_MAIN_FUNC = ctypes.WINFUNCTYPE(None, wt.DWORD, ctypes.POINTER(wt.LPWSTR))
_HANDLER_FUNC = ctypes.WINFUNCTYPE(None, wt.DWORD)


class SERVICE_TABLE_ENTRY(ctypes.Structure):
    _fields_ = [("lpServiceName", wt.LPWSTR),
                ("lpServiceProc", _SERVICE_MAIN_FUNC)]


def _svc_set_status(state, accept=0, wait_hint=0, exit_code=0):
    global _svc_status_handle
    if _svc_status_handle is None:
        return
    ss = SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, state, accept,
                        exit_code, 0, 0, wait_hint)
    _adv.SetServiceStatus(_svc_status_handle, ctypes.byref(ss))


def _svc_handler(ctrl):
    if ctrl in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
        _svc_set_status(SERVICE_STOP_PENDING, wait_hint=5000)
        _svc_stop.set()
        threading.Thread(target=_unblock_pipe, daemon=True).start()


def _svc_main(argc, argv):
    global _svc_status_handle
    args = [argv[i] for i in range(argc)]
    allow_sid = None
    if "--allow-sid" in args:
        i = args.index("--allow-sid")
        if i + 1 < argc:
            allow_sid = args[i + 1]
    _svc_status_handle = _adv.RegisterServiceCtrlHandlerW(
        ctypes.c_wchar_p(SERVICE_NAME), _handler_ref)
    _svc_set_status(SERVICE_START_PENDING)
    # announce RUNNING before serving: without this the SCM keeps the
    # service in START_PENDING forever (unstoppable, restart policy may
    # consider it hung) even though the pipe works
    _svc_set_status(SERVICE_RUNNING,
                    accept=SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN)
    try:
        if not allow_sid:
            log("service pipe grants Authenticated Users (no --allow-sid)")
        serve_foreground(allow_sid=allow_sid, stop_event=_svc_stop)
    finally:
        _svc_set_status(SERVICE_STOPPED, exit_code=0)


# keep callbacks alive for the SCM thread
_service_main_ref = _SERVICE_MAIN_FUNC(_svc_main)
_handler_ref = _HANDLER_FUNC(_svc_handler)


def run_as_service():
    global _svc_stop
    _svc_stop = threading.Event()
    table = (SERVICE_TABLE_ENTRY * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = _service_main_ref
    # table[1] stays zeroed as the terminator
    if not _adv.StartServiceCtrlDispatcherW(table):
        err = ctypes.get_last_error()
        if err == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            log("not started by the SCM; falling back to foreground")
            serve_foreground(stop_event=_svc_stop)
            return 0
        raise ctypes.WinError(err)
    return 0


# --- owner-run install / uninstall -------------------------------------------

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def current_user_sid():
    """The SID string of the token this process runs under."""
    tok = wt.HANDLE()
    if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), TOKEN_QUERY,
                                 ctypes.byref(tok)):
        raise ctypes.WinError(ctypes.get_last_error())
    n = wt.DWORD(0)
    _adv.GetTokenInformation(tok, TOKEN_USER, None, 0, ctypes.byref(n))
    buf = ctypes.create_string_buffer(n.value)
    if not _adv.GetTokenInformation(tok, TOKEN_USER, buf, n.value,
                                    ctypes.byref(n)):
        raise ctypes.WinError(ctypes.get_last_error())
    sid_ptr = ctypes.c_void_p.from_buffer_copy(buf)
    sid_str = wt.LPWSTR()
    if not _adv.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str)):
        raise ctypes.WinError(ctypes.get_last_error())
    return sid_str.value


def _install_dir():
    return os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                        "EzExp")


def _run_sc(argv, strict=True):
    print("  sc.exe %s" % " ".join(argv), flush=True)
    rc = subprocess.call(["sc.exe"] + argv)
    if rc != 0 and strict:
        print("  FAILED (sc.exe exit %d)" % rc, flush=True)
        return rc
    return 0


def cmd_install(args):
    if not is_admin():
        print("--install must run from an ELEVATED prompt "
              "(right-click the prompt -> Run as administrator).", flush=True)
        return 1
    if not getattr(sys, "frozen", False):
        print("--install must run from the built EzExpUsnHelper.exe "
              "(build_helper.bat), not from source.", flush=True)
        return 1
    dest_dir = _install_dir()
    dest = os.path.join(dest_dir, "EzExpUsnHelper.exe")
    sid = args.allow_sid or current_user_sid()
    print("Installing %s (%s)" % (SERVICE_DISPLAY, SERVICE_NAME), flush=True)
    print("  helper exe: %s" % dest, flush=True)
    print("  pipe access SID: %s" % sid, flush=True)
    os.makedirs(dest_dir, exist_ok=True)
    shutil.copy2(sys.executable, dest)
    if _run_sc(["create", SERVICE_NAME,
                "binPath=", '"%s" --serve --allow-sid %s' % (dest, sid),
                "DisplayName=", SERVICE_DISPLAY, "start=", "auto"]):
        return 1
    _run_sc(["description", SERVICE_NAME,
             "Reads NTFS USN change journals for EzExp (LocalSystem; "
             "journal access only, no files, no database)."], strict=False)
    _run_sc(["failure", SERVICE_NAME, "reset=", "86400", "actions=",
             "restart/30000/restart/60000/restart/300000"], strict=False)
    if _run_sc(["start", SERVICE_NAME]):
        return 1
    print("Done. Verify with: sc query %s" % SERVICE_NAME, flush=True)
    return 0


def cmd_uninstall(args):
    if not is_admin():
        print("--uninstall must run from an ELEVATED prompt.", flush=True)
        return 1
    print("Uninstalling %s" % SERVICE_NAME, flush=True)
    _run_sc(["stop", SERVICE_NAME], strict=False)
    _run_sc(["delete", SERVICE_NAME])
    shutil.rmtree(_install_dir(), ignore_errors=True)
    print("Done.", flush=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="%s — serves NTFS USN change journals to EzExp "
                    "over %s" % (SERVICE_DISPLAY, PIPE_NAME))
    ap.add_argument("--foreground", action="store_true",
                    help="serve the pipe from a console (battery/debugging)")
    ap.add_argument("--serve", action="store_true",
                    help="run as a Windows service (used by the SCM)")
    ap.add_argument("--install", action="store_true",
                    help="elevated, owner-run: install and start the service")
    ap.add_argument("--uninstall", action="store_true",
                    help="elevated, owner-run: stop and remove the service")
    ap.add_argument("--allow-sid", default=None, metavar="SID",
                    help="the one user SID allowed to talk to the pipe "
                         "(default: Authenticated Users, developer mode)")
    args = ap.parse_args(argv)
    if args.install:
        return cmd_install(args)
    if args.uninstall:
        return cmd_uninstall(args)
    if args.serve:
        return run_as_service()
    log("pipe %s" % PIPE_NAME)
    if not args.allow_sid:
        log("no --allow-sid given: pipe grants Authenticated Users "
            "(developer mode)")
    serve_foreground(allow_sid=args.allow_sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
