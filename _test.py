"""Smoke tests for EzExp: indexing, search, delta updates, and GUI creation."""
import os
import sys
import time
import tempfile

tmp = tempfile.mkdtemp(prefix="ezexp_test_")
os.environ["EZEXP_DB_DIR"] = os.path.join(tmp, "db")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["EZEXP_NO_AUTOSCAN"] = "1"
sys.path.insert(0, r"D:\exp\ezexp")

import app as ez  # noqa: E402

# --- fake "drive" tree ------------------------------------------------------
data = os.path.join(tmp, "data")
docs = os.path.join(data, "Documents")
pics = os.path.join(data, "Pictures")
code = os.path.join(data, "code")
for d in (docs, pics, code):
    os.makedirs(d)
with open(os.path.join(docs, "meeting-notes.txt"), "w") as f:
    f.write("hello")
with open(os.path.join(docs, "Q3 Financial Report.pdf"), "w") as f:
    f.write("x")
with open(os.path.join(pics, "vacation-photo.jpg"), "w") as f:
    f.write("x")
with open(os.path.join(code, "main.py"), "w") as f:
    f.write("print(1)")
with open(os.path.join(data, "render_final.mp4"), "w") as f:
    f.write("x" * 2048)

conn = ez.db_connect()
assert ez.FTS_OK, "FTS5 trigram tokenizer should be available"
ez.scan_full(conn, [data], lambda p, c: None)
assert ez.get_meta(conn, "phase") == "full_done"

# --- a "fresh open" must NOT re-read the whole tree --------------------------
# Routing check: phase is set, so the launch path takes the incremental branch.
assert ez.get_meta(conn, "phase") == "full_done"
# With nothing changed on disk, the delta scan must re-read nothing: progress
# fires only for re-read dirs, and only the scan root itself can legitimately
# appear (test roots have no DB row of their own); every count must stay 0.
t0 = time.perf_counter()
emissions = []
ez.scan_delta(conn, [data], lambda path, count: emissions.append((path, count)))
delta_secs = time.perf_counter() - t0
assert all(c == 0 for _, c in emissions), emissions
assert len(emissions) <= 1, emissions
# and a delta pass must be far cheaper than the full scan that built the index
t0 = time.perf_counter()
ez.scan_full(conn, [data], lambda p, c: None)
full_secs = time.perf_counter() - t0
assert delta_secs < full_secs, (delta_secs, full_secs)


def names(rows):
    return sorted(r["name"] for r in rows)


rows, _ = ez.do_search(conn, "meeting")
assert names(rows) == ["meeting-notes.txt"], rows
rows, _ = ez.do_search(conn, "MEETING")
assert len(rows) == 1, "case-insensitive search failed"
rows, _ = ez.do_search(conn, "photo")
assert names(rows) == ["vacation-photo.jpg"], rows
rows, _ = ez.do_search(conn, "cemo")
assert len(rows) == 0, "mid-word trigram should not match 'meeting'" if False else None

rows, _ = ez.do_search(conn, "")          # browse = everything incl. folders
assert len(rows) == 8, rows
rows, _ = ez.do_search(conn, "", cat="img")
assert names(rows) == ["vacation-photo.jpg"], rows

# browsing a place shows direct children: folders first, then name-sorted
rows, _ = ez.do_search(conn, "", place=docs)
assert [r["name"] for r in rows] == ["meeting-notes.txt", "Q3 Financial Report.pdf"], rows
rows, _ = ez.do_search(conn, "", place=data)
assert [r["name"] for r in rows] == [
    "code", "Documents", "Pictures", "render_final.mp4"], rows

# searching within a place is still recursive
rows, _ = ez.do_search(conn, "notes", place=data)
assert names(rows) == ["meeting-notes.txt"], rows

rows, _ = ez.do_search(conn, "mai")       # prefix beats substring
assert rows[0]["name"] == "main.py", rows
rows, _ = ez.do_search(conn, "ai")        # 2 chars → LIKE path
assert names(rows) == ["main.py"], rows

# --- delta: additions, deletions -------------------------------------------
time.sleep(0.05)
with open(os.path.join(docs, "brand-new-file.mp4"), "w") as f:
    f.write("x")
os.remove(os.path.join(pics, "vacation-photo.jpg"))
os.utime(docs)
os.utime(pics)
ez.scan_delta(conn, [data], lambda p, c: None)

rows, _ = ez.do_search(conn, "brand-new")
assert names(rows) == ["brand-new-file.mp4"], rows
rows, _ = ez.do_search(conn, "vacation")
assert rows == [], rows
rows, _ = ez.do_search(conn, "meeting")   # old files survive delta
assert len(rows) == 1, rows

# rename propagation through the DB helpers
os.rename(os.path.join(docs, "brand-new-file.mp4"), os.path.join(docs, "renamed-clip.mp4"))
os.utime(docs)
ez.scan_delta(conn, [data], lambda p, c: None)
rows, _ = ez.do_search(conn, "renamed-clip")
assert names(rows) == ["renamed-clip.mp4"], rows
rows, _ = ez.do_search(conn, "brand-new")
assert rows == [], rows

