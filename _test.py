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

# Part E tests: Agent mode in Spotlight (LOCAL ONLY, stub server, no network)
# ===========================================================================
import json
import http.server

# 1. .gitignore contains agent.json, .env, *.key
repo_root = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(repo_root, ".gitignore"), "r", encoding="utf-8") as f:
    gi_text = f.read()
assert "agent.json" in gi_text, "agent.json missing from .gitignore"
assert ".env" in gi_text, ".env missing from .gitignore"
assert "*.key" in gi_text, "*.key missing from .gitignore"

# 2. Config round-trip
_orig_app_dir = ez.APP_DIR
_test_cfg_dir = os.path.join(tmp, "agent_cfg_test")
os.makedirs(_test_cfg_dir, exist_ok=True)
ez.APP_DIR = _test_cfg_dir
try:
    # Defaults load when no file exists
    defaults = ez.load_agent_config()
    expected_defaults = {
        "backend": "endpoint",
        "harness_path": "",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "meta/llama-3.3-70b-instruct",
        "api_key": "",
        "grant_dir": "",
    }
    assert defaults == expected_defaults, f"Expected {expected_defaults}, got {defaults}"

    # Save a config, load it back, assert equality
    test_cfg = {
        "backend": "endpoint",
        "harness_path": "",
        "base_url": "http://127.0.0.1:5555/v1",
        "model": "meta/llama-test",
        "api_key": "local-test-key",
        "grant_dir": _test_cfg_dir,
    }
    ez.save_agent_config(test_cfg)
    loaded = ez.load_agent_config()
    assert loaded == test_cfg, f"Expected {test_cfg}, got {loaded}"

    # Corrupt JSON -> defaults without raising
    with open(ez.agent_config_path(), "w", encoding="utf-8") as f:
        f.write("corrupted JSON {[{")
    assert ez.load_agent_config() == expected_defaults
finally:
    ez.APP_DIR = _orig_app_dir

# 3. Grant gates
# create_file with no grant -> error
client_no_grant = ez.AgentClient(config={"grant_dir": "", "api_key": "dummy"})
res = client_no_grant.dispatch_tool("create_file", {"rel_path": "foo.txt", "content": "bar"})
assert "error" in res.lower() or "no grant" in res.lower(), f"Expected error, got: {res}"

# with a grant but ../escape.txt -> error
grant_test_dir = os.path.join(tmp, "grant_sandbox")
os.makedirs(grant_test_dir, exist_ok=True)
client_grant = ez.AgentClient(config={"grant_dir": grant_test_dir, "api_key": "dummy"})
res_esc = client_grant.dispatch_tool("create_file", {"rel_path": "../escape.txt", "content": "bar"})
assert "error" in res_esc.lower() or "escape" in res_esc.lower(), f"Expected escape error, got: {res_esc}"
assert not os.path.exists(os.path.join(grant_test_dir, "../escape.txt"))

# inside the grant with confirmer stubbed to False -> no file, "declined" returned
client_declined = ez.AgentClient(
    config={"grant_dir": grant_test_dir, "api_key": "dummy"},
    confirmer=lambda txt: False,
)
res_dec = client_declined.dispatch_tool("create_file", {"rel_path": "declined.txt", "content": "bar"})
assert "declined" in res_dec.lower(), f"Expected 'declined', got: {res_dec}"
assert not os.path.exists(os.path.join(grant_test_dir, "declined.txt"))

# inside the grant with confirmer stubbed to True -> file exists on disk
client_ok = ez.AgentClient(
    config={"grant_dir": grant_test_dir, "api_key": "dummy"},
    confirmer=lambda txt: True,
)
res_ok = client_ok.dispatch_tool("create_file", {"rel_path": "sub/ok.txt", "content": "hello disk"})
target_ok = os.path.join(grant_test_dir, "sub", "ok.txt")
assert os.path.isfile(target_ok), f"Expected file on disk at {target_ok}"
assert os.path.realpath(res_ok) == os.path.realpath(target_ok)
with open(target_ok, "r", encoding="utf-8") as f:
    assert f.read() == "hello disk"

# create_folder tests
res_fld = client_ok.dispatch_tool("create_folder", {"rel_path": "my_sub_dir"})
target_fld = os.path.join(grant_test_dir, "my_sub_dir")
assert os.path.isdir(target_fld), f"Expected directory at {target_fld}"
# refuse if already exists
res_exist = client_ok.dispatch_tool("create_file", {"rel_path": "sub/ok.txt", "content": "new"})
assert "error" in res_exist.lower() or "already exists" in res_exist.lower()

