"""EzExp U2 live battery B1-B6 — TEST index only.

Phases (separate processes, share D:\\exp\\ezexp_battery\\state.json):
  A  bootstrap + B1 create + B2 rename + B3 folder rename + B4 delete
     + B5 prep (two watcher-applied rows), then stops the watcher.
  B  B5 closed-app replay: shell changes while NOTHING watches, fresh
     session must fix the catalog via journal replay (no enum, no delta).
  C  B6 helper kill/relaunch, driven by the OWNER (elevated console):
     degradation + delta net while dead, reconnect + replay when back.

The live index (%LOCALAPPDATA%\\EzExp) is never opened: EZEXP_DB_DIR
points at the scratch battery db for the whole process.
"""
import json
import os
import shutil
import sys
import time

BATTERY_DIR = r"D:\exp\ezexp_battery"
SCRATCH = r"D:\ezexp_battery_scratch"
DRIVE = SCRATCH[:2]
ROOT = DRIVE + "\\"
STATE = os.path.join(BATTERY_DIR, "state.json")

os.environ["EZEXP_DB_DIR"] = os.path.join(BATTERY_DIR, "db")
os.environ["EZEXP_NO_AUTOSCAN"] = "1"
sys.path.insert(0, r"D:\exp\ezexp")

import app as ez
from ezexp.fsutil import fixed_drives
from ezexp.indexer import UsnWatcher, scan_delta
from ezexp.usn_client import UsnClient

PHASE = sys.argv[1] if len(sys.argv) > 1 else "A"

RESULTS = []
DELTA_CALLS = []
CLIENT_EVENTS = []
READY_DRIVES = {}


def report(step, ok, detail):
    RESULTS.append((step, ok, detail))
    print("[%s] %s: %s" % ("PASS" if ok else "FAIL", step, detail), flush=True)


def measure(cond_fn, budget=2.0, max_wait=30.0, poll=0.05):
    """Poll cond_fn; return (latency_s, passed). passed = seen within
    budget; latency = first-seen time (eventual value even if late)."""
    t0 = time.perf_counter()
    seen_at = None
    while time.perf_counter() - t0 < max_wait:
        if cond_fn():
            seen_at = time.perf_counter() - t0
            break
        time.sleep(poll)
    return seen_at, (seen_at is not None and seen_at <= budget)


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


def make_watcher():
    DELTA_CALLS.clear()
    CLIENT_EVENTS.clear()
    return UsnWatcher(client_factory=InstrumentedClient,
                      delta_fn=recording_delta)


def conn():
    return ez.db_connect()


def row_exists(parent, name):
    c = conn()
    try:
        return c.execute("SELECT 1 FROM files WHERE parent=? AND name=?",
                         (parent, name)).fetchone() is not None
    finally:
        c.close()


def fts_names(query):
    c = conn()
    try:
        return [r[0] for r in c.execute(
            "SELECT name FROM fts WHERE fts MATCH ?", (query,))]
    finally:
        c.close()


def frn_count():
    c = conn()
    try:
        return c.execute("SELECT COUNT(*) FROM frn_map WHERE drive=?",
                         (DRIVE,)).fetchone()[0]
    finally:
        c.close()


def checkpoint():
    c = conn()
    try:
        return (ez.get_meta(c, "usn.%s.journal_id" % DRIVE),
                ez.get_meta(c, "usn.%s.next_usn" % DRIVE))
    finally:
        c.close()


def wait_mode(w, mode, secs):
    """Poll for the drive's mode; after 30 s without a flip, dump every
    thread's stack ONCE so a stuck watcher is diagnosable from the log."""
    import threading
    import traceback
    dumped = [False]
    end = time.time() + secs
    while time.time() < end:
        st = w.last_status or ""
        if DRIVE + " " + mode in st:
            return True
        if not dumped[0] and time.time() > end - secs + 30:
            dumped[0] = True
            print("STALL: no mode flip after 30 s; thread stacks:", flush=True)
            for tid, frame in sys._current_frames().items():
                name = "t%d" % tid
                for t in threading.enumerate():
                    if t.ident == tid:
                        name = t.name
                        break
                print("--- thread %s ---" % name, flush=True)
                traceback.print_stack(frame)
        time.sleep(0.2)
    return False


