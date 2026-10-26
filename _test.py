"""Smoke tests for EzExp: indexing, search, delta updates, and GUI creation."""
import os
import sys
import time
import tempfile
import shutil
import subprocess
import threading

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
ez.ensure_indexes(conn)
assert ez.FTS_OK, "FTS5 trigram tokenizer should be available"
plan = " ".join(str(p) for p in conn.execute(
    "EXPLAIN QUERY PLAN SELECT * FROM files WHERE parent = ?"
    " COLLATE NOCASE", ("x",)).fetchall())
assert "idx_files_parent_nc" in plan, plan
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

_fresh = ez.sqlite3.connect(os.path.join(tmp, "fresh.db"))
ez._ensure_schema(_fresh)
assert _fresh.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
    " AND name='idx_files_parent_nc'").fetchone()[0] == 0
ez.ensure_indexes(_fresh)
assert _fresh.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
    " AND name='idx_files_parent_nc'").fetchone()[0] == 1
_fresh.close()

_attempts = {"n": 0}
_orig_schema = ez._ensure_schema
def _flaky_schema(conn):
    _attempts["n"] += 1
    if _attempts["n"] < 3:
        raise ez.sqlite3.OperationalError("database is locked")
    return _orig_schema(conn)
ez._ensure_schema = _flaky_schema
try:
    _c = ez.db_connect()
    _c.close()
finally:
    ez._ensure_schema = _orig_schema
assert _attempts["n"] == 3

_orig_app_dir = ez.APP_DIR
ez.APP_DIR = data
try:
    # crash log hook appends a traceback to APP_DIR/error.log
    log = os.path.join(data, "error.log")
    ez.install_crash_log()
    try:
        raise ValueError("ezexp-test-marker")
    except ValueError:
        ez.sys.excepthook(*ez.sys.exc_info())
    with open(log, encoding="utf-8") as fh:
        assert "ezexp-test-marker" in fh.read()
    # single instance: first acquire holds, second is refused
    fresh = os.path.join(tempfile.mkdtemp(), "fresh")
    ez.APP_DIR = fresh
    assert not os.path.exists(fresh)
    lk = ez.acquire_single_instance()
    assert lk is not None
    assert ez.acquire_single_instance() is None
    lk.unlock()
    ez._single_instance_lock = None
    lk = ez.acquire_single_instance()
    assert lk is not None
    lk.unlock()
    ez._single_instance_lock = None
finally:
    if os.path.exists(log):
        os.remove(log)
    ez.APP_DIR = _orig_app_dir
assert ez.APP_VERSION.count(".") == 2


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

# --- list sorting -----------------------------------------------------------
sort_sample = [
    {"name": "bdir", "is_dir": 1, "size": None, "mtime": 50},
    {"name": "big.txt", "is_dir": 0, "size": 900, "mtime": 10},
    {"name": "afile.txt", "is_dir": 0, "size": 5, "mtime": 99},
    {"name": "mid.txt", "is_dir": 0, "size": 100, "mtime": 60},
]
assert [r["name"] for r in ez.sort_rows(sort_sample, "name")] == \
    ["bdir", "afile.txt", "big.txt", "mid.txt"]
assert [r["name"] for r in ez.sort_rows(sort_sample, "newest")] == \
    ["afile.txt", "mid.txt", "bdir", "big.txt"]
assert [r["name"] for r in ez.sort_rows(sort_sample, "largest")] == \
    ["big.txt", "mid.txt", "afile.txt", "bdir"]
assert [r["name"] for r in ez.sort_rows(sort_sample, "smallest")] == \
    ["afile.txt", "mid.txt", "big.txt", "bdir"]
assert ez.sort_rows(sort_sample, "default") == sort_sample

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

# --- delta: deleting a FOLDER removes its contents from the index too -------
doomed = os.path.join(data, "doomed")
os.makedirs(doomed)
with open(os.path.join(doomed, "ghost-file.txt"), "w") as f:
    f.write("x")
