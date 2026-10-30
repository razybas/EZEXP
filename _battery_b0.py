"""Battery B0: unelevated helper in --foreground; the app-side session
must connect, see access_denied for the NTFS drives, stay on delta, and
not crash. Test index via EZEXP_DB_DIR."""
import os
import sys
import time
import tempfile

tmp = tempfile.mkdtemp(prefix="ezexp_b0_")
os.environ["EZEXP_DB_DIR"] = os.path.join(tmp, "db")
os.environ["EZEXP_NO_AUTOSCAN"] = "1"
sys.path.insert(0, r"D:\exp\ezexp")

import app as ez
from ezexp import indexer as idxmod

t0 = time.perf_counter()
cli = None
try:
    from ezexp.usn_client import UsnClient
    cli = UsnClient()
    ready = cli.connect()
    print("B0 connect: OK in %.2f s" % (time.perf_counter() - t0))
    print("B0 ready (verbatim): %r" % (ready,))
except Exception as exc:
    print("B0 connect: FAILED: %r" % (exc,))
    sys.exit(1)
finally:
    if cli is not None:
        cli.close()

# full watcher session: real client factory, test-index connection
w = idxmod.UsnWatcher()
w.start()
status = None
end = time.time() + 20
while time.time() < end:
    if w.last_status:
        status = w.last_status
        break
    time.sleep(0.2)
print("B0 watcher status (verbatim): %r after %.2f s" % (
    status, time.perf_counter() - t0))
time.sleep(2)
w.stop()
w.wait(5000)
ok = status is not None and "delta" in status and "access_denied" not in status
print("B0 watcher stopped cleanly: %s" % (not w.isRunning()))
print("B0 RESULT: %s" % ("PASS" if ok and not w.isRunning() else "FAIL"))