# 4. Client against a stub server (http.server on 127.0.0.1)
stub_requests = []
stub_final_reply = "I found your meeting notes in the catalog."

class StubServerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        len_hdr = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(len_hdr)
        req_json = json.loads(body.decode("utf-8"))
        stub_requests.append(req_json)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        if len(stub_requests) == 1:
            # (a) First: tool_calls response asking for search_files
            resp = {
                "id": "chatcmpl-test-1",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_search_notes",
                            "type": "function",
                            "function": {
                                "name": "search_files",
                                "arguments": json.dumps({"query": "meeting"})
                            }
                        }]
                    },
                    "finish_reason": "tool_calls"
                }]
            }
        else:
            # (b) Then: final text reply
            resp = {
                "id": "chatcmpl-test-2",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": stub_final_reply
                    },
                    "finish_reason": "stop"
                }]
            }
        self.wfile.write(json.dumps(resp).encode("utf-8"))

httpd = http.server.HTTPServer(("127.0.0.1", 0), StubServerHandler)
server_port = httpd.server_port
t_server = threading.Thread(target=httpd.serve_forever, daemon=True)
t_server.start()

try:
    # Point Spotlight's agent client to the stub server
    spot.agent_client.config = {
        "base_url": f"http://127.0.0.1:{server_port}/v1",
        "model": "meta/llama-3.3-70b-instruct",
        "api_key": "stub-test-key",
        "grant_dir": grant_test_dir,
    }
    # Switch Spotlight to Agent mode
    spot.set_mode("agent")
    assert spot.mode == "agent"
    assert not spot.transcript.isHidden()
    assert spot.tree.isHidden()
    assert "Send" in spot.foot.text()
    assert not spot.btn_endpoint.isHidden()

    spot.transcript.clear()
    spot.edit.setText("Please find the meeting notes")

    # Drive one user message and wait for agent reply
    loop = QEventLoop()
    spot.agent_client.agent_text.connect(lambda txt: loop.quit())
    spot.agent_client.agent_error.connect(lambda err: loop.quit())
    QTimer.singleShot(8000, loop.quit)

    spot.send_agent_message()
    loop.exec()

    # Assert transcript ends with stub's final text
    actual_transcript = spot.transcript.toPlainText().strip()
    assert actual_transcript.endswith(stub_final_reply), (
        f"Transcript did not end with '{stub_final_reply}'. Full transcript:\n{actual_transcript}"
    )

    # Assert tool actually ran against the test catalog
    assert len(stub_requests) >= 2, f"Expected 2 HTTP requests to stub, got {len(stub_requests)}"
    round_2_messages = stub_requests[1]["messages"]
    tool_msgs = [m for m in round_2_messages if m.get("role") == "tool"]
    assert len(tool_msgs) >= 1, f"Expected tool message in round 2: {round_2_messages}"
    tool_content = tool_msgs[0].get("content", "")
    assert "meeting-notes.txt" in tool_content, (
        f"Tool should have run against test catalog and found 'meeting-notes.txt', got:\n{tool_content}"
    )

    # Verify opening Spotlight resets it to Search mode
    spot.open_spotlight()
    assert spot.mode == "search"
    assert not spot.tree.isHidden()
    assert spot.transcript.isHidden()
    assert spot.btn_endpoint.isHidden()
    assert "Open" in spot.foot.text()
    spot.hide()
finally:
    httpd.shutdown()
    httpd.server_close()
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

# ====================================================================
# Spotlight: real app icons, complete Quick Launch + web row, Enter drills folders
# ====================================================================

# 1. Icon routing (FIX 1)
_calls = {"app": 0, "img": 0, "cat": 0}
sentinel_app = object()
sentinel_img = object()
sentinel_cat = object()

_orig_app_icon = getattr(ez, "app_icon_for", None)
_orig_real_icon = getattr(ez, "real_file_icon", None)
_orig_cat_icon = getattr(ez, "category_icon", None)

def _stub_app(path):
    _calls["app"] += 1
    if "boom" in path:
        raise RuntimeError("boom")
    return sentinel_app

def _stub_img(path):
    _calls["img"] += 1
    return sentinel_img

def _stub_cat(cat):
    _calls["cat"] += 1
    return sentinel_cat

ez.app_icon_for = _stub_app
ez.real_file_icon = _stub_img
ez.category_icon = _stub_cat
if hasattr(ez, "_spot_icon_cache"):
    ez._spot_icon_cache.clear()