os.utime(data)
ez.scan_delta(conn, [data], lambda p, c: None)
rows, _ = ez.do_search(conn, "ghost-file")
assert names(rows) == ["ghost-file.txt"], rows
shutil.rmtree(doomed)
os.utime(data)
ez.scan_delta(conn, [data], lambda p, c: None)
rows, _ = ez.do_search(conn, "ghost-file")
assert rows == [], rows          # contents must not survive as orphans
rows, _ = ez.do_search(conn, "doomed")
assert rows == [], rows

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

# one-time link analysis: parses every note into note_links, then never again
assert ez.pending_link_count(conn, [gdir]) == 3
analyzed, remaining = ez.analyze_pending_links(conn, [gdir], time_budget=60)
assert (analyzed, remaining) == (3, 0), (analyzed, remaining)
assert ez.pending_link_count(conn, [gdir]) == 0
g = ez.build_vault_graph(conn, gdir)
assert g["mode"] == "vault"
labels = {n["label"] for n in g["nodes"]}
assert {"gvault", "sub", "alpha", "beta"} <= labels, labels
le = [(a.lower(), b.lower()) for a, b in g["link_edges"]]
assert len(le) == 2, le
assert all(a.endswith("alpha.md") for a, b in le), le
assert sorted(b.rsplit("\\", 1)[-1] for a, b in le) == ["beta.md", "gamma.md"]
assert g["analyzed"] == 3

# incremental: a new note and an edited note become pending; unchanged never re-read
with open(os.path.join(gdir, "empty.md"), "w") as f:
    f.write("no links in this note")
os.utime(os.path.join(gdir, "alpha.md"))  # content edit -> stale links
ez.scan_delta(conn, [data, gdir], lambda p, c: None)
assert ez.pending_link_count(conn, [gdir]) == 2, ez.pending_link_count(conn, [gdir])
analyzed, remaining = ez.analyze_pending_links(conn, [gdir], time_budget=60)
assert (analyzed, remaining) == (2, 0)
g = ez.build_vault_graph(conn, gdir)
labels = {n["label"] for n in g["nodes"]}
assert "empty" in labels and "gamma" not in labels, labels
assert len(g["link_edges"]) == 2, g["link_edges"]
assert g["analyzed"] == 4

# --- structure graphs (no file reads) -----------------------------------------
st = ez.build_structure_graph(conn, place=data)
labels = {n["label"] for n in st["nodes"]}
assert "data" in labels and "Documents" in labels and "code" in labels, labels
assert "render_final.mp4" in labels, labels            # direct file of the root
assert "meeting-notes.txt" not in labels, labels        # lives inside Documents
kinds = {n["label"]: n["kind"] for n in st["nodes"]}
assert kinds["data"] == "folder" and kinds["render_final.mp4"] == "file"
# root + 3 subdirs + 1 direct file, each connected to its parent
assert len(st["nodes"]) == 5, st["nodes"]
assert len(st["edges"]) == 4, st["edges"]
assert all(n["sub"] and "items" in n["sub"] for n in st["nodes"] if n["kind"] != "file")

# drilled one level deeper, the folder's own files appear
st_docs = ez.build_structure_graph(conn, place=os.path.join(data, "Documents"))
labels_d = {n["label"] for n in st_docs["nodes"]}
assert {"Documents", "meeting-notes.txt", "Q3 Financial Report.pdf",
        "renamed-clip.mp4"} <= labels_d, labels_d

st2 = ez.build_structure_graph(conn, None, roots=["C:"])
assert st2["nodes"] and st2["nodes"][0]["kind"] == "drive", st2["nodes"][:1]
assert "(C:)" in st2["nodes"][0]["label"], st2["nodes"][0]["label"]
# depth-capped: only the drive root is close enough to the temp data to show
assert all(n["kind"] in ("drive", "folder") or n["parent_path"] == "C:" for n in st2["nodes"])