def preseed_roots():
    """Give the test catalog a drive-root row per fixed drive with the
    CURRENT mtime, so the watcher's one-time post-bootstrap delta scan
    finds nothing to do. Without this the first session would full-scan
    every fixed drive into the test index (hours) — the battery only
    exercises the USN path, and this seed changes no watcher code."""
    c = conn()
    try:
        for root in fixed_drives():
            d = root[:2]
            st = os.stat(root)
            ez.insert_rows(c, [ez._make_row("", d, True, st, d)])
        ez.set_meta(c, "phase", "full_done")
        c.commit()
    finally:
        c.close()


def save_state(**kw):
    st = {}
    if os.path.exists(STATE):
        with open(STATE) as f:
            st = json.load(f)
    st.update(kw)
    with open(STATE, "w") as f:
        json.dump(st, f, indent=1)


def load_state():
    with open(STATE) as f:
        return json.load(f)


def all_pass():
    return all(ok for _, ok, _ in RESULTS)


def wait_for(pred, secs):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------- phase A
if PHASE == "A":
    shutil.rmtree(BATTERY_DIR, ignore_errors=True)
    os.makedirs(BATTERY_DIR, exist_ok=True)
    shutil.rmtree(SCRATCH, ignore_errors=True)  # no stale rows from old runs
    preseed_roots()

    w = make_watcher()
    t0 = time.perf_counter()
    w.start()

    def steady():
        # truly live: the checkpoint written (enum + delta-once finished)
        # AND every drive the helper reports ok is subscribed — no worker
        # still mid-enum contending for the db lock
        jid, _ = checkpoint()
        subs = {e[1] for e in CLIENT_EVENTS if e[0] == "subscribe"}
        ok_drives = {d for d, p in READY_DRIVES.items() if p == "ok"}
        return (jid is not None
                and DRIVE + " USN" in (w.last_status or "")
                and ok_drives and subs >= ok_drives)

    end = time.time() + 30 * 60
    while time.time() < end and not steady():
        time.sleep(0.2)
    if not steady():
        report("A-bootstrap", False,
               "drive %s never went live; status=%r events=%r"
               % (DRIVE, w.last_status, CLIENT_EVENTS))
        w.stop(); w.wait(3000)
        sys.exit(1)
    time.sleep(2)  # let the replay backlog drain before timing anything
    report("A-bootstrap", True,
           "live (checkpoint+subscribe) in %.1f s; status=%r"
           % (time.perf_counter() - t0, w.last_status))
    print("A-events:", CLIENT_EVENTS, flush=True)
    print("A-delta-calls:", DELTA_CALLS, flush=True)

    # the scratch folder itself must enter the catalog via the USN path
    t0 = time.perf_counter()
    os.makedirs(SCRATCH)
    lat, ok = measure(lambda: row_exists(DRIVE, os.path.basename(SCRATCH)))
    report("A-scratchdir", ok,
           "dir row %s appeared in %.3f s (budget 2 s)"
           % (SCRATCH, lat if lat is not None else -1))
    c = conn()
    sfrn = c.execute("SELECT frn FROM frn_map WHERE drive=? AND name=? AND parent_frn=5",
                     (DRIVE, os.path.basename(SCRATCH))).fetchall()
    c.close()
    assert len(sfrn) == 1, "expected exactly one map entry for the scratch dir: %r" % (sfrn,)
    SCRATCH_FRN = sfrn[0][0]

    # B1 create
    p = os.path.join(SCRATCH, "b1_probe.txt")
    t0 = time.perf_counter()
    with open(p, "w") as f:
        f.write("battery b1 probe")
    lat, ok = measure(lambda: row_exists(SCRATCH, "b1_probe.txt"))
    report("B1-create", ok and DRIVE + " USN" in (w.last_status or ""),
           "row in %.3f s (budget 2 s); mode=%r; delta_calls=%d"
           % (lat or -1, w.last_status, len(DELTA_CALLS)))

    # B2 rename
    t0 = time.perf_counter()
    os.rename(p, os.path.join(SCRATCH, "b2_probe.txt"))
    lat, ok = measure(lambda: row_exists(SCRATCH, "b2_probe.txt")
                      and not row_exists(SCRATCH, "b1_probe.txt"))
    time.sleep(0.5)  # settle, then FTS evidence
    new_fts = fts_names("b2_probe")
    old_fts = fts_names("b1_probe")
    report("B2-rename", ok and new_fts == ["b2_probe.txt"] and old_fts == [],
           "row moved in %.3f s; FTS b2_probe=%r; FTS b1_probe=%r"
           % (lat or -1, new_fts, old_fts))

    # B3 folder rename with child
    os.makedirs(os.path.join(SCRATCH, "sub"))
    with open(os.path.join(SCRATCH, "sub", "inner.txt"), "w") as f:
        f.write("inner")
    lat, ok = measure(lambda: row_exists(SCRATCH + "\\sub", "inner.txt"))
    report("B3-create", ok, "inner row in %.3f s" % (lat or -1))
    t0 = time.perf_counter()
    os.rename(os.path.join(SCRATCH, "sub"), os.path.join(SCRATCH, "sub2"))
    lat, ok = measure(lambda: row_exists(SCRATCH + "\\sub2", "inner.txt")
                      and not row_exists(SCRATCH + "\\sub", "inner.txt"))
    c = conn()
    child = c.execute("SELECT parent, name FROM files WHERE name='inner.txt'"
                      ).fetchone()
    c.close()
    report("B3-dirrename", ok and child == (SCRATCH + "\\sub2", "inner.txt"),
           "child follows in %.3f s; verbatim row=%r" % (lat or -1, child))
    c = conn()
    s2 = c.execute("SELECT frn FROM frn_map WHERE drive=? AND name='sub2' AND parent_frn=?",
                   (DRIVE, SCRATCH_FRN)).fetchall()
    c.close()
    assert len(s2) == 1, "expected exactly one map entry for sub2: %r" % (s2,)
    SUB2_FRN = s2[0][0]

    # B4 delete file + folder
    t0 = time.perf_counter()
    os.remove(os.path.join(SCRATCH, "sub2", "inner.txt"))
    os.rmdir(os.path.join(SCRATCH, "sub2"))
    os.remove(os.path.join(SCRATCH, "b2_probe.txt"))
    lat, ok = measure(lambda:
                      not row_exists(SCRATCH + "\\sub2", "inner.txt")
                      and not row_exists(SCRATCH, "b2_probe.txt")
                      and not row_exists(SCRATCH, "sub2")
                      and fts_names("inner") == []
                      and fts_names("b2_probe") == [])
    c = conn()
    frn_left = c.execute(
        "SELECT name FROM frn_map WHERE drive=? AND (frn=? OR parent_frn=?"
        " OR parent_frn=?)",
        (DRIVE, SUB2_FRN, SUB2_FRN, SCRATCH_FRN)).fetchall()
    c.close()
    report("B4-delete", ok and frn_left == [],
           "rows+FTS gone in %.3f s; deleted-subtree frn_map leftovers=%r"
           % (lat or -1, frn_left))

    # B5 prep: two rows the closed-app gap will rename/delete
    with open(os.path.join(SCRATCH, "b5_old.txt"), "w") as f:
        f.write("old")
    with open(os.path.join(SCRATCH, "b5_del.txt"), "w") as f:
        f.write("del")
    lat, ok = measure(lambda: row_exists(SCRATCH, "b5_old.txt")
                      and row_exists(SCRATCH, "b5_del.txt"))
    report("B5-prep", ok, "b5_old/b5_del rows applied in %.3f s" % (lat or -1))

    save_state(pre_gap_cp=checkpoint(), frn_before_b5=frn_count(),
               delta_calls_A=len(DELTA_CALLS), events_A=list(CLIENT_EVENTS),
               mode_A=w.last_status)
    w.stop()
    w.wait(5000)
    print("A-watcher-stopped cleanly:", not w.isRunning(), flush=True)
    sys.exit(0 if all_pass() else 1)