try:
    # a Chrome.lnk row whose stored category is "other" routes to app_icon_for (the staleness case)
    r_lnk = {"parent": r"C:\Programs", "name": "Chrome.lnk", "category": "other"}
    ic1 = ez.spot_row_icon(r_lnk)
    assert _calls["app"] == 1, f"Expected app_icon_for call, got {_calls}"
    assert ic1 == sentinel_app

    # an "img" row routes to real_file_icon
    r_img = {"parent": r"C:\Pics", "name": "photo.png", "category": "img"}
    ic2 = ez.spot_row_icon(r_img)
    assert _calls["img"] == 1, f"Expected real_file_icon call, got {_calls}"
    assert ic2 == sentinel_img

    # a "doc" row routes to category_icon
    r_doc = {"parent": r"C:\Docs", "name": "notes.txt", "category": "doc"}
    ic3 = ez.spot_row_icon(r_doc)
    assert _calls["cat"] >= 1, f"Expected category_icon call, got {_calls}"
    assert ic3 == sentinel_cat

    # a second call for the same path hits the cache (stub call count unchanged)
    app_calls_before = _calls["app"]
    ic1_again = ez.spot_row_icon(r_lnk)
    assert _calls["app"] == app_calls_before, "Cache was bypassed on second call"
    assert ic1_again == sentinel_app

    # a stub raising an exception yields the category glyph, not a raise
    r_boom = {"parent": r"C:\Programs", "name": "boom.exe", "category": "other"}
    cat_calls_before = _calls["cat"]
    ic_boom = ez.spot_row_icon(r_boom)
    assert _calls["cat"] > cat_calls_before, "Expected category_icon fallback on error"
    assert ic_boom == sentinel_cat
finally:
    if _orig_app_icon is not None:
        ez.app_icon_for = _orig_app_icon
    if _orig_real_icon is not None:
        ez.real_file_icon = _orig_real_icon
    if _orig_cat_icon is not None:
        ez.category_icon = _orig_cat_icon
    if hasattr(ez, "_spot_icon_cache"):
        ez._spot_icon_cache.clear()

# 2. QL completeness (FIX 2.1)
uncat_dir = os.path.join(tmp, "uncat_pins")
os.makedirs(uncat_dir, exist_ok=True)
uncat_files = []
for i in range(5):
    p = os.path.join(uncat_dir, f"uncat_{i}.exe")
    with open(p, "wb") as f:
        f.write(b"MZ")
    uncat_files.append(os.path.normpath(p))

cat_files = [
    os.path.normpath(os.path.join(docs, "meeting-notes.txt")),
    os.path.normpath(os.path.join(docs, "Q3 Financial Report.pdf")),
    os.path.normpath(os.path.join(code, "main.py")),
    os.path.normpath(os.path.join(data, "render_final.mp4")),
]
nonexistent_pin = os.path.normpath(os.path.join(tmp, "nonexistent_file.exe"))
ten_pins = cat_files + uncat_files + [nonexistent_pin]
assert len(ten_pins) == 10

_orig_counts = ez.get_launch_counts()
_settings = ez.QSettings("ezexp", "EzExp")
_settings.setValue("launch_counts", "{}")
_settings.sync()
try:
    ez.set_quicklaunch(ten_pins)
    spot.spot_seq = 2000
    spot.on_results(2000, "", [], "")

    ql_items = []
    is_ql = False
    for i in range(spot.tree.topLevelItemCount()):
        it = spot.tree.topLevelItem(i)
        d = it.data(0, Qt.UserRole)
        if d and d.get("__header__"):
            is_ql = (d.get("text") == "Quick Launch")
        elif is_ql:
            ql_items.append(d)

    assert len(ql_items) == 9, f"Expected 9 resolvable QL items, got {len(ql_items)}"
    assert all(os.path.basename(nonexistent_pin) != r["name"] for r in ql_items), "Nonexistent pin should be skipped"
    assert spot.tree.currentItem() is not None, "A non-header item should be selected"
    assert not spot.tree.currentItem().data(0, Qt.UserRole).get("__header__"), "Selected item must not be a header"
    assert spot.tree.currentItem() == spot.tree.topLevelItem(1), "First non-header item must be selected"
finally:
    _settings.setValue("launch_counts", json.dumps(_orig_counts))
    _settings.sync()

