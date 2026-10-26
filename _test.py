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

# ===========================================================================
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
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "meta/llama-3.3-70b-instruct",
        "api_key": "",
        "grant_dir": "",
    }
    assert defaults == expected_defaults, f"Expected {expected_defaults}, got {defaults}"

    # Save a config, load it back, assert equality
    test_cfg = {
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

worker.stop()
worker.wait(3000)
print("ALL TESTS PASSED")