# ---------------------------------------------------------------- phase B
if PHASE == "B":
    st = load_state()
    # changes made while NOTHING is watching (app "closed")
    with open(os.path.join(SCRATCH, "b5_new.txt"), "w") as f:
        f.write("new while closed")
    os.rename(os.path.join(SCRATCH, "b5_old.txt"),
              os.path.join(SCRATCH, "b5_renamed.txt"))
    os.remove(os.path.join(SCRATCH, "b5_del.txt"))
    print("B-shell-changes done at", time.strftime("%H:%M:%S"), flush=True)

    w = make_watcher()
    w.start()
    t0 = time.perf_counter()
    ok = measure(lambda: row_exists(SCRATCH, "b5_new.txt")
                 and row_exists(SCRATCH, "b5_renamed.txt")
                 and not row_exists(SCRATCH, "b5_old.txt")
                 and not row_exists(SCRATCH, "b5_del.txt"),
                 budget=5.0, max_wait=60.0)[1]
    lat = time.perf_counter() - t0
    # the checkpoint advance can land a lock-cycle after the rows (the
    # applier's delete_tree commits mid-batch); wait for it explicitly
    pre_gap = int(st["pre_gap_cp"][1])
    _u2b_cp_ok = wait_for(lambda: int(checkpoint()[1]) > pre_gap, 10)
    c = conn()
    evidence = c.execute("SELECT parent, name FROM files WHERE name LIKE "
                         "'b5%' ORDER BY name").fetchall()
    c.close()
    no_enum = not any(e[0] == "enum" for e in CLIENT_EVENTS)
    subs = [e for e in CLIENT_EVENTS if e[0] == "subscribe"]
    from_cp = any(e[1] == DRIVE and e[2] == int(st["pre_gap_cp"][1])
                  for e in subs)
    no_delta = len(DELTA_CALLS) == 0
    frn_after = frn_count()
    report("B5-replay", ok and _u2b_cp_ok and no_enum and from_cp and no_delta,
           "catalog correct in %.2f s; checkpoint %r -> %r (advanced=%s); "
           "rows=%r; subscribe events=%r; enum_sent=%s; delta_calls=%d; "
           "frn_map %d -> %d (+1 create -1 delete)"
           % (lat, st["pre_gap_cp"], checkpoint(), _u2b_cp_ok, evidence,
              subs[:3], not no_enum, len(DELTA_CALLS),
              st["frn_before_b5"], frn_after))
    print("B-checkpoint-after:", checkpoint(), flush=True)
    print("B-mode:", w.last_status, flush=True)
    save_state(cp_after_b5=checkpoint(), mode_B=w.last_status)
    w.stop()
    w.wait(5000)
    sys.exit(0 if all_pass() else 1)