# 3. Web row (FIX 2.2, 2.3)
spot.spot_seq = 2001
spot.on_results(2001, "gibberishqueryxyz", [], "")
assert spot.tree.topLevelItemCount() == 1, f"Expected exactly 1 item, got {spot.tree.topLevelItemCount()}"
only_item = spot.tree.topLevelItem(0)
d_web = only_item.data(0, Qt.UserRole)
assert d_web and d_web.get("__web__") is True, f"Expected web sentinel, got {d_web}"
assert d_web.get("name") == "gibberishqueryxyz"
assert spot.tree.currentItem() == only_item, "Web row must be current selection on no results"

spot.spot_seq = 2002
spot.on_results(2002, "notes", [{"parent": docs, "name": "meeting-notes.txt", "is_dir": 0, "size": 5, "mtime": 1.0, "category": "doc", "kind": "Text"}], "")
assert spot.tree.topLevelItemCount() >= 2
last_item = spot.tree.topLevelItem(spot.tree.topLevelItemCount() - 1)
d_last = last_item.data(0, Qt.UserRole)
assert d_last and d_last.get("__web__") is True, f"Expected last item to be web sentinel, got {d_last}"
assert spot.tree.currentItem() == spot.tree.topLevelItem(1), "Matches query must select first result"

from PySide6.QtGui import QDesktopServices  # noqa: E402
recorded_urls = []
recorded_startfile = []
_orig_openUrl = getattr(QDesktopServices, "openUrl", None)
_orig_startfile = os.startfile
QDesktopServices.openUrl = staticmethod(lambda url: recorded_urls.append(url.toString()))
os.startfile = lambda p: recorded_startfile.append(p)
try:
    spot.spot_seq = 2003
    spot.on_results(2003, "test search", [], "")
    spot.show()
    ev_enter = QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier)
    spot.keyPressEvent(ev_enter)
    assert spot.isHidden(), "Spotlight should hide after Enter on web row"
    assert len(recorded_urls) == 1, f"Expected 1 openUrl call, got {recorded_urls}"
    assert recorded_urls[0].startswith("https://duckduckgo.com/?q=test+search") or "test" in recorded_urls[0]
    assert len(recorded_startfile) == 0, f"os.startfile should not be called: {recorded_startfile}"
finally:
    if _orig_openUrl is not None:
        QDesktopServices.openUrl = _orig_openUrl
    os.startfile = _orig_startfile

# 4. Enter semantics (FIX 3)
foot_lbl = spot.findChild(ez.QLabel, "spotFooter")
assert foot_lbl is not None
assert foot_lbl.text() == "  \u23ce  Open (folders in EzExp)      Ctrl \u23ce  Show in Folder      Alt \u23ce  In EzExp      Esc  Close  ", f"Got {foot_lbl.text()}"

folder_row = {"parent": data, "name": "Documents", "is_dir": 1, "size": 0, "mtime": 1.0, "category": "folder", "kind": "Folder"}
file_row = {"parent": docs, "name": "meeting-notes.txt", "is_dir": 0, "size": 5, "mtime": 1.0, "category": "doc", "kind": "Text"}

# Enter on folder row with spot.main -> win.current_place becomes folder path and startfile NOT called
startfile_calls = []
_orig_startfile = os.startfile
os.startfile = lambda p: startfile_calls.append(p)
win.current_place = None
spot.main = win
spot.show()
spot.spot_seq = 2004
spot.on_results(2004, "Documents", [folder_row], "")
spot.tree.setCurrentItem(spot.tree.topLevelItem(1))
try:
    ev_enter = QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier)
    spot.keyPressEvent(ev_enter)
    assert spot.isHidden(), "Spotlight should hide after Enter on folder"
    expected_folder_path = os.path.normpath(os.path.join(data, "Documents"))
    assert win.current_place == expected_folder_path, f"win.current_place should be drilled: {win.current_place}"
    assert len(startfile_calls) == 0, f"os.startfile should NOT be called on folder Enter: {startfile_calls}"
finally:
    os.startfile = _orig_startfile

# Enter on a file row -> startfile called once
startfile_calls = []
os.startfile = lambda p: startfile_calls.append(p)
win.current_place = None
spot.show()
spot.spot_seq = 2005
spot.on_results(2005, "meeting-notes.txt", [file_row], "")
spot.tree.setCurrentItem(spot.tree.topLevelItem(1))
try:
    ev_enter = QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier)
    spot.keyPressEvent(ev_enter)
    assert spot.isHidden(), "Spotlight should hide after Enter on file"
    expected_file_path = os.path.normpath(os.path.join(docs, "meeting-notes.txt"))
    assert len(startfile_calls) == 1 and os.path.normpath(startfile_calls[0]) == expected_file_path, f"startfile should be called once on file: {startfile_calls}"
    assert win.current_place is None, "win.current_place should not change on file Enter"