# multi-drive view gets a single virtual "This PC" root connecting the drives
st3 = ez.build_structure_graph(conn, None, roots=["C:", "D:"])
assert st3["nodes"][0]["label"] == "This PC" and st3["nodes"][0]["kind"] == "drive"
lab = {n["label"] for n in st3["nodes"]}
assert any("(C:)" in l for l in lab) and any("(D:)" in l for l in lab), lab
drive_edges = [(a, b) for a, b in st3["edges"] if st3["nodes"][a]["label"] == "This PC"]
assert len(drive_edges) == 2, drive_edges

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
img = win.tree.viewport().grab().toImage()
px = img.pixelColor(200, 23)   # name-column background of selected row
assert px.blue() > px.red() + 30, px   # blue selection, not white

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

# Graph tab: select the vault, switch to graph mode; links fill in after
# the background analyzer runs (graph itself renders instantly from the index)
for b in win._side_buttons:
    if getattr(b, "_vault", False):
        b.click()
        break
wait_results(500)
win.enter_graph_mode()
wait_results(2000)
assert win.view_mode == "graph"
assert len(win.graph_view._items) == 5, list(win.graph_view._items)
assert len(win.graph_view._link_lines) == 2, len(win.graph_view._link_lines)
alpha = [it for it in win.graph_view._items.values() if it.label == "alpha"][0]
win.graph_view.select_node(alpha)
wait_results(300)
assert "alpha" in win.details.name_label.text().lower()
# burst the sub bubble: gamma appears, its bubble link becomes a direct note link
sub_idx = [i for i, it in win.graph_view._items.items() if it.label == "sub"][0]
assert win.graph_view._items[sub_idx].can_expand
win.graph_view.expand_node(sub_idx)
wait_results(300)
assert len(win.graph_view._items) == 6, len(win.graph_view._items)
assert len(win.graph_view._link_lines) == 2, len(win.graph_view._link_lines)
win.enter_list_mode("all")
wait_results(300)
assert win.view_mode == "list" and not win.graph_view.isVisible()

# a plain folder (inside the vault) gets a STRUCTURE graph: root + its files,
# laid out as a static radial tree (no simulation running)
sub_path = os.path.normpath(os.path.join(gdir, "sub"))
win.drill_to_path(sub_path, "sub")
win.enter_graph_mode()
wait_results(4000)
assert win.view_mode == "graph"
assert len(win.graph_view._items) == 2, list(win.graph_view._items)
assert {it.kind for it in win.graph_view._items.values()} == {"folder", "file"}
assert not win.graph_view._timer.isActive(), "structure graphs must be static"
assert win.graph_view._float_timer.isActive(), "structure graph nodes must float"
its = list(win.graph_view._items.values())
assert (its[0].pos() - its[1].pos()).manhattanLength() > 50
win.enter_list_mode("all")
wait_results(300)
assert win.view_mode == "list" and not win.graph_view.isVisible()

# progressive disclosure: pressing a folder reveals one level of children;
# collapsing the root folds everything back
win.drill_to_path(data, "data")
win.enter_graph_mode()
wait_results(3000)
gv = win.graph_view
assert len(gv._items) == 5, len(gv._items)          # data + 3 dirs + 1 file
doc_idx = [i for i, it in gv._items.items() if it.label == "Documents"][0]
assert gv._items[doc_idx].can_expand
gv.expand_node(doc_idx)
wait_results(300)
assert len(gv._items) == 8, len(gv._items)          # + its 3 files
assert not gv._items[doc_idx].can_expand
data_idx = [i for i, it in gv._items.items() if it.label == "data"][0]
gv.collapse_node(data_idx)
wait_results(300)
assert len(gv._items) == 1, len(gv._items)
assert gv._items[data_idx].can_expand
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

# --- quick launch -------------------------------------------------------------
fake_app = os.path.join(data, "fake_app.exe")
with open(fake_app, "wb") as f:
    f.write(b"x")

ez.set_quicklaunch([fake_app])
assert ez.get_quicklaunch() == [os.path.normpath(fake_app)]
ez.bump_launch_count(fake_app)
assert ez.get_launch_counts().get(fake_app, 0) >= 1

win._build_sidebar()
wait_results(300)
assert len(win._ql_buttons) >= 1
ql_btn = win._ql_buttons[0]
assert ql_btn.path().lower() == os.path.normpath(fake_app).lower()

