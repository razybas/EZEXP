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
_test_settings = ez.QSettings('ezexp', 'EzExp')
_test_settings.remove('agent_dock_visible')
_test_settings.sync()

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
        "harness_python": "py",
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
        "harness_python": "py",
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
# Organism Harness integration tests (stub harness runner via subprocess; no real checkout, no network, no keys)
# ---------------------------------------------------------------------------
fake_harness_root = tempfile.mkdtemp(prefix="fake_harness_")
fake_harness_pkg = os.path.join(fake_harness_root, "harness")
fake_runtime_pkg = os.path.join(fake_harness_pkg, "runtime")
os.makedirs(fake_runtime_pkg, exist_ok=True)

with open(os.path.join(fake_harness_pkg, "__init__.py"), "w", encoding="utf-8") as fh:
    fh.write("# stub harness package\n")

with open(os.path.join(fake_runtime_pkg, "__init__.py"), "w", encoding="utf-8") as fh:
    fh.write("")

# Write stub host_runner.py in fake_harness_pkg
host_runner_code = r"""import sys
import json
import os
import time

def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ctrl_path = os.path.join(root, "control.json")

    def load_control():
        if os.path.exists(ctrl_path):
            with open(ctrl_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    ctrl = load_control()
    mode = ctrl.get("mode", os.environ.get("EZ_TEST_RUNNER_MODE", "happy"))

    # Read setup
    line = sys.stdin.readline()
    if not line:
        return
    setup_msg = json.loads(line)
    with open(os.path.join(root, "setup_received.json"), "w", encoding="utf-8") as f:
        json.dump(setup_msg, f)

    if mode == "error":
        err_msg = ctrl.get("error_message", "simulated harness explosion")
        sys.stdout.write(json.dumps({"type": "error", "message": err_msg}) + "\n")
        sys.stdout.flush()
        sys.exit(1)

    if mode == "sabotage":
        ready_msg = {
            "type": "ready",
            "tools": ["create_file", "create_folder", "list_folder", "open_file", "search_files", "shell"]
        }
        sys.stdout.write(json.dumps(ready_msg) + "\n")
        sys.stdout.flush()
        time.sleep(10)
        return

    # Normal ready
    ready_msg = {
        "type": "ready",
        "tools": sorted(["create_file", "create_folder", "list_folder", "open_file", "search_files"])
    }
    sys.stdout.write(json.dumps(ready_msg) + "\n")
    sys.stdout.flush()

    # Read task
    line = sys.stdin.readline()
    if not line:
        return
    task_msg = json.loads(line)
    if task_msg.get("mode") != "single":
        sys.stdout.write(json.dumps({"type": "error", "message": f"Expected mode single, got {task_msg.get('mode')}"}) + "\n")
        sys.stdout.flush()
        sys.exit(1)
    with open(os.path.join(root, "task_received.json"), "w", encoding="utf-8") as f:
        json.dump(task_msg, f)

    # Optional tool call
    tool_call = ctrl.get("tool_call")
    if tool_call:
        call_msg = {
            "type": "tool_call",
            "id": "call_1",
            "name": tool_call["name"],
            "arguments": tool_call.get("arguments", {}),
        }
        sys.stdout.write(json.dumps(call_msg) + "\n")
        sys.stdout.flush()

        res_line = sys.stdin.readline()
        if res_line:
            tool_res = json.loads(res_line)
            with open(os.path.join(root, "tool_result_received.json"), "w", encoding="utf-8") as f:
                json.dump(tool_res, f)

    # Final
    final_msg = {
        "type": "final",
        "text": ctrl.get("final_text", "canned response from fake harness"),
        "tokens": 42.0,
        "ms": 123.45,
    }
    sys.stdout.write(json.dumps(final_msg) + "\n")
    sys.stdout.flush()

if __name__ == "__main__":
    main()
"""

with open(os.path.join(fake_harness_pkg, "host_runner.py"), "w", encoding="utf-8") as fh:
    fh.write(host_runner_code)