finally:
    os.startfile = _orig_startfile

# with spot.main = None, Enter on a folder -> startfile called (fallback)
startfile_calls = []
os.startfile = lambda p: startfile_calls.append(p)
spot.main = None
spot.show()
spot.spot_seq = 2006
spot.on_results(2006, "Documents", [folder_row], "")
spot.tree.setCurrentItem(spot.tree.topLevelItem(1))
try:
    ev_enter = QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier)
    spot.keyPressEvent(ev_enter)
    assert spot.isHidden(), "Spotlight should hide after Enter on folder fallback"
    assert len(startfile_calls) == 1 and os.path.normpath(startfile_calls[0]) == expected_folder_path, f"startfile fallback expected: {startfile_calls}"
finally:
    os.startfile = _orig_startfile
    spot.main = win


# ====================================================================
# Organism Harness integration tests (stub harness only; no real checkout, no network, no keys)
# ---------------------------------------------------------------------------
fake_harness_root = tempfile.mkdtemp(prefix="fake_harness_")
fake_harness_pkg = os.path.join(fake_harness_root, "harness")
fake_runtime_pkg = os.path.join(fake_harness_pkg, "runtime")
os.makedirs(fake_runtime_pkg, exist_ok=True)

with open(os.path.join(fake_runtime_pkg, "__init__.py"), "w", encoding="utf-8") as fh:
    fh.write("")

with open(os.path.join(fake_runtime_pkg, "tools.py"), "w", encoding="utf-8") as fh:
    fh.write('''
class ToolResult:
    def __init__(self, ok=True, output="", error=None):
        self.ok = ok
        self.output = output
        self.error = error

    def __repr__(self):
        return f"ToolResult(ok={self.ok}, output={self.output!r}, error={self.error!r})"
''')

with open(os.path.join(fake_harness_pkg, "__init__.py"), "w", encoding="utf-8") as fh:
    fh.write('''
from harness.runtime.tools import ToolResult

class FakeTaskResult:
    def __init__(self, output_markdown="canned response from fake harness", atp_burned=42.0, total_cycle_latency_ms=123.45):
        self.output_markdown = output_markdown
        self.output = output_markdown
        self.atp_burned = atp_burned
        self.total_cycle_latency_ms = total_cycle_latency_ms

class Harness:
    instances = []
    run_calls = []
    should_raise = False
    sabotage_disable = False
    tool_to_call_during_run = None
    last_tool_result = None

    def __init__(self, workspace=None, providers=None, tools=None, disable_tools=None):
        self.workspace = workspace
        self.providers = providers
        self.constructor_disable_tools = disable_tools
        self._active_tools = {
            "shell": {"name": "shell", "description": "native shell", "parameters": {}, "func": None},
            "read_file": {"name": "read_file", "description": "native read", "parameters": {}, "func": None},
            "write_file": {"name": "write_file", "description": "native write", "parameters": {}, "func": None},
            "edit_file": {"name": "edit_file", "description": "native edit", "parameters": {}, "func": None},
            "list_dir": {"name": "list_dir", "description": "native list", "parameters": {}, "func": None},
        }
        if disable_tools and not Harness.sabotage_disable:
            self.disable_tools(disable_tools)
        if tools:
            for t in tools:
                self.register_tool(t["name"], t["description"], t["parameters"], t["func"])
        Harness.instances.append(self)

    def register_tool(self, name, description, parameters, func, replace=False):
        self._active_tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "func": func,
        }

    def disable_tools(self, names):
        for n in names:
            self._active_tools.pop(n, None)

    def tool_names(self):
        return list(self._active_tools.keys())

    def tool_schemas(self):
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            }
            for t in self._active_tools.values()
        ]

    def run(self, task, mode="pipeline", **kwargs):
        Harness.run_calls.append({
            "task": task,
            "mode": mode,
            "workspace": self.workspace,
            "providers": self.providers,
            "kwargs": kwargs,
        })
        if Harness.should_raise:
            raise RuntimeError("simulated harness explosion")
        if Harness.tool_to_call_during_run:
            name, call_kwargs = Harness.tool_to_call_during_run
            fn = self._active_tools[name]["func"]
            res = fn(**call_kwargs)
            Harness.last_tool_result = res
        return FakeTaskResult()
''')