win.select_app(fake_app, ql_btn)
assert win._ql_selected == fake_app
assert win.details.btn_show.isEnabled()
assert win.details.btn_open.isEnabled()
assert "fake_app" in win.details.name_label.text().lower()

app_menu = win.build_app_menu(fake_app)
assert app_menu is not None
assert len(app_menu.actions()) >= 4

# clear via remove_quicklaunch
win.remove_quicklaunch(fake_app)
assert fake_app not in ez.get_quicklaunch()
assert win._ql_selected is None

# an app that was launched (has a count) must also disappear on remove
ez.set_quicklaunch([fake_app])
ez.bump_launch_count(fake_app)
win.remove_quicklaunch(fake_app)
assert fake_app not in ez.get_quicklaunch()
assert fake_app not in ez.get_launch_counts()
win._build_sidebar()
wait_results(300)
assert all(b.path().lower() != fake_app.lower() for b in win._ql_buttons)

# Add App flow (dialog stubbed): must pin the chosen file — this is the
# regression test for the missing-QFileDialog-import NameError
_orig_dlg = ez.QFileDialog.getOpenFileName
ez.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (fake_app, "Programs"))
try:
    win.add_quicklaunch_app()
finally:
    ez.QFileDialog.getOpenFileName = _orig_dlg
assert os.path.normpath(fake_app) in ez.get_quicklaunch()
ql_btn = win._ql_buttons[0]

# single-press on an app opens its containing folder in the main panel
win.drill_to_path(os.path.join(data, "Documents"), "Documents")
wait_results(300)
assert win.current_place == os.path.join(data, "Documents")
win.select_app(fake_app, ql_btn)
wait_results(300)
assert win.current_place == os.path.dirname(fake_app), win.current_place
assert win._ql_selected == fake_app
assert "fake_app" in win.details.name_label.text().lower()
# real icons: button and details icon must be present (the shell supplies
# the actual glyph on Windows; non-null is the assertable part)
assert not ql_btn.icon().isNull()
assert not win.details.icon_label.pixmap().isNull()
win.remove_quicklaunch(fake_app)

# press must open the folder even when already "in" it behind a search
win.titlebar.search.edit.setText("alpha")
wait_results(500)
win.select_app(fake_app, ql_btn)
wait_results(500)
assert win.titlebar.search.edit.text() == ""
assert win.current_place == os.path.dirname(fake_app)
assert win.tree.topLevelItemCount() >= 3, win.tree.topLevelItemCount()
# and from graph mode: press returns to the list with the folder open
win.enter_graph_mode()
wait_results(500)
assert win.view_mode == "graph"
win.select_app(fake_app, ql_btn)
wait_results(500)
assert win.view_mode == "list"
assert win.current_place == os.path.dirname(fake_app)
assert win._ql_selected == fake_app
# a .lnk press opens the TARGET's folder, not the shortcut's folder
desk = os.path.join(data, "Desktop")
os.makedirs(desk, exist_ok=True)
lnk = os.path.join(desk, "fake_app.lnk")
ps = ("$s=(New-Object -COM WScript.Shell).CreateShortcut('%s');"
      "$s.TargetPath='%s';$s.Save()") % (lnk, fake_app)
subprocess.run(["powershell", "-NoProfile", "-Command", ps],
               check=True, capture_output=True)
assert os.path.isfile(lnk)
assert os.path.normpath(ez.resolve_lnk_target(lnk)) \
    == os.path.normpath(fake_app)
ez.set_quicklaunch([lnk])
win._build_sidebar()
wait_results(300)
win.select_app(lnk, win._ql_buttons[0])
wait_results(500)
assert win.current_place == os.path.dirname(fake_app), win.current_place

# icon identity for shortcuts
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
lnk_pm = ez.app_icon_for(lnk).pixmap(32, 32)
tgt_pm = ez.real_file_icon(fake_app).pixmap(32, 32)
assert not lnk_pm.isNull()
assert not tgt_pm.isNull()
assert not win._ql_buttons[0].icon().isNull()


def _pm_png(pm):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    pm.save(buf, "PNG")
    return bytes(ba.data())


