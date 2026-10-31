"""B7 post-install smoke: fresh UNELEVATED session against the SERVICE's
pipe (EzExpUsn, LocalSystem), test index only. Ready must show the NTFS
drives ok and one scratch create must land in the test catalog <= 2 s.
"""
import os
import sys
import time

BATTERY_DIR = r"D:\exp\ezexp_battery"
SCRATCH = r"D:\ezexp_battery_scratch"
DRIVE = SCRATCH[:2]
os.environ["EZEXP_DB_DIR"] = os.path.join(BATTERY_DIR, "db")
os.environ["EZEXP_NO_AUTOSCAN"] = "1"
sys.path.insert(0, r"D:\exp\ezexp")

import app as ez  # noqa: E402
from ezexp.indexer import UsnWatcher  # noqa: E402
from ezexp.usn_client import UsnClient  # noqa: E402

t0 = time.perf_counter()
c = UsnClient()
ready = c.connect()
print("B7 ready (verbatim): %r in %.2f s" % (ready, time.perf_counter() - t0),
      flush=True)
c.close()
ok_ready = ready.get(DRIVE) == "ok"
print("B7 ready verdict: %s" % ("PASS" if ok_ready else "FAIL"), flush=True)

DELTA_CALLS = []


def recording_delta(conn, roots, progress=None, should_run=None):
    DELTA_CALLS.append(list(roots))
    return ez.scan_delta(conn, roots, progress, should_run)


w = UsnWatcher(delta_fn=recording_delta)
w.start()
end = time.time() + 120
while time.time() < end:
    if DRIVE + " USN" in (w.last_status or ""):
        break
    time.sleep(0.2)
print("B7 watcher: %r; delta_calls=%d" % (w.last_status, len(DELTA_CALLS)),
      flush=True)

p = os.path.join(SCRATCH, "b7_smoke.txt")
t1 = time.perf_counter()
with open(p, "w") as f:
    f.write("service smoke")


def present():
    cc = ez.db_connect()
    try:
        return cc.execute("SELECT 1 FROM files WHERE parent=? AND name=?",
                          (SCRATCH, "b7_smoke.txt")).fetchone() is not None
    finally:
        cc.close()


seen = None
while time.perf_counter() - t1 < 30:
    if present():
        seen = time.perf_counter() - t1
        break
    time.sleep(0.02)
print("B7 create: b7_smoke.txt row in %s s (budget 2 s); delta_calls=%d"
      % ("%.3f" % seen if seen is not None else "NEVER", len(DELTA_CALLS)),
      flush=True)
w.stop()
w.wait(3000)
# spec terms: ready shows the NTFS drives ok, and the scratch create lands
# <= 2 s. Delta calls during the session are legitimate (a stale checkpoint
# correctly re-bootstraps with enum + one delta scan before going live).
ok = ok_ready and seen is not None and seen <= 2.0
print("B7 RESULT: %s" % ("PASS" if ok else "FAIL"), flush=True)