# 1. Detection
_orig_sys_path = list(sys.path)
_orig_harness_mod = sys.modules.pop("harness", None)
try:
    # with harness_path pointing at fake package, harness_available returns True
    ok, err = ez.harness_available({"harness_path": fake_harness_root})
    assert ok is True, f"Expected True with fake harness, got: {err}"
    assert err == ""

    # with a bogus path, False + non-empty error string
    sys.modules.pop("harness", None)
    sys.path = [p for p in sys.path if os.path.abspath(p) != os.path.abspath(fake_harness_root)]
    ok, err = ez.harness_available({"harness_path": os.path.join(fake_harness_root, "nonexistent_dir_xyz")})
    assert ok is False, "Expected False with bogus harness path"
    assert len(err) > 0, "Expected non-empty error string"

    # with no fake package importable, False
    sys.modules.pop("harness", None)
    sys.path = [p for p in sys.path if "harness" not in p.lower() and "organism" not in p.lower()]
    ok, err = ez.harness_available({"harness_path": ""})
    assert ok is False, "Expected False with no package importable"
finally:
    sys.path = _orig_sys_path
    if _orig_harness_mod is not None:
        sys.modules["harness"] = _orig_harness_mod

# 2. Config round-trip
test_cfg_dir = tempfile.mkdtemp(prefix="ez_cfg_test_")
_orig_ez_app_dir = ez.APP_DIR
ez.APP_DIR = test_cfg_dir
try:
    # Legacy config (only phase-1 fields)
    legacy_json = os.path.join(test_cfg_dir, "agent.json")
    with open(legacy_json, "w", encoding="utf-8") as fh:
        json.dump({"base_url": "http://legacy.test", "model": "legacy-model", "api_key": "k", "grant_dir": "g"}, fh)
    loaded_legacy = ez.load_agent_config()
    assert loaded_legacy["backend"] == "endpoint", f"Expected default backend 'endpoint', got {loaded_legacy.get('backend')}"
    assert loaded_legacy["harness_path"] == "", f"Expected default harness_path '', got {loaded_legacy.get('harness_path')}"
    assert loaded_legacy["base_url"] == "http://legacy.test"

    # Save new config with backend=harness and harness_path
    ez.save_agent_config({
        "backend": "harness",
        "harness_path": fake_harness_root,
        "grant_dir": test_cfg_dir,
    })
    loaded_new = ez.load_agent_config()
    assert loaded_new["backend"] == "harness"
    assert loaded_new["harness_path"] == fake_harness_root
    assert loaded_new["grant_dir"] == test_cfg_dir
finally:
    ez.APP_DIR = _orig_ez_app_dir

# 3. Case A: Run wiring, public tool injection, schema equality, and Case A banner
grant_temp = tempfile.mkdtemp(prefix="ez_grant_test_")
harness_cfg = {
    "backend": "harness",
    "harness_path": fake_harness_root,
    "grant_dir": grant_temp,
}

if os.path.abspath(fake_harness_root) not in [os.path.abspath(p) for p in sys.path]:
    sys.path.insert(0, fake_harness_root)
sys.modules.pop("harness", None)
from harness import Harness

Harness.instances.clear()
Harness.run_calls.clear()
Harness.should_raise = False
Harness.sabotage_disable = False
Harness.tool_to_call_during_run = None
Harness.last_tool_result = None

spot.agent_client.config = harness_cfg
spot.set_mode("agent")
spot.transcript.clear()
spot._harness_banner_shown = False
spot.edit.setText("hello harness")
spot.send_agent_message()

assert spot._harness_worker is not None, "Worker should be created"
spot._harness_worker.wait(5000)
qapp.processEvents()

assert len(Harness.run_calls) == 1, f"Expected 1 run call, got {len(Harness.run_calls)}"
call = Harness.run_calls[0]
assert call["workspace"] == grant_temp, f"Expected workspace {grant_temp}, got {call['workspace']}"
assert call["providers"] == ["groq", "openrouter"], f"Expected providers ['groq', 'openrouter'], got {call['providers']}"
assert call["mode"] == "pipeline", f"Expected mode 'pipeline', got {call['mode']}"
assert call["task"] == "hello harness", f"Expected task 'hello harness', got {call['task']}"

transcript_txt = spot.transcript.toPlainText()
assert "canned response from fake harness" in transcript_txt, f"Canned text missing from transcript: {transcript_txt}"
assert "Tokens: 42.0" in transcript_txt, f"Token budget missing: {transcript_txt}"
assert "Time: 123.45ms" in transcript_txt, f"Latency missing: {transcript_txt}"
assert "Tools: EzExp catalog + grant tools via the harness (native harness tools disabled)." in transcript_txt, f"Case A banner missing: {transcript_txt}"