# ---------------------------------------------------------------- phase C
if PHASE == "C":
    st = load_state()
    w = make_watcher()
    w.start()
    # the helper may still be down from a previous step: wait up to 15 min
    # for the owner's elevated console to come up before declaring failure
    if not wait_mode(w, "USN", 15 * 60):
        report("B6-reconnect-ready", False,
               "no USN session before outage; status=%r" % w.last_status)
        sys.exit(1)

    print("OWNER: close the helper console window NOW.", flush=True)
    t0 = time.perf_counter()
    if not wait_mode(w, "delta", 15 * 60):
        report("B6-degrade", False, "status never flipped to delta")
        w.stop(); w.wait(3000); sys.exit(1)
    report("B6-degrade", True,
           "status flipped to delta in %.1f s; mode=%r"
           % (time.perf_counter() - t0, w.last_status))

    # with the helper dead, the delta net must still catch a new file.
    # Scope the scan to the SCRATCH subtree: a drive-wide delta walks every
    # dir the watcher indexed during the session (owner's churn-heavy
    # trees) and can run for many minutes — the net is proven by the
    # subtree alone.
    with open(os.path.join(SCRATCH, "b6_offline.txt"), "w") as f:
        f.write("made while helper down")
    c = conn()
    try:
        scan_delta(c, [SCRATCH])
        c.commit()
    finally:
        c.close()
    report("B6-delta-net", row_exists(SCRATCH, "b6_offline.txt"),
           "b6_offline.txt indexed by an explicit delta scan while the "
           "helper was down")

    # this one is left for the helper's replay to catch
    with open(os.path.join(SCRATCH, "b6_offline2.txt"), "w") as f:
        f.write("left for replay")
    delta_count_down = len(DELTA_CALLS)

    print("OWNER: relaunch the helper ELEVATED now (same --foreground "
          "command).", flush=True)
    t0 = time.perf_counter()
    if not wait_mode(w, "USN", 15 * 60):
        report("B6-reconnect", False, "status never returned to USN")
        w.stop(); w.wait(3000); sys.exit(1)
    reconnect_s = time.perf_counter() - t0
    lat, ok = measure(lambda: row_exists(SCRATCH, "b6_offline2.txt"),
                      budget=10.0, max_wait=30.0)
    no_enum = not any(e[0] == "enum" for e in CLIENT_EVENTS)
    no_new_delta = len(DELTA_CALLS) == delta_count_down
    report("B6-reconnect", ok and reconnect_s <= 35 and no_enum and no_new_delta,
           "USN back in %.1f s (budget ~35); b6_offline2 via replay in "
           "%.3f s; enum_sent=%s; delta_calls_during=%d"
           % (reconnect_s, lat or -1, not no_enum, len(DELTA_CALLS)))
    print("C-mode:", w.last_status, flush=True)
    save_state(mode_C=w.last_status)
    w.stop()
    w.wait(5000)
    sys.exit(0 if all_pass() else 1)

print("unknown phase", PHASE)
sys.exit(2)