assert _pm_png(lnk_pm) == _pm_png(tgt_pm)
win.remove_quicklaunch(lnk)

# (2) Creation
_orig_txt = ez.QInputDialog.getText
ez.QInputDialog.getText = staticmethod(
    lambda *a, **k: ("created-by-test.txt", True))
try:
    win.create_new_entry(data, False)
finally:
    ez.QInputDialog.getText = _orig_txt
wait_results(500)
assert os.path.isfile(os.path.join(data, "created-by-test.txt"))
assert any(r["name"] == "created-by-test.txt" for r in win.current_rows)
os.remove(os.path.join(data, "created-by-test.txt"))

mvA = os.path.join(data, "mvA")
mvB = os.path.join(data, "mvB")
os.makedirs(mvA)
os.makedirs(mvB)
srcf = os.path.join(mvA, "move-me.txt")
with open(srcf, "w") as fh:
    fh.write("x")
conn_mv = ez.thread_conn()
ez.index_add_path(conn_mv, mvA)
ez.index_add_path(conn_mv, mvB)
conn_mv.commit()
# engine: move keeps disk + index in sync
assert win.move_or_copy([srcf], mvB, copy=False) is True
assert os.path.isfile(os.path.join(mvB, "move-me.txt"))
assert not os.path.exists(srcf)
assert conn_mv.execute("SELECT COUNT(*) FROM files WHERE parent=? AND name=?",
                       (mvB, "move-me.txt")).fetchone()[0] == 1
assert conn_mv.execute("SELECT COUNT(*) FROM files WHERE parent=? AND name=?",
                       (mvA, "move-me.txt")).fetchone()[0] == 0
# copy into the same folder collides -> Explorer-style " (2)" name
assert win.move_or_copy([os.path.join(mvB, "move-me.txt")], mvB,
                        copy=True) is True
assert os.path.isfile(os.path.join(mvB, "move-me (2).txt"))
# clipboard cut/paste round trip through the window methods
win.set_clipboard_files([os.path.join(mvB, "move-me.txt")], True)
paths, cut = win.clipboard_files()
assert cut is True and len(paths) == 1
win.paste_into(mvA)
assert os.path.isfile(os.path.join(mvA, "move-me.txt"))
assert not os.path.exists(os.path.join(mvB, "move-me.txt"))
# same-folder move via handle_drop is a no-op
win.handle_drop([os.path.join(mvA, "move-me.txt")], mvA, copy=False)
assert os.path.isfile(os.path.join(mvA, "move-me.txt"))
assert not os.path.exists(os.path.join(mvA, "move-me (2).txt"))

# 1. Lock-hog: hold write lock for ~35 s while IndexerThread runs
hog_ready = threading.Event()

def _hog():
    hog = ez.sqlite3.connect(ez.DB_PATH)
    hog.execute("BEGIN IMMEDIATE")
    hog.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('lock_hog', '1')")
    hog_ready.set()
    time.sleep(35)
    hog.commit()
    hog.close()

t_hog = threading.Thread(target=_hog, daemon=True)
t_hog.start()
assert hog_ready.wait(5), "hog thread failed to start"

_orig_fixed = ez.fixed_drives
ez.fixed_drives = lambda: [data]
idx_res = []
try:
    idx = ez.IndexerThread(rebuild=True)
    idx.done.connect(lambda total, err: idx_res.append((total, err)), ez.Qt.DirectConnection)
    idx.start()
    assert idx.wait(60000), "IndexerThread timed out"
    qapp.processEvents()
    assert idx_res and idx_res[0][0] >= 0, f"Indexer failed with: {idx_res}"
finally:
    ez.fixed_drives = _orig_fixed
    t_hog.join(10)

win._index_lock_retried = False
win.on_index_done(-1, "database is locked")
assert win._index_lock_retried is True
assert "retrying" in win.status_right.text()
win._index_lock_retried = False

# 2. Analyzer quiesce
_orig_fixed = ez.fixed_drives
ez.fixed_drives = lambda: [data]
try:
    win._analyzer_running = True
    win.start_indexer(True)
    assert win._analyzer_stop.is_set()