total = ez.update_total(conn)
assert total == 8, total

# --- Obsidian vault settings -------------------------------------------------
vault = os.path.join(tmp, "vault")
os.makedirs(vault)
with open(os.path.join(vault, "note.md"), "w") as f:
    f.write("# Hello vault")
ez.set_vaults([vault])
got = ez.get_vaults()
assert got and got[0].lower() == os.path.normpath(vault).lower(), got
ez.set_vaults([vault])  # keep it for the GUI section below

# --- vault graph building ------------------------------------------------------
gdir = os.path.join(tmp, "gvault")
os.makedirs(os.path.join(gdir, "sub"))
notes = {
    "alpha.md": "Links to [[beta]] and [[gamma|alias]] and [[missing one]]",
    "beta.md": "[[alpha]] and [[alpha#sec]] again [[alpha]]",
    os.path.join("sub", "gamma.md"): "back to [[alpha]], self [[gamma]] skipped",
}
for rel, content in notes.items():
    with open(os.path.join(gdir, rel), "w", encoding="utf-8") as f:
        f.write(content)
ez.scan_full(conn, [data, gdir], lambda p, c: None)
ez.set_vaults([gdir])
assert ez.parse_wikilinks("[[a|x]] [[b#h]] [[c]] broken [[") == ["a", "b", "c"]
assert ez.active_vault_for(os.path.join(gdir, "sub")) == os.path.normpath(gdir)
assert ez.active_vault_for("C:\\nowhere") is None
g = ez.build_vault_graph(conn, gdir)
assert sorted(n["name"] for n in g["nodes"]) == ["alpha", "beta", "gamma"], g["nodes"]
assert len(g["edges"]) == 2, g["edges"]
degs = {n["name"]: n["degree"] for n in g["nodes"]}
assert degs == {"alpha": 2, "beta": 1, "gamma": 1}, degs

# --- GUI smoke test ----------------------------------------------------------
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402

qapp = QApplication.instance() or QApplication([])
worker = ez.SearchWorker()
worker.start()
win = ez.MainWindow(worker)
win.resize(1180, 720)
win.show()
spot = ez.Spotlight(worker)


def wait_results(msec=5000):
    loop = QEventLoop()
    QTimer.singleShot(msec, loop.quit)
    loop.exec()


captured = {}
worker.have.connect(lambda seq, q, rows_, note: captured.update({"seq": seq, "rows": rows_}))

win.titlebar.search.edit.setText("meeting")
win.refresh_view()
wait_results()
assert captured.get("rows") and names(captured["rows"]) == ["meeting-notes.txt"], captured
assert win.tree.topLevelItemCount() == 1, win.tree.topLevelItemCount()
assert "meeting-notes" in win.details.name_label.text(), win.details.name_label.text()

# text preview for .txt files loads asynchronously into the details pane
wait_results(1500)
assert "hello" in win.details.text_preview.text(), win.details.text_preview.text()

# vault shows up in the sidebar
assert any(getattr(b, "_vault", False) for b in win._side_buttons), "vault button missing"

# double-clicking a folder drills into it inside the app; Back returns
rows, _ = ez.do_search(conn, "", place=data)
folder_row = [r for r in rows if r["is_dir"] and r["name"] == "Documents"][0]
win.open_in_app(folder_row)
wait_results(300)
assert win.current_place == os.path.normpath(folder_row["parent"] + "\\Documents").rstrip("\\"), win.current_place
assert win.titlebar.title.text() == "Documents"
assert win.tree.topLevelItemCount() == 3, "drilled folder should list 3 children"
assert win.titlebar.btn_back.isEnabled()
win.go_back()
wait_results(300)
assert win.current_place is None, win.current_place

# Graph tab: select the vault, switch to graph mode, interact with nodes
for b in win._side_buttons:
    if getattr(b, "_vault", False):
        b.click()
        break
wait_results(500)
win.enter_graph_mode()
wait_results(4000)
assert win.view_mode == "graph"
assert len(win.graph_view.nodes) == 3, len(win.graph_view.nodes)
alpha = [n for n in win.graph_view.nodes if n.label == "alpha"][0]
win.graph_view.select_node(alpha)
wait_results(300)
assert "alpha" in win.details.name_label.text().lower()
win.enter_list_mode("all")
wait_results(300)
assert win.view_mode == "list" and not win.graph_view.isVisible()

spot.edit.setText("notes")
wait_results()
assert spot.tree.topLevelItemCount() >= 2, "spotlight should have header + top hit"
row = spot.current_row()
assert row and row["name"] == "meeting-notes.txt", row

win.titlebar.search.edit.setText("zzzznothing")
wait_results()
assert win.tree.topLevelItemCount() == 0
assert not win.empty_label.isHidden()

worker.stop()
worker.wait(3000)
print("ALL TESTS PASSED")