# Assert all 5 native tools were disabled at construction
assert len(Harness.instances) >= 1
inst = Harness.instances[-1]
assert inst.constructor_disable_tools == ["shell", "read_file", "write_file", "edit_file", "list_dir"], f"Expected all 5 native tools disabled at construction, got: {inst.constructor_disable_tools}"

# Assert exactly the 5 EzExp tools are registered
expected_five = sorted(["search_files", "list_folder", "open_file", "create_file", "create_folder"])
assert sorted(inst.tool_names()) == expected_five, f"Expected active tools {expected_five}, got {inst.tool_names()}"

# Assert registered tool schemas equal endpoint tool dicts
endpoint_tool_map = {t["function"]["name"]: t["function"] for t in ez.AGENT_TOOLS}
for t_schema in inst.tool_schemas():
    t_name = t_schema["name"]
    assert t_name in endpoint_tool_map, f"Unexpected tool {t_name}"
    expected_spec = endpoint_tool_map[t_name]
    assert t_schema["description"] == expected_spec["description"], f"Description mismatch for {t_name}"
    assert t_schema["parameters"] == expected_spec["parameters"], f"Parameters mismatch for {t_name}"

# 4. Fail-closed tool set assertion (sabotaged harness ignoring disable_tools)
Harness.sabotage_disable = True
spot.transcript.clear()
spot.edit.setText("test sabotage")
spot.send_agent_message()
assert spot._harness_worker is not None
spot._harness_worker.wait(5000)
qapp.processEvents()

# Run should have been refused BEFORE h.run
assert len(Harness.run_calls) == 1, "h.run should NOT have been called when tool set mismatched"
refusal_transcript = spot.transcript.toPlainText()
assert "Refused to run: harness tool set mismatch — expected EzExp tools only, got:" in refusal_transcript, f"Refusal line missing: {refusal_transcript}"
assert "shell" in refusal_transcript, f"Sabotaged tool 'shell' should be mentioned in refusal message: {refusal_transcript}"
Harness.sabotage_disable = False

# 5. Real tool dispatch through fake harness loop
Harness.tool_to_call_during_run = ("search_files", {"query": "test"})
spot.transcript.clear()
spot.edit.setText("run real search")
spot.send_agent_message()
assert spot._harness_worker is not None
spot._harness_worker.wait(5000)
qapp.processEvents()
assert Harness.last_tool_result is not None, "Tool should have executed"
assert Harness.last_tool_result.ok is True, f"search_files failed: {Harness.last_tool_result}"
assert isinstance(Harness.last_tool_result.output, str)
Harness.tool_to_call_during_run = None

# 6. Confirmer gates create_file: Deny vs Allow
# 6a. Deny
spot.agent_client.confirmer = lambda text: False
deny_rel_path = "denied_file.txt"
Harness.tool_to_call_during_run = ("create_file", {"rel_path": deny_rel_path, "content": "should be blocked"})
spot.transcript.clear()
spot.edit.setText("create denied file")
spot.send_agent_message()
assert spot._harness_worker is not None
spot._harness_worker.wait(5000)
qapp.processEvents()
assert not os.path.exists(os.path.join(grant_temp, deny_rel_path)), "Denied file should NOT exist on disk"
assert Harness.last_tool_result.ok is False, "Declined tool call should return ok=False"
assert Harness.last_tool_result.error == "user declined", f"Expected 'user declined', got: {Harness.last_tool_result}"

# 6b. Allow
spot.agent_client.confirmer = lambda text: True
allow_rel_path = "allowed_file.txt"
Harness.tool_to_call_during_run = ("create_file", {"rel_path": allow_rel_path, "content": "approved content 123"})
spot.transcript.clear()
spot.edit.setText("create allowed file")
spot.send_agent_message()
assert spot._harness_worker is not None
spot._harness_worker.wait(5000)
qapp.processEvents()
allowed_full_path = os.path.join(grant_temp, allow_rel_path)
assert os.path.exists(allowed_full_path), "Allowed file should exist on disk"
with open(allowed_full_path, "r", encoding="utf-8") as f:
    created_content = f.read()
assert created_content == "approved content 123", f"Created content mismatch: {created_content}"
assert Harness.last_tool_result.ok is True, f"Expected ok=True, got: {Harness.last_tool_result}"
Harness.tool_to_call_during_run = None

# 7. Failure honesty
Harness.should_raise = True
spot.transcript.clear()
spot.edit.setText("test failure")
spot.send_agent_message()
assert spot._harness_worker is not None
spot._harness_worker.wait(5000)
qapp.processEvents()