finally:
    if win._indexer:
        win._indexer.wait(5000)
    ez.fixed_drives = _orig_fixed

# 3. Same-operation retry
assert getattr(win, "_last_index_rebuild", None) is True
recorded = []
_orig_start = win.start_indexer
win.start_indexer = lambda rebuild: recorded.append(rebuild)
try:
    win._retry_last_index()
    assert recorded == [True], f"Expected [True], got {recorded}"
finally:
    win.start_indexer = _orig_start

# 4. Queue-on-busy
class _RunningStub:
    def isRunning(self):
        return True

_orig_indexer = win._indexer
win._indexer = _RunningStub()
try:
    win._index_pending = None
    win.start_indexer(False)
    assert win._index_pending is False
    assert "queued" in win.rescan_btn.text().lower()
    win.start_indexer(True)
    assert win._index_pending is True
finally:
    win._indexer = _orig_indexer

# 5. Consume-pending
recorded5 = []
_orig_start5 = win.start_indexer
win.start_indexer = lambda rebuild: recorded5.append(rebuild)
try:
    win._index_pending = False
    win._consume_pending_index()
    loop = QEventLoop()
    QTimer.singleShot(600, loop.quit)
    loop.exec()
    assert recorded5 == [False], f"Expected [False], got {recorded5}"
    assert win._index_pending is None
    assert win.rescan_btn.text() == "  Rescan now"
finally:
    win.start_indexer = _orig_start5

# --- HotkeyFilter: nativeEventFilter receives WM_HOTKEY ---
import ctypes  # noqa: E402
from ctypes import wintypes  # noqa: E402
from PySide6.QtCore import QByteArray  # noqa: E402

hotkey_events = []
filt = ez.HotkeyFilter(lambda: hotkey_events.append(1))

msg = wintypes.MSG()
msg.message = 0x0312  # WM_HOTKEY
msg.wParam = 0xB00B   # HOTKEY_ID

filt.nativeEventFilter(QByteArray(b"windows_generic_MSG"), ctypes.addressof(msg))
assert len(hotkey_events) == 1, f"Expected 1 hotkey fire, got {len(hotkey_events)}"

# Negative 1: same call with msg.wParam = 0x1234 -> no additional fire
msg.wParam = 0x1234
filt.nativeEventFilter(QByteArray(b"windows_generic_MSG"), ctypes.addressof(msg))
assert len(hotkey_events) == 1, f"Expected 1 hotkey fire, got {len(hotkey_events)}"

# Negative 2: same call with event type QByteArray(b"windows_dbt_DEVICEARRIVAL") -> no additional fire
msg.wParam = 0xB00B
filt.nativeEventFilter(QByteArray(b"windows_dbt_DEVICEARRIVAL"), ctypes.addressof(msg))
assert len(hotkey_events) == 1, f"Expected 1 hotkey fire, got {len(hotkey_events)}"

# --- Spotlight launcher tests (Part A, Part B, Part C) ---
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtCore import QEvent, Qt  # noqa: E402

spot.main = win

# Part A: Apps first (query results)
fake_prog_dir = r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\TestApp"
app_row1 = {"parent": fake_prog_dir, "name": "AppOne.lnk", "is_dir": 0, "size": 100, "mtime": 1.0, "category": "other", "kind": "Shortcut"}
doc_row1 = {"parent": r"C:\Docs", "name": "AppNotes.txt", "is_dir": 0, "size": 200, "mtime": 2.0, "category": "doc", "kind": "Text Document"}
other_exe = {"parent": r"C:\Tools", "name": "AppUtil.exe", "is_dir": 0, "size": 300, "mtime": 3.0, "category": "other", "kind": "Application"}
ez.bump_launch_count(os.path.normpath(r"C:\Tools\AppUtil.exe"))

spot.spot_seq = 999
test_rows = [doc_row1, app_row1, other_exe]
spot.on_results(999, "App", test_rows, "")