def set_control(data):
    with open(os.path.join(fake_harness_root, "control.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)

def remove_control_files():
    for fname in ["control.json", "setup_received.json", "task_received.json", "tool_result_received.json"]:
        p = os.path.join(fake_harness_root, fname)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

# 1. Detection & Non-Import Probe Caching
ez.clear_harness_probe_cache()
assert len(ez._harness_probe_cache) == 0

# with harness_path pointing at fake package, harness_available returns True
ok, err = ez.harness_available({"harness_path": fake_harness_root, "harness_python": "py"})
assert ok is True, f"Expected True with fake harness, got: {err}"
assert err == ""
assert len(ez._harness_probe_cache) == 1

# cached hit returns same without re-running probe
ok_cached, err_cached = ez.harness_available({"harness_path": fake_harness_root, "harness_python": "py"})
assert ok_cached is True
assert err_cached == ""

# with a bogus path, False + non-empty error string (fast path: no spawn)
ok, err = ez.harness_available({"harness_path": os.path.join(fake_harness_root, "nonexistent_dir_xyz"), "harness_python": "py"})
assert ok is False, "Expected False with bogus harness path"
assert len(err) > 0, "Expected non-empty error string"

# with an existing empty dir without harness, probe spawns, fails, and is NOT cached
empty_probe_dir = tempfile.mkdtemp(prefix="empty_probe_")
ok, err = ez.harness_available({"harness_path": empty_probe_dir, "harness_python": "py"})
assert ok is False
assert len(err) > 0
assert len(ez._harness_probe_cache) == 1, "Failures must not be cached"

# with empty harness_path, False
ok, err = ez.harness_available({"harness_path": ""})
assert ok is False, "Expected False with no package importable"

# clearing cache
ez.clear_harness_probe_cache()
assert len(ez._harness_probe_cache) == 0

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
    assert loaded_legacy["harness_python"] == "py", f"Expected default harness_python 'py', got {loaded_legacy.get('harness_python')}"
    assert loaded_legacy["base_url"] == "http://legacy.test"

    # Save new config with backend=harness, harness_path, harness_python
    ez.save_agent_config({
        "backend": "harness",
        "harness_path": fake_harness_root,
        "harness_python": "py",
        "grant_dir": test_cfg_dir,
    })
    loaded_new = ez.load_agent_config()
    assert loaded_new["backend"] == "harness"
    assert loaded_new["harness_path"] == fake_harness_root
    assert loaded_new["harness_python"] == "py"
    assert loaded_new["grant_dir"] == test_cfg_dir
finally:
    ez.APP_DIR = _orig_ez_app_dir

# 2a. Dialog construction and round-trip widget population
dlg_test_dir = tempfile.mkdtemp(prefix="ez_dlg_test_")
_orig_app_dir_dlg = ez.APP_DIR
ez.APP_DIR = dlg_test_dir
try:
    dlg = ez.AgentSettingsDialog(spot)
    assert hasattr(dlg, "combo_backend"), "Dialog must have combo_backend"
    backend_items = [dlg.combo_backend.itemText(i) for i in range(dlg.combo_backend.count())]
    assert backend_items == ["Endpoint", "Organism Harness"], f"Expected ['Endpoint', 'Organism Harness'], got: {backend_items}"

    # Verify all widgets exist
    assert hasattr(dlg, "edit_url") and dlg.edit_url is not None
    assert hasattr(dlg, "edit_key") and dlg.edit_key is not None
    assert hasattr(dlg, "edit_model") and dlg.edit_model is not None
    assert hasattr(dlg, "edit_harness") and dlg.edit_harness is not None
    assert hasattr(dlg, "edit_python") and dlg.edit_python is not None
    assert hasattr(dlg, "edit_grant") and dlg.edit_grant is not None

    # Fill every field and accept via _on_save
    dlg.combo_backend.setCurrentIndex(1)
    dlg.edit_harness.setText(fake_harness_root)
    dlg.edit_python.setText("py")
    dlg.edit_url.setText("https://test.endpoint/v1")
    dlg.edit_model.setText("test-custom-model")
    dlg.edit_key.setText("test-secret-key-456")
    dlg.edit_grant.setText(dlg_test_dir)
    dlg._on_save()

    # Verify persisted in agent.json
    persisted = ez.load_agent_config()
    assert persisted["backend"] == "harness"
    assert persisted["harness_path"] == fake_harness_root
    assert persisted["harness_python"] == "py"
    assert persisted["base_url"] == "https://test.endpoint/v1"
    assert persisted["model"] == "test-custom-model"
    assert persisted["api_key"] == "test-secret-key-456"
    assert persisted["grant_dir"] == dlg_test_dir

    # Construct second dialog and assert widgets show persisted values
    dlg2 = ez.AgentSettingsDialog(spot)
    assert dlg2.combo_backend.currentIndex() == 1
    assert dlg2.edit_harness.text() == fake_harness_root
    assert dlg2.edit_python.text() == "py"
    assert dlg2.edit_url.text() == "https://test.endpoint/v1"
    assert dlg2.edit_model.text() == "test-custom-model"
    assert dlg2.edit_key.text() == "test-secret-key-456"
    assert dlg2.edit_grant.text() == dlg_test_dir
finally:
    ez.APP_DIR = _orig_app_dir_dlg

grant_temp = tempfile.mkdtemp(prefix="ez_grant_test_")

# 2b. agent_status unit coverage for all five states
# State 1: endpoint, no key
st1_ok, st1_msg = ez.agent_status({"backend": "endpoint", "api_key": ""})
assert st1_ok is False
assert st1_msg == "Setup required: No API key configured. Click 'Endpoint…' above to configure your OpenAI-compatible endpoint and API key."

# State 2: endpoint, key present
st2_ok, st2_msg = ez.agent_status({"backend": "endpoint", "api_key": "some-key-value"})
assert st2_ok is True
assert st2_msg == ""

# State 3: harness, harness_available False
st3_ok, st3_msg = ez.agent_status({"backend": "harness", "harness_path": os.path.join(fake_harness_root, "bogus_dir_xyz"), "harness_python": "py"})
assert st3_ok is False
assert st3_msg.startswith("Organism Harness not available:")
assert "Set the harness folder via 'Endpoint…' above." in st3_msg

# State 4: harness, available, no grant
st4_ok, st4_msg = ez.agent_status({"backend": "harness", "harness_path": fake_harness_root, "harness_python": "py", "grant_dir": ""})
assert st4_ok is False
assert st4_msg == "Set a grant folder via 'Endpoint…' above — it becomes the harness workspace."

# State 5: harness, available + grant
st5_ok, st5_msg = ez.agent_status({"backend": "harness", "harness_path": fake_harness_root, "harness_python": "py", "grant_dir": grant_temp})
assert st5_ok is True
assert st5_msg == f"Organism Harness ready · workspace: {grant_temp}"

# 2c. Panel-level status and regression pinning
# Harness configured + fake harness + grant -> ready line visible, NOT "No API key configured"
spot.agent_client.config = {"backend": "harness", "harness_path": fake_harness_root, "harness_python": "py", "grant_dir": grant_temp}
spot.agent_messages.clear()
spot.set_mode("agent")
panel_harness_txt = spot.transcript.toPlainText()
assert f"Organism Harness ready · workspace: {grant_temp}" in panel_harness_txt
assert "No API key configured" not in panel_harness_txt

# Endpoint + no key -> original setup text verbatim
spot.agent_client.config = {"backend": "endpoint", "api_key": ""}
spot.agent_messages.clear()
spot.set_mode("agent")
panel_ep_txt = spot.transcript.toPlainText()
assert "Setup required: No API key configured. Click 'Endpoint…' above to configure your OpenAI-compatible endpoint and API key." in panel_ep_txt

# 2d. Send while not-ready -> no worker constructed
spot.agent_client.config = {"backend": "endpoint", "api_key": ""}
spot.transcript.clear()
spot.edit.setText("Attempt send while not ready")
spot.send_agent_message()
assert spot._bridge_worker is None or not spot._bridge_worker.isRunning()
assert "Setup required: No API key configured" in spot.transcript.toPlainText()

# 3. Case A: Run wiring, public tool injection, schema equality, and Case A banner
remove_control_files()
set_control({"mode": "happy"})

harness_cfg = {
    "backend": "harness",
    "harness_path": fake_harness_root,
    "harness_python": "py",
    "grant_dir": grant_temp,
}

spot.agent_client.config = harness_cfg
spot.set_mode("agent")
spot.transcript.clear()
spot._harness_banner_shown = False
spot.edit.setText("hello harness")
spot.send_agent_message()

assert spot._bridge_worker is not None, "Worker should be created"
while spot._bridge_worker.isRunning():
    qapp.processEvents()
spot._bridge_worker.wait(5000)
qapp.processEvents()

# Verify setup received
setup_file = os.path.join(fake_harness_root, "setup_received.json")
assert os.path.exists(setup_file), "Runner should have received setup"
with open(setup_file, "r", encoding="utf-8") as f:
    setup_data = json.load(f)
assert setup_data["providers"] == ["groq", "openrouter"]
assert setup_data["disable"] == ["shell", "read_file", "write_file", "edit_file", "list_dir"]
manifest_names = sorted([t["name"] for t in setup_data["tools"]])
expected_five = sorted(["search_files", "list_folder", "open_file", "create_file", "create_folder"])
assert manifest_names == expected_five, f"Expected manifest tools {expected_five}, got {manifest_names}"

# Verify tool schemas match AGENT_TOOLS
endpoint_tool_map = {t["function"]["name"]: t["function"] for t in ez.AGENT_TOOLS}
for t_schema in setup_data["tools"]:
    t_name = t_schema["name"]
    assert t_name in endpoint_tool_map, f"Unexpected tool {t_name}"
    expected_spec = endpoint_tool_map[t_name]
    assert t_schema["description"] == expected_spec["description"], f"Description mismatch for {t_name}"
    assert t_schema["parameters"] == expected_spec["parameters"], f"Parameters mismatch for {t_name}"

# Verify task received
task_file = os.path.join(fake_harness_root, "task_received.json")
assert os.path.exists(task_file), "Runner should have received task"
with open(task_file, "r", encoding="utf-8") as f:
    task_data = json.load(f)
assert task_data["text"] == "hello harness"
assert task_data.get("mode") == "single", f"Expected mode 'single', got {task_data.get('mode')}"
# Step 2: verify context field when no folder is open
assert task_data.get("context") == {"current_folder": None}, f"Expected null current_folder, got: {task_data.get('context')}"

# Verify transcript
transcript_txt = spot.transcript.toPlainText()
assert "canned response from fake harness" in transcript_txt, f"Canned text missing from transcript: {transcript_txt}"
assert "Tokens: 42.0" in transcript_txt, f"Token budget missing: {transcript_txt}"
assert "Time: 123.45ms" in transcript_txt, f"Latency missing: {transcript_txt}"
assert "Tools: EzExp catalog + grant tools via the harness (native harness tools disabled)." in transcript_txt, f"Case A banner missing: {transcript_txt}"

# Step 2: verify context field when folder is open
remove_control_files()
set_control({"mode": "happy"})
spot.current_folder = r"D:\games\gta 5"
spot.transcript.clear()
spot.edit.setText("test folder context")
spot.send_agent_message()
assert spot._bridge_worker is not None
while spot._bridge_worker.isRunning():
    qapp.processEvents()
spot._bridge_worker.wait(5000)
qapp.processEvents()

with open(task_file, "r", encoding="utf-8") as f:
    task_data_folder = json.load(f)
assert task_data_folder.get("context") == {"current_folder": os.path.abspath(r"D:\games\gta 5")}, f"Unexpected context: {task_data_folder.get('context')}"
spot.current_folder = None

# 4. Fail-closed tool set assertion (sabotaged runner emitting shell tool)
remove_control_files()
set_control({"mode": "sabotage"})
spot.transcript.clear()
spot.edit.setText("test sabotage")
spot.send_agent_message()
assert spot._bridge_worker is not None
while spot._bridge_worker.isRunning():
    qapp.processEvents()
spot._bridge_worker.wait(5000)
qapp.processEvents()

# Run should have been refused BEFORE task execution
refusal_transcript = spot.transcript.toPlainText()
assert "Refused to run: harness tool set mismatch — expected EzExp tools only, got:" in refusal_transcript, f"Refusal line missing: {refusal_transcript}"
assert "shell" in refusal_transcript, f"Sabotaged tool 'shell' should be mentioned in refusal message: {refusal_transcript}"
assert not os.path.exists(os.path.join(fake_harness_root, "task_received.json")), "Task line must NOT have been sent on refusal"

# 5. Real tool dispatch through bridge
remove_control_files()
set_control({
    "mode": "happy",
    "tool_call": {"name": "search_files", "arguments": {"query": "test"}}
})
spot.transcript.clear()
spot.edit.setText("run real search")
spot.send_agent_message()
assert spot._bridge_worker is not None
while spot._bridge_worker.isRunning():
    qapp.processEvents()
spot._bridge_worker.wait(5000)
qapp.processEvents()

tool_res_file = os.path.join(fake_harness_root, "tool_result_received.json")
assert os.path.exists(tool_res_file), "Tool result should have been received by runner"
with open(tool_res_file, "r", encoding="utf-8") as f:
    tool_res = json.load(f)
assert tool_res["ok"] is True, f"search_files failed: {tool_res}"
assert isinstance(tool_res["output"], str)

# 6. Confirmer gates create_file: Deny vs Allow
# 6a. Deny
spot.agent_client.confirmer = lambda text: False
deny_rel_path = "denied_file.txt"
remove_control_files()
set_control({
    "mode": "happy",
    "tool_call": {"name": "create_file", "arguments": {"rel_path": deny_rel_path, "content": "should be blocked"}}
})
spot.transcript.clear()
spot.edit.setText("create denied file")
spot.send_agent_message()
assert spot._bridge_worker is not None
while spot._bridge_worker.isRunning():
    qapp.processEvents()
spot._bridge_worker.wait(5000)
qapp.processEvents()
assert not os.path.exists(os.path.join(grant_temp, deny_rel_path)), "Denied file should NOT exist on disk"
tool_res_file = os.path.join(fake_harness_root, "tool_result_received.json")
with open(tool_res_file, "r", encoding="utf-8") as f:
    tool_res = json.load(f)
assert tool_res["ok"] is False, "Declined tool call should return ok=False"
assert tool_res["output"] == "user declined", f"Expected 'user declined', got: {tool_res}"

# 6b. Allow
spot.agent_client.confirmer = lambda text: True
allow_rel_path = "allowed_file.txt"
remove_control_files()
set_control({
    "mode": "happy",
    "tool_call": {"name": "create_file", "arguments": {"rel_path": allow_rel_path, "content": "approved content 123"}}
})
spot.transcript.clear()
spot.edit.setText("create allowed file")
spot.send_agent_message()
assert spot._bridge_worker is not None
while spot._bridge_worker.isRunning():
    qapp.processEvents()
spot._bridge_worker.wait(5000)
qapp.processEvents()
allowed_full_path = os.path.join(grant_temp, allow_rel_path)
assert os.path.exists(allowed_full_path), "Allowed file should exist on disk"
with open(allowed_full_path, "r", encoding="utf-8") as f:
    created_content = f.read()
assert created_content == "approved content 123", f"Created content mismatch: {created_content}"
tool_res_file = os.path.join(fake_harness_root, "tool_result_received.json")
with open(tool_res_file, "r", encoding="utf-8") as f:
    tool_res = json.load(f)
assert tool_res["ok"] is True

# 7. Failure honesty (Setup error)
remove_control_files()
set_control({"mode": "error", "error_message": "simulated harness explosion"})
spot.transcript.clear()
spot.edit.setText("test failure")
spot.send_agent_message()
assert spot._bridge_worker is not None
while spot._bridge_worker.isRunning():
    qapp.processEvents()
spot._bridge_worker.wait(5000)
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

    # 8d. Real BridgeWorker with production bridge (no injected confirmer)
    spot.agent_client.confirmer = None
    ez.QMessageBox.question = stub_question_yes
    bridge_rel_path = "bridge_approved.txt"
    remove_control_files()
    set_control({
        "mode": "happy",
        "tool_call": {"name": "create_file", "arguments": {"rel_path": bridge_rel_path, "content": "bridge content"}}
    })
    spot.transcript.clear()
    spot.edit.setText("test bridge worker")
    spot.send_agent_message()
    assert spot._bridge_worker is not None
    while spot._bridge_worker.isRunning():
        qapp.processEvents()
    spot._bridge_worker.wait(5000)
    qapp.processEvents()

    assert os.path.exists(os.path.join(grant_temp, bridge_rel_path)), "File should exist when bridge approved"
    with open(os.path.join(grant_temp, bridge_rel_path), "r", encoding="utf-8") as f:
        content_br = f.read()
    assert content_br == "bridge content"
    tool_res_file = os.path.join(fake_harness_root, "tool_result_received.json")
    with open(tool_res_file, "r", encoding="utf-8") as f:
        tool_res = json.load(f)
    assert tool_res["ok"] is True

finally:
    ez.QMessageBox.question = _orig_qmessagebox_question

# ====================================================================
# ====================================================================
# 9. Construction smoke test for all dialog and widget classes
# ====================================================================
# Complete enumeration of all dialog and top-level widget classes defined in app.py:
# - AgentSettingsDialog: QDialog subclass (only QDialog subclass in app.py)
# - MainWindow: FramelessWindow / QWidget top-level window
# - Spotlight: FramelessWindow / QWidget top-level window
# - FramelessWindow: QWidget top-level base window
#
# Component and UI widget classes:
# - TitleBar: QFrame subclass
# - SearchField: QFrame subclass
# - ResultsTree: QTreeWidget subclass
# - DetailsPane: QFrame subclass
# - GraphView: QGraphicsView subclass
# - GraphNode: QGraphicsObject subclass
# - ClickableLabel: QLabel subclass
# - QLaunchButton: QPushButton subclass
#
# Supporting Qt classes:
# - ConfirmBridge: QObject
# - AgentClient: QObject
# - HotkeyFilter: QAbstractNativeEventFilter
# - HarnessWorker: QThread
# - IndexerThread: QThread
# - NameDelegate: QStyledItemDelegate
# - SpotDelegate: QStyledItemDelegate
#
# (No classes excluded; all 19 can be constructed offscreen without live user state.)

from PySide6.QtGui import QMouseEvent  # noqa: E402

# 1. AgentSettingsDialog
smoke_dlg = ez.AgentSettingsDialog()
assert isinstance(smoke_dlg, ez.QDialog)
assert hasattr(smoke_dlg, "combo_backend")
assert smoke_dlg.combo_backend.count() == 2

# 2. FramelessWindow (and exercise QRect resize in mouseMoveEvent)
smoke_fw = ez.FramelessWindow()
assert isinstance(smoke_fw, ez.QWidget)
smoke_fw.setGeometry(100, 100, 400, 300)
smoke_fw._resize_dir = 1
smoke_fw._resize_geo = smoke_fw.geometry()
smoke_fw._resize_gpos = ez.QPointF(100, 100).toPoint()
move_ev = QMouseEvent(
    ez.QEvent.Type.MouseMove,
    ez.QPointF(105, 105),
    ez.QPointF(105, 105),
    ez.Qt.MouseButton.NoButton,
    ez.Qt.MouseButton.NoButton,
    ez.Qt.KeyboardModifier.NoModifier,
)
smoke_fw.mouseMoveEvent(move_ev)
smoke_fw._resize_dir = 0

# 3. MainWindow
smoke_mw = ez.MainWindow(worker)
assert isinstance(smoke_mw, ez.FramelessWindow)
assert smoke_mw.tree is not None
assert smoke_mw.titlebar is not None

# 4. Spotlight
smoke_sp = ez.Spotlight(worker)
assert isinstance(smoke_sp, ez.FramelessWindow)
assert smoke_sp.edit is not None
assert smoke_sp.btn_agent is not None
assert smoke_sp.btn_search is not None
assert smoke_sp.btn_endpoint is not None

# 5. TitleBar
smoke_tb = ez.TitleBar(smoke_fw, with_search=True)
assert isinstance(smoke_tb, ez.QFrame)
assert smoke_tb.win == smoke_fw

# 6. SearchField
smoke_sf = ez.SearchField(None, "Search smoke placeholder")
assert isinstance(smoke_sf, ez.QFrame)
assert smoke_sf.edit.placeholderText() == "Search smoke placeholder"

# 7. ResultsTree
smoke_rt = ez.ResultsTree()
assert isinstance(smoke_rt, ez.QTreeWidget)
assert smoke_rt.columnCount() == 4

# 8. DetailsPane (requires parent widget)
smoke_dp = ez.DetailsPane(smoke_fw)
assert isinstance(smoke_dp, ez.QFrame)
assert smoke_dp.objectName() == "details"

# 9. GraphView
smoke_gv = ez.GraphView()
assert isinstance(smoke_gv, ez.QGraphicsView)
assert smoke_gv.scene() is not None

# 10. GraphNode
smoke_gn = ez.GraphNode(smoke_gv, 0, "Smoke Node", 20.0, "C:\\", "smoke.txt", "C:\\smoke.txt", "doc", False)
assert isinstance(smoke_gn, ez.QGraphicsObject)
assert smoke_gn.label == "Smoke Node"

# 11. ClickableLabel
smoke_cl = ez.ClickableLabel("Smoke Clickable")
assert isinstance(smoke_cl, ez.QLabel)
assert smoke_cl.text() == "Smoke Clickable"

# 12. QLaunchButton (requires path and parent widget)
smoke_qlb = ez.QLaunchButton("C:\\Windows\\notepad.exe", smoke_fw)
assert isinstance(smoke_qlb, ez.QPushButton)
assert smoke_qlb._path == "C:\\Windows\\notepad.exe"

# 13. ConfirmBridge
smoke_cb = ez.ConfirmBridge()
assert isinstance(smoke_cb, ez.QObject)

# 14. AgentClient
smoke_ac = ez.AgentClient()
assert isinstance(smoke_ac, ez.QObject)

# 15. HotkeyFilter
smoke_hf = ez.HotkeyFilter(lambda: None)
assert isinstance(smoke_hf, ez.QAbstractNativeEventFilter)

# 16. HarnessWorker
smoke_hw = ez.HarnessWorker("smoke_task", grant_temp)
assert isinstance(smoke_hw, ez.QThread)

# 17. IndexerThread
smoke_idx = ez.IndexerThread(rebuild=False)
assert isinstance(smoke_idx, ez.QThread)

# 18. NameDelegate
smoke_nd = ez.NameDelegate()
assert isinstance(smoke_nd, ez.QStyledItemDelegate)

# 19. SpotDelegate
smoke_sd = ez.SpotDelegate(smoke_sp)
assert isinstance(smoke_sd, ez.QStyledItemDelegate)

# 20. ProbeWorker
smoke_pw = ez.ProbeWorker()
assert isinstance(smoke_pw, ez.QThread)

# ---------------------------------------------------------------------------
# Part 0 Tests: Probe 30s timeout, async entry, cache, failure-not-cached, dedup
# ---------------------------------------------------------------------------

# 1. slow-probe pass (stub interpreter sleeps ~6s, succeeds under 30s limit)
stub_probe_bat = os.path.join(tempfile.mkdtemp(prefix="ez_probe_"), "stub_py.bat")
with open(stub_probe_bat, "w", encoding="utf-8") as f:
    f.write(f'@echo off\n"{sys.executable}" -c "import time; time.sleep(6)"\nexit /b 0\n')
temp_probe_harness_dir = tempfile.mkdtemp(prefix="ez_probe_dir_")

ez.clear_harness_probe_cache()
t0 = time.time()
ok_slow, err_slow = ez.harness_available({"backend": "harness", "harness_path": temp_probe_harness_dir, "harness_python": stub_probe_bat})
probe_elapsed = time.time() - t0
assert ok_slow is True, f"Expected slow probe to succeed within 30s, got err: {err_slow}"
assert probe_elapsed >= 5.5, f"Expected sleep ~6s, got {probe_elapsed:.2f}s"

# 2. async entry (< 2s return, ready after landing)
ez.clear_harness_probe_cache()
async_cfg = {
    "backend": "harness",
    "harness_path": temp_probe_harness_dir,
    "harness_python": stub_probe_bat,
    "grant_dir": temp_probe_harness_dir,
}
test_spot_async = ez.Spotlight(worker)
test_spot_async.agent_client.config = async_cfg
t_entry = time.time()
test_spot_async.set_mode("agent")
entry_duration = time.time() - t_entry
assert entry_duration < 2.0, f"set_mode('agent') took {entry_duration:.2f}s, must be < 2.0s"
assert test_spot_async._probe_in_progress is True
assert "checking" in test_spot_async.transcript.toPlainText().lower()

# Wait for probe worker to finish and verify ready
if test_spot_async._probe_worker is not None:
    test_spot_async._probe_worker.wait(15000)
    QApplication.processEvents()
assert test_spot_async._probe_in_progress is False
assert "ready" in test_spot_async.transcript.toPlainText().lower()

# 3. cache (two entries = one stub invocation)
counter_file = os.path.join(tempfile.mkdtemp(prefix="ez_counter_"), "count.txt")
stub_counter_bat = os.path.join(tempfile.mkdtemp(prefix="ez_counter_bat_"), "stub_counter.bat")
with open(stub_counter_bat, "w", encoding="utf-8") as f:
    f.write(f'@echo off\necho 1>>"{counter_file}"\nexit /b 0\n')

ez.clear_harness_probe_cache()
cache_cfg = {
    "backend": "harness",
    "harness_path": temp_probe_harness_dir,
    "harness_python": stub_counter_bat,
    "grant_dir": temp_probe_harness_dir,
}
test_spot_cache = ez.Spotlight(worker)
test_spot_cache.agent_client.config = cache_cfg
test_spot_cache.set_mode("agent")
if test_spot_cache._probe_worker is not None:
    test_spot_cache._probe_worker.wait(10000)
    QApplication.processEvents()

with open(counter_file, "r", encoding="utf-8") as cf:
    invocations = len(cf.readlines())
assert invocations == 1, f"Expected 1 stub invocation, got {invocations}"

# Second entry: switch to search and back to agent
test_spot_cache.set_mode("search")
test_spot_cache.set_mode("agent")
QApplication.processEvents()
with open(counter_file, "r", encoding="utf-8") as cf:
    invocations2 = len(cf.readlines())
assert invocations2 == 1, f"Expected cache hit (still 1 invocation), got {invocations2}"

# 4. failure-not-cached
ez.clear_harness_probe_cache()
fail_counter_file = os.path.join(tempfile.mkdtemp(prefix="ez_fail_counter_"), "fail_count.txt")
stub_fail_bat = os.path.join(tempfile.mkdtemp(prefix="ez_fail_bat_"), "stub_fail.bat")
with open(stub_fail_bat, "w", encoding="utf-8") as f:
    f.write(f'@echo off\necho fail>>"{fail_counter_file}"\nexit /b 1\n')

ok_f1, err_f1 = ez.harness_available({"backend": "harness", "harness_path": temp_probe_harness_dir, "harness_python": stub_fail_bat})
assert ok_f1 is False
assert len(ez._harness_probe_cache) == 0, "Failed probe must not be cached"

ok_f2, err_f2 = ez.harness_available({"backend": "harness", "harness_path": temp_probe_harness_dir, "harness_python": stub_fail_bat})
assert ok_f2 is False
with open(fail_counter_file, "r", encoding="utf-8") as fcf:
    fail_invocations = len(fcf.readlines())
assert fail_invocations == 2, f"Expected 2 invocations since failures are not cached, got {fail_invocations}"

# 5. dedup (two sends while not-ready = one status line)
ez.clear_harness_probe_cache()
notready_spot = ez.Spotlight(worker)
notready_spot.agent_client.config = {
    "backend": "endpoint",
    "api_key": "",
}
notready_spot.set_mode("agent")
initial_text = notready_spot.transcript.toPlainText().strip()
assert "Setup required: No API key configured" in initial_text

notready_spot.edit.setText("test message 1")
notready_spot.send_agent_message()
after_send1 = notready_spot.transcript.toPlainText().strip()
lines1 = [l.strip() for l in after_send1.splitlines() if l.strip()]
assert len(lines1) == 1, f"Expected 1 line after send 1, got {len(lines1)}: {lines1}"

notready_spot.edit.setText("test message 2")
notready_spot.send_agent_message()
after_send2 = notready_spot.transcript.toPlainText().strip()
lines2 = [l.strip() for l in after_send2.splitlines() if l.strip()]
assert len(lines2) == 1, f"Expected 1 line after send 2 (deduped), got {len(lines2)}: {lines2}"
assert lines2[0] == lines1[0]

# 6. Step 2 (v1.2): Spotlight cleanup check (NO workspace pane / splitter)
ws_spot = ez.Spotlight(worker)
ws_spot.set_mode("agent")
assert not hasattr(ws_spot, "workspace_pane"), "Spotlight must not have workspace_pane"
assert not hasattr(ws_spot, "agent_splitter"), "Spotlight must not have agent_splitter"
assert not hasattr(ws_spot, "btn_toggle_workspace"), "Spotlight must not have btn_toggle_workspace"
assert not ws_spot.transcript.isHidden(), "Spotlight transcript should be visible in agent mode"
ws_spot.set_mode("search")
assert ws_spot.transcript.isHidden()
assert not ws_spot.tree.isHidden()

# 7. Step 2 (v1.2): MainWindow Agent dock & workspace tests
main_win = ez.MainWindow(worker)
main_win.show()

# (a) Dock exists in MainWindow, hidden by default; toggle shows/hides it
assert main_win.agent_dock is not None, "agent_dock must exist in MainWindow"
# Fresh settings simulation
ez.QSettings("ezexp", "EzExp").setValue("agent_dock_visible", "false")
main_win.agent_dock.hide()
main_win.btn_toggle_agent.setChecked(False)
assert main_win.agent_dock.isHidden(), "agent_dock should be hidden by default"

main_win.btn_toggle_agent.click()
assert not main_win.agent_dock.isHidden(), "agent_dock should be visible after toggle click"
assert main_win.btn_toggle_agent.isChecked()

main_win.btn_toggle_agent.click()
assert main_win.agent_dock.isHidden(), "agent_dock should be hidden after toggle click again"
assert not main_win.btn_toggle_agent.isChecked()

# Re-open dock for workspace tests
main_win.toggle_agent_dock(True)
assert not main_win.agent_dock.isHidden()

# (b) Navigating to a folder updates the dock's Context label to that folder
main_win.drill_to_path(r"D:\exp\test_nav_dir", "test_nav_dir")
assert "D:\\exp\\test_nav_dir" in main_win.ws_lbl_context.text(), f"Context not updated: {main_win.ws_lbl_context.text()}"

main_win.current_place = r"D:\games\gta 5"
assert "D:\\games\\gta 5" in main_win.ws_lbl_context.text(), f"Context not updated: {main_win.ws_lbl_context.text()}"

# (c) Grant table lists files; text preview shows content; binary shows the note
test_ws_grant = tempfile.mkdtemp(prefix="ezexp_dock_ws_")
f_txt = os.path.join(test_ws_grant, "sample.txt")
with open(f_txt, "w", encoding="utf-8") as fh:
    fh.write("Hello from sample text file")

f_bin = os.path.join(test_ws_grant, "binary.dat")
with open(f_bin, "wb") as fh:
    fh.write(b"\x00\x01\x02\xff" * 100)

f_big = os.path.join(test_ws_grant, "large.txt")
with open(f_big, "w", encoding="utf-8") as fh:
    fh.write("A" * (250 * 1024))

main_win.agent_client.config = {"grant_dir": test_ws_grant}
main_win.grant_dir = test_ws_grant
main_win.refresh_workspace_pane()

# Context & Grant labels
assert "Context: D:\\games\\gta 5" in main_win.ws_lbl_context.text()
assert test_ws_grant in main_win.ws_lbl_grant.text()

# File list
assert main_win.ws_file_list.rowCount() == 3, f"Expected 3 rows, got {main_win.ws_file_list.rowCount()}"
listed_names = [main_win.ws_file_list.item(r, 0).text() for r in range(main_win.ws_file_list.rowCount())]
assert "sample.txt" in listed_names
assert "binary.dat" in listed_names
assert "large.txt" in listed_names

# Text preview
sample_row = listed_names.index("sample.txt")
main_win.ws_file_list.selectRow(sample_row)
main_win._on_ws_file_selected()
assert main_win.ws_preview.toPlainText() == "Hello from sample text file"

# Binary preview -> note
bin_row = listed_names.index("binary.dat")
main_win.ws_file_list.selectRow(bin_row)
main_win._on_ws_file_selected()
assert main_win.ws_preview.toPlainText() == "binary or too large to preview"

# Oversize preview (>200 KB) -> note
big_row = listed_names.index("large.txt")
main_win.ws_file_list.selectRow(big_row)
main_win._on_ws_file_selected()
assert main_win.ws_preview.toPlainText() == "binary or too large to preview"

# Refresh button picks up newly created file
f_new = os.path.join(test_ws_grant, "added_later.py")
with open(f_new, "w", encoding="utf-8") as fh:
    fh.write("print('new code')")

main_win.ws_btn_refresh.click()
assert main_win.ws_file_list.rowCount() == 4
updated_names = [main_win.ws_file_list.item(r, 0).text() for r in range(main_win.ws_file_list.rowCount())]
assert "added_later.py" in updated_names
new_row = updated_names.index("added_later.py")
main_win.ws_file_list.selectRow(new_row)
main_win._on_ws_file_selected()
assert main_win.ws_preview.toPlainText() == "print('new code')"

# (d) Task sent from the dock carries context.current_folder = the navigated folder
remove_control_files()
set_control({"mode": "happy"})
main_win.agent_client.config = {
    "backend": "harness",
    "harness_path": fake_harness_root,
    "harness_python": "py",
    "grant_dir": test_ws_grant,
}
main_win.current_place = r"D:\games\gta 5"
main_win.dock_transcript.clear()
main_win.dock_input.setText("test folder context from dock")
main_win.send_agent_message()

t0 = time.time()
while main_win._harness_worker is not None and main_win._harness_worker.isRunning():
    qapp.processEvents()
    time.sleep(0.05)
    if time.time() - t0 > 10:
        break

task_file = os.path.join(fake_harness_root, "task_received.json")
assert os.path.exists(task_file), "Runner should have received task from dock"
with open(task_file, "r", encoding="utf-8") as f:
    task_data_dock = json.load(f)
assert task_data_dock.get("context") == {"current_folder": os.path.abspath(r"D:\games\gta 5")}, f"Unexpected context from dock: {task_data_dock.get('context')}"
assert "test folder context from dock" in task_data_dock.get("text", "")

# (e) T1: Three-state honest context semantics
# State 1: Drilled into a folder -> that folder
main_win.current_place = r"D:\games\gta 5"
assert main_win.get_current_folder() == os.path.abspath(r"D:\games\gta 5")
assert main_win.ws_lbl_context.text() == f"Context: {os.path.abspath(r'D:\games\gta 5')}"

# State 2: Library root (All Files) WITH a folder selected -> the selected folder is the context
main_win.current_place = None
folder_row = {
    "parent": r"C:\Windows",
    "name": "Temp",
    "is_dir": 1,
    "size": 0,
    "mtime": 1700000000,
    "category": "folder",
    "kind": "Folder",
}
file_row = {
    "parent": r"C:\Windows",
    "name": "notepad.exe",
    "is_dir": 0,
    "size": 12345,
    "mtime": 1700000000,
    "category": "prog",
    "kind": "Application",
}
main_win.tree.clear()
item_folder = ez.make_row_item(folder_row)
item_file = ez.make_row_item(file_row)
main_win.tree.addTopLevelItem(item_folder)
main_win.tree.addTopLevelItem(item_file)

main_win.tree.setCurrentItem(item_folder)
qapp.processEvents()

expected_folder_ctx = os.path.abspath(r"C:\Windows\Temp")
assert main_win.get_current_folder() == expected_folder_ctx, f"Expected {expected_folder_ctx}, got {main_win.get_current_folder()}"
assert main_win.ws_lbl_context.text() == f"Context: {expected_folder_ctx}", f"Expected Context: {expected_folder_ctx}, got {main_win.ws_lbl_context.text()}"

# Payload check for State 2: fake-harness pattern
remove_control_files()
set_control({"mode": "happy"})
main_win.dock_transcript.clear()
main_win.dock_input.setText("test folder context from selected folder")
main_win.send_agent_message()

t0 = time.time()
while main_win._harness_worker is not None and main_win._harness_worker.isRunning():
    qapp.processEvents()
    time.sleep(0.05)
    if time.time() - t0 > 10:
        break

task_file = os.path.join(fake_harness_root, "task_received.json")
assert os.path.exists(task_file), "Runner should have received task from dock"
with open(task_file, "r", encoding="utf-8") as f:
    task_data_sel = json.load(f)
assert task_data_sel.get("context") == {"current_folder": expected_folder_ctx}, f"Unexpected context from selection: {task_data_sel.get('context')}"

# State 3: Library root with nothing (or a file) selected -> context stays none, label says Context: (no folder open)
# Subcase 3a: File selected
main_win.tree.setCurrentItem(item_file)
qapp.processEvents()
assert main_win.get_current_folder() is None, f"Expected None for file selection, got {main_win.get_current_folder()}"
assert main_win.ws_lbl_context.text() == "Context: (no folder open)", f"Expected Context: (no folder open), got {main_win.ws_lbl_context.text()}"

# Subcase 3b: Nothing selected
main_win.tree.setCurrentItem(None)
main_win.details.show_row(None)
main_win.on_selection()
qapp.processEvents()
assert main_win.get_current_folder() is None, f"Expected None for no selection, got {main_win.get_current_folder()}"
assert main_win.ws_lbl_context.text() == "Context: (no folder open)", f"Expected Context: (no folder open), got {main_win.ws_lbl_context.text()}"

# Payload check for State 3:
remove_control_files()
set_control({"mode": "happy"})
main_win.dock_transcript.clear()
main_win.dock_input.setText("test folder context with no folder open")
main_win.send_agent_message()

t0 = time.time()
while main_win._harness_worker is not None and main_win._harness_worker.isRunning():
    qapp.processEvents()
    time.sleep(0.05)
    if time.time() - t0 > 10:
        break

task_file = os.path.join(fake_harness_root, "task_received.json")
assert os.path.exists(task_file), "Runner should have received task from dock"
with open(task_file, "r", encoding="utf-8") as f:
    task_data_none = json.load(f)
assert task_data_none.get("context") == {"current_folder": None}, f"Unexpected context when no folder open: {task_data_none.get('context')}"

# (f) T2: Dock chat light theme styling (guard against near-black regression)
# Old values: dock_transcript.styleSheet() == "" and dock_input.styleSheet() == ""
# (unstyled widgets rendered near-black background with dark grey text in Windows dark theme)
assert main_win.dock_transcript.styleSheet() != "", "dock_transcript styleSheet must not be empty (old unstyled value was '')"
assert main_win.dock_input.styleSheet() != "", "dock_input styleSheet must not be empty (old unstyled value was '')"
assert "#ffffff" in main_win.dock_transcript.styleSheet().lower(), "dock_transcript must have light #ffffff background"
assert "#1d1d1f" in main_win.dock_transcript.styleSheet().lower(), "dock_transcript must have dark #1d1d1f text"
assert "#ffffff" in main_win.dock_input.styleSheet().lower(), "dock_input must have light #ffffff background"
assert "#1d1d1f" in main_win.dock_input.styleSheet().lower(), "dock_input must have dark #1d1d1f text"


_test_settings.remove('agent_dock_visible')
_test_settings.sync()
worker.stop()
worker.wait(3000)
print("ALL TESTS PASSED")
sys.stdout.flush()
os._exit(0)
