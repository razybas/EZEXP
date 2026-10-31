import os
import time
import sqlite3
import threading

from ezexp import APP_DIR, DB_DIR, DB_PATH
from ezexp.fsutil import join_path, split_path, like_escape, category_for, kind_for

FTS_OK = False

_tls = threading.local()

def db_connect():
    os.makedirs(DB_DIR, exist_ok=True)
    deadline = time.time() + 180
    while True:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-65536")
            conn.execute("PRAGMA busy_timeout=30000")
            _ensure_schema(conn)
            return conn
        except sqlite3.OperationalError as exc:
            try:
                conn.close()
            except Exception:
                pass
            if "locked" not in str(exc) or time.time() > deadline:
                raise
            time.sleep(1)

def thread_conn():
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = db_connect()
        _tls.conn = conn
    return conn

def _ensure_schema(conn):
    """Tables and the UNIQUE index only — this runs on EVERY
    connection, so it must stay instant. The performance indexes are
    built once by ensure_indexes() (indexer thread): building one
    over millions of rows here would hold the write lock while every
    other connection queues behind it and dies on 'database is
    locked'."""
    global FTS_OK
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY,
            parent TEXT NOT NULL,
            name TEXT NOT NULL,
            is_dir INTEGER NOT NULL,
            ext TEXT,
            size INTEGER,
            mtime REAL,
            drive TEXT,
            category TEXT,
            kind TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_files_key ON files(parent, name);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS note_links(
            parent TEXT NOT NULL,
            name TEXT NOT NULL,
            mtime REAL NOT NULL,
            target TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS frn_map(
            drive TEXT NOT NULL,
            frn INTEGER NOT NULL,
            parent_frn INTEGER,
            name TEXT,
            PRIMARY KEY(drive, frn)
        );
    """)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5("
            "name, content='files', content_rowid='id', tokenize='trigram')"
        )
        conn.execute("SELECT count(*) FROM fts")
        FTS_OK = True
    except sqlite3.Error:
        FTS_OK = False
    conn.commit()

def ensure_indexes(conn):
    """Performance indexes, built once per database by the indexer
    thread before it scans. IF NOT EXISTS keeps later calls cheap."""
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent);
        CREATE INDEX IF NOT EXISTS idx_files_parent_nc ON files(parent COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_files_drive ON files(drive);
        CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime);
        CREATE INDEX IF NOT EXISTS idx_files_cat ON files(category);
        CREATE INDEX IF NOT EXISTS idx_links_key ON note_links(parent, name);
    """)
    conn.commit()

def rebuild_fts(conn):
    global FTS_OK
    if not FTS_OK:
        return
    conn.execute("DROP TABLE IF EXISTS fts")
    conn.execute(
        "CREATE VIRTUAL TABLE fts USING fts5("
        "name, content='files', content_rowid='id', tokenize='trigram')"
    )
    conn.execute("INSERT INTO fts(rowid, name) SELECT id, name FROM files")

def fts_touch(conn, rowid, name):
    if not FTS_OK:
        return
    conn.execute("DELETE FROM fts WHERE rowid=?", (rowid,))
    conn.execute("INSERT INTO fts(rowid, name) VALUES(?,?)", (rowid, name))

def fts_rename(conn, rowid, old_name, new_name):
    """Re-index a renamed row. The plain DELETE in fts_touch reads the
    row's CURRENT content from files, which after the rename no longer
    matches the indexed tokens: the old name's tokens stay forever and
    the delete targets tokens never inserted. FTS5's explicit 'delete'
    command unindexes the SUPPLIED old value without touching the
    content table, so it is correct on either side of the row move."""
    if not FTS_OK:
        return
    conn.execute("INSERT INTO fts(fts, rowid, name) VALUES('delete', ?, ?)",
                 (rowid, old_name))
    conn.execute("INSERT INTO fts(rowid, name) VALUES(?,?)", (rowid, new_name))

def fts_drop_ids(conn, ids):
    if not FTS_OK or not ids:
        return
    conn.executemany("DELETE FROM fts WHERE rowid=?", [(i,) for i in ids])

def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, str(value)))

def update_total(conn):
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    set_meta(conn, "total", total)
    conn.commit()
    return total

def insert_rows(conn, rows, sync_fts=True):
    """rows: (parent, name, is_dir, ext, size, mtime, drive, category, kind)"""
    if not rows:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO files(parent,name,is_dir,ext,size,mtime,drive,category,kind)"
        " VALUES(?,?,?,?,?,?,?,?,?)", rows)
    if sync_fts and FTS_OK:
        conn.executemany(
            "INSERT INTO fts(rowid, name)"
            " SELECT id, name FROM files WHERE parent=? AND name=?",
            [(r[0], r[1]) for r in rows])
        # De-duplicate defensively: a rowid that somehow got indexed twice.
        conn.execute("DELETE FROM fts WHERE rowid NOT IN (SELECT id FROM files)")

def delete_tree(conn, path):
    """Remove an item (file or directory) and all descendants from the index."""
    if len(path) == 2 and path[1] == ":":
        ids = [r[0] for r in conn.execute("SELECT id FROM files WHERE drive=?", (path,))]
        conn.execute("DELETE FROM note_links WHERE parent=? OR parent LIKE ? ESCAPE '#'",
                     (path, like_escape(path) + "\\%"))
    else:
        parent, name = split_path(path)
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM files WHERE parent=? OR parent LIKE ? ESCAPE '#'"
            " OR (parent=? AND name=?)",
            (path, like_escape(path) + "\\%", parent, name))]
        conn.execute(
            "DELETE FROM note_links WHERE (parent=? AND name=?) OR parent=?"
            " OR parent LIKE ? ESCAPE '#'",
            (parent, name, path, like_escape(path) + "\\%"))
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        fts_drop_ids(conn, chunk)
        conn.executemany("DELETE FROM files WHERE id=?", [(c,) for c in chunk])
    conn.commit()

ROW_FIELDS = "parent, name, is_dir, size, mtime, category, kind"

ROW_FIELDS_F = ("files.parent, files.name, files.is_dir, files.size, "
                "files.mtime, files.category, files.kind")

def _row_dict(r):
    return {
        "parent": r[0], "name": r[1], "is_dir": r[2], "size": r[3],
        "mtime": r[4], "category": r[5], "kind": r[6],
    }

def _unique_dest(folder, name):
    """A destination path that never overwrites: 'name (2).ext',
    'name (3).ext', ... (Explorer-style)."""
    dest = join_path(folder, name)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(name)
    n = 2
    while True:
        cand = join_path(folder, f"{stem} ({n}){ext}")
        if not os.path.exists(cand):
            return cand
        n += 1

from ezexp.indexer import _make_row

def index_add_path(conn, path):
    """Insert a file, or a folder and its whole subtree, into the
    index using the scanner's own row builder."""
    st = os.stat(path)
    parent = os.path.dirname(path)
    if not os.path.isdir(path):
        insert_rows(conn, [_make_row(parent, os.path.basename(path),
                                     False, st, path[:2])])
        return
    rows = [_make_row(parent, os.path.basename(path), True, st,
                      path[:2])]
    for base, dirs, files in os.walk(path):
        for nm in dirs + files:
            p = os.path.join(base, nm)
            try:
                s2 = os.stat(p)
            except OSError:
                continue
            rows.append(_make_row(base, nm, os.path.isdir(p), s2,
                                  path[:2]))
    insert_rows(conn, rows)
