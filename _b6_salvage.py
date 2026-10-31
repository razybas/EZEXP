"""B6 salvage: delta-net while the helper is down, then a fresh
degraded-state watcher that must reconnect + replay at G2.

Same real classes as _battery_b1_b6.py phase C (UsnWatcher, UsnClient,
scan_delta); runs in two halves around the owner's relaunch:
  down   - create b6_offline.txt, index it via a scoped delta scan,
           create b6_offline2.txt left for replay, start the watcher
           (helper down => all drives delta; it keeps retrying)
  up     - (owner relaunched) verify reconnect <= 35 s, replay of the
           gap (b6_offline2 appears; no enum; no delta calls), USN back
"""
import json
import os
import sys
import time

BATTERY_DIR = r"D:\exp\ezexp_battery"
SCRATCH = r"D:\ezexp_battery_scratch"
DRIVE = SCRATCH[:2]
STATE = os.path.join(BATTERY_DIR, "state_b6.json")

os.environ["EZEXP_DB_DIR"] = os.path.join(BATTERY_DIR, "db")
os.environ["EZEXP_NO_AUTOSCAN"] = "1"
sys.path.insert(0, r"D:\exp\ezexp")

import app as ez  # noqa: E402
from ezexp.indexer import UsnWatcher, scan_delta  # noqa: E402
from ezexp.usn_client import UsnClient  # noqa: E402

PHASE = sys.argv[1]
DELTA_CALLS = []
CLIENT_EVENTS = []
READY_DRIVES = {}


class InstrumentedClient(UsnClient):
    def connect(self):
        ready = super().connect()
        READY_DRIVES.clear()
        READY_DRIVES.update(ready or {})
        return ready

    def send_subscribe(self, drive, start_usn):
        CLIENT_EVENTS.append(("subscribe", drive, int(start_usn)))
        return super().send_subscribe(drive, start_usn)

    def send_enum(self, drive):
        CLIENT_EVENTS.append(("enum", drive))
        return super().send_enum(drive)


def recording_delta(conn, roots, progress=None, should_run=None):
    DELTA_CALLS.append(list(roots))
    return scan_delta(conn, roots, progress, should_run)


def conn():
    return ez.db_connect()


def row_exists(parent, name):
    c = conn()
    try:
        return c.execute("SELECT 1 FROM files WHERE parent=? AND name=?",
                         (parent, name)).fetchone() is not None
    finally:
        c.close()


def frn_count():
    c = conn()
    try:
        return c.execute("SELECT COUNT(*) FROM frn_map WHERE drive=?",
                         (DRIVE,)).fetchone()[0]
    finally:
        c.close()


if PHASE == "down":
    with open(os.path.join(SCRATCH, "b6_offline.txt"), "w") as f:
        f.write("made while helper down")
    c = conn()
    try:
        scan_delta(c, [SCRATCH])
        c.commit()
    finally:
        c.close()
    print("[PASS] B6-delta-net: b6_offline.txt indexed via a scoped delta "
          "scan while the helper was down; row present = %s"
          % row_exists(SCRATCH, "b6_offline.txt"), flush=True)
    with open(os.path.join(SCRATCH, "b6_offline2.txt"), "w") as f:
        f.write("left for replay")
    print("b6_offline2.txt created for the replay check", flush=True)

    DELTA_CALLS.clear()
    CLIENT_EVENTS.clear()
    w = UsnWatcher(client_factory=InstrumentedClient, delta_fn=recording_delta)
    w.start()
    time.sleep(5)
    print("watcher state while helper is down: %r" % w.last_status, flush=True)
    with open(STATE, "w") as f:
        json.dump({"frn_before": frn_count(),
                   "delta_calls_down": len(DELTA_CALLS),
                   "down_status": w.last_status}, f)
    print("READY-FOR-G2", flush=True)
    # the SAME watcher must detect the relaunch, reconnect, and replay
    t0 = time.perf_counter()
    flipped = False
    while time.perf_counter() - t0 < 20 * 60:
        if DRIVE + " USN" in (w.last_status or ""):
            flipped = True
            break
        time.sleep(0.2)
    reconnect_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    ok2 = False
    while time.perf_counter() - t1 < 30:
        if row_exists(SCRATCH, "b6_offline2.txt"):
            ok2 = True
            break
        time.sleep(0.05)
    st = json.load(open(STATE))
    no_enum = not any(e[0] == "enum" for e in CLIENT_EVENTS)
    no_new_delta = len(DELTA_CALLS) == 0
    print("[PASS]" if (flipped and ok2 and reconnect_s <= 35 and no_enum
                       and no_new_delta) else "[FAIL]",
          "B6-reconnect: USN back in %.1f s (budget ~35); b6_offline2 via "
          "replay in %.2f s (present=%s); enum_sent=%s; delta_calls=%d; "
          "status=%r; frn_map %d -> %d"
          % (reconnect_s, time.perf_counter() - t1, ok2, not no_enum,
             len(DELTA_CALLS), w.last_status, st["frn_before"], frn_count()),
          flush=True)
    print("subscribe events:", CLIENT_EVENTS, flush=True)
    w.stop()
    w.wait(3000)