fail_transcript = spot.transcript.toPlainText()
assert "simulated harness explosion" in fail_transcript, f"Exception text missing from transcript: {fail_transcript}"
assert "Error:" in fail_transcript

# 8. ConfirmBridge tests (cross-thread confirm, UI-thread guard, and production confirmer resolution)
bridge_test_spot = ez.Spotlight(worker)
assert hasattr(bridge_test_spot, "confirm_bridge"), "Spotlight must own a ConfirmBridge"
bridge = bridge_test_spot.confirm_bridge

recorded_threads = []
_orig_qmessagebox_question = ez.QMessageBox.question

try:
    # 8a. Cross-thread confirm returning Yes:
    # Monkeypatch QMessageBox.question with a stub that records QThread.currentThread() and returns Yes
    def stub_question_yes(parent, title, text, *args, **kwargs):
        recorded_threads.append(ez.QThread.currentThread())
        return ez.QMessageBox.StandardButton.Yes

    ez.QMessageBox.question = stub_question_yes

    # Production confirmer path: bridge attached, no injected confirmer (confirmer is None)
    client_prod = ez.AgentClient(config={"grant_dir": grant_temp}, confirm_bridge=bridge)
    assert client_prod.confirmer is None

    class WorkerCrossThreadConfirm(ez.QThread):
        def __init__(self, action_fn):
            super().__init__()
            self.action_fn = action_fn
            self.res = None

        def run(self):
            self.res = self.action_fn()

    recorded_threads.clear()
    w_yes = WorkerCrossThreadConfirm(lambda: client_prod._confirm_action("Allow write to plan.txt?"))
    w_yes.start()
    while w_yes.isRunning():
        qapp.processEvents()
    w_yes.wait(5000)

    assert w_yes.res is True, f"Expected True from bridge, got: {w_yes.res}"
    assert len(recorded_threads) == 1, f"Expected exactly 1 QMessageBox call, got {len(recorded_threads)}"
    assert recorded_threads[0] == qapp.thread(), f"QMessageBox did not run on main UI thread! Thread: {recorded_threads[0]}"

    # 8b. Repeat with stub returning No -> returns False
    recorded_threads.clear()
    def stub_question_no(parent, title, text, *args, **kwargs):
        recorded_threads.append(ez.QThread.currentThread())
        return ez.QMessageBox.StandardButton.No

    ez.QMessageBox.question = stub_question_no

    w_no = WorkerCrossThreadConfirm(lambda: client_prod._confirm_action("Allow write to plan.txt?"))
    w_no.start()
    while w_no.isRunning():
        qapp.processEvents()
    w_no.wait(5000)

    assert w_no.res is False, f"Expected False from bridge, got: {w_no.res}"
    assert len(recorded_threads) == 1, f"Expected exactly 1 QMessageBox call, got {len(recorded_threads)}"
    assert recorded_threads[0] == qapp.thread(), f"QMessageBox did not run on main UI thread! Thread: {recorded_threads[0]}"

    # 8c. UI-thread guard: calling ask_from_worker on the UI thread returns the stubbed answer without hanging
    guard_res_no = bridge.ask_from_worker("Direct UI call with stub No")
    assert guard_res_no is False, f"Expected False from UI guard, got {guard_res_no}"

    ez.QMessageBox.question = stub_question_yes
    guard_res_yes = bridge.ask_from_worker("Direct UI call with stub Yes")
    assert guard_res_yes is True, f"Expected True from UI guard, got {guard_res_yes}"

    # 8d. Real HarnessWorker with production bridge (no injected confirmer)
    Harness.should_raise = False
    spot.agent_client.confirmer = None
    Harness.tool_to_call_during_run = ("create_file", {"rel_path": "bridge_approved.txt", "content": "bridge content"})
    spot.transcript.clear()
    spot.edit.setText("test bridge worker")
    spot.send_agent_message()
    assert spot._harness_worker is not None
    while spot._harness_worker.isRunning():
        qapp.processEvents()
    spot._harness_worker.wait(5000)
    qapp.processEvents()

    assert os.path.exists(os.path.join(grant_temp, "bridge_approved.txt")), "File should exist when bridge approved"
    assert Harness.last_tool_result.ok is True
    Harness.tool_to_call_during_run = None

finally:
    ez.QMessageBox.question = _orig_qmessagebox_question

# ====================================================================
worker.stop()
worker.wait(3000)
print("ALL TESTS PASSED")