assert spot.tree.topLevelItemCount() >= 4, f"Unexpected item count: {spot.tree.topLevelItemCount()}"
assert spot.tree.topLevelItem(0).text(0) == "Apps", f"Expected Apps header, got {spot.tree.topLevelItem(0).text(0)}"
assert spot.tree.topLevelItem(1).text(0) == "AppOne.lnk"
assert spot.tree.topLevelItem(2).text(0) == "AppUtil.exe"
assert spot.tree.topLevelItem(3).text(0) == "Top Hit"
assert spot.tree.topLevelItem(4).text(0) == "AppNotes.txt"
all_texts = [spot.tree.topLevelItem(i).text(0) for i in range(spot.tree.topLevelItemCount())]
assert all_texts.count("AppOne.lnk") == 1
assert all_texts.count("AppUtil.exe") == 1

# Part B: Quick Launch home (empty query)
pinned_path = os.path.normpath(os.path.join(docs, "meeting-notes.txt"))
counted_path = os.path.normpath(os.path.join(data, "render_final.mp4"))
ez.set_quicklaunch([pinned_path])
ez.bump_launch_count(counted_path)

spot.spot_seq = 1000
recent_test_rows = [
    {"parent": docs, "name": "Q3 Financial Report.pdf", "is_dir": 0, "size": 10, "mtime": 10.0, "category": "doc", "kind": "PDF Document"}
]
spot.on_results(1000, "", recent_test_rows, "")
assert spot.tree.topLevelItem(0).text(0) == "Quick Launch", f"Expected Quick Launch header, got {spot.tree.topLevelItem(0).text(0)}"
assert spot.tree.topLevelItem(1).text(0) == "meeting-notes.txt"
ql_names = [spot.tree.topLevelItem(i).text(0) for i in range(spot.tree.topLevelItemCount())]
assert "meeting-notes.txt" in ql_names
assert "render_final.mp4" in ql_names
assert "Recent" in ql_names
assert "Q3 Financial Report.pdf" in ql_names

# Part C: Enter opens folders; Alt+Enter drills into EzExp; footer text
foot_lbl = spot.findChild(ez.QLabel, "spotFooter")
assert foot_lbl is not None
assert foot_lbl.text() == "  \u23ce  Open      Ctrl \u23ce  Show in Folder      Alt \u23ce  In EzExp      Esc  Close  ", f"Got {foot_lbl.text()}"

folder_row = {"parent": data, "name": "Documents", "is_dir": 1, "size": 0, "mtime": 1.0, "category": "folder", "kind": "Folder"}
spot.spot_seq = 1001
spot.on_results(1001, "Documents", [folder_row], "")
spot.tree.setCurrentItem(spot.tree.topLevelItem(1))
assert spot.current_row() and spot.current_row()["name"] == "Documents"

startfile_calls = []
_orig_startfile = os.startfile
os.startfile = lambda p: startfile_calls.append(p)
win.current_place = None
spot.show()
try:
    ev_enter = QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier)
    spot.keyPressEvent(ev_enter)
    assert spot.isHidden(), "Spotlight should hide after Enter"
    expected_folder_path = os.path.normpath(os.path.join(data, "Documents"))
    assert len(startfile_calls) == 1 and os.path.normpath(startfile_calls[0]) == expected_folder_path, f"os.startfile not called with folder: {startfile_calls}"
    assert win.current_place is None, f"win.current_place should not change on Enter: {win.current_place}"
finally:
    os.startfile = _orig_startfile

startfile_calls = []
os.startfile = lambda p: startfile_calls.append(p)
spot.show()
spot.tree.setCurrentItem(spot.tree.topLevelItem(1))
try:
    ev_alt_enter = QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.AltModifier)
    spot.keyPressEvent(ev_alt_enter)
    assert spot.isHidden(), "Spotlight should hide after Alt+Enter"
    assert len(startfile_calls) == 0, f"os.startfile should NOT be called on Alt+Enter: {startfile_calls}"
    assert win.current_place == expected_folder_path, f"win.current_place should be drilled to folder: {win.current_place}"
finally:
    os.startfile = _orig_startfile

worker.stop()
worker.wait(3000)
print("ALL TESTS PASSED")
