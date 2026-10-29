import os
import time

from ezexp.fsutil import like_escape
import sqlite3
from ezexp.db import db_connect, thread_conn, ROW_FIELDS, ROW_FIELDS_F, _row_dict

class _FtsOkProxy:
    def __bool__(self):
        import ezexp.db
        return bool(ezexp.db.FTS_OK)
    def __repr__(self):
        import ezexp.db
        return repr(ezexp.db.FTS_OK)
    def __eq__(self, other):
        import ezexp.db
        return ezexp.db.FTS_OK == other

FTS_OK = _FtsOkProxy()

def sort_rows(rows, mode):
    """Client-side ordering for the list view (fetch is capped at 600)."""
    if mode == "name":
        return sorted(rows, key=lambda r: (not r["is_dir"],
                                           r["name"].lower()))
    if mode == "newest":
        return sorted(rows, key=lambda r: (-(r["mtime"] or 0),
                                           r["name"].lower()))
    if mode == "oldest":
        return sorted(rows, key=lambda r: ((r["mtime"] or 0),
                                           r["name"].lower()))
    if mode == "largest":
        return sorted(rows, key=lambda r: (r["is_dir"],
                                           -(r["size"] or 0),
                                           r["name"].lower()))
    if mode == "smallest":
        return sorted(rows, key=lambda r: (r["is_dir"],
                                           (r["size"] or 0),
                                           r["name"].lower()))
    return rows

def do_search(conn, query, place=None, cat="all", limit=600):
    """Returns (rows, note). Empty query = browse the current place by date."""
    q = (query or "").strip()
    conds, args = [], []
    if place:
        p = place.rstrip("\\")
        if q:
            # searching within a place: everything beneath it
            conds.append("(files.parent = ? COLLATE NOCASE OR files.parent LIKE ? ESCAPE '#')")
            args += [p, like_escape(p) + "\\%"]
        else:
            # browsing a place: its direct children only
            conds.append("files.parent = ? COLLATE NOCASE")
            args.append(p)
    if cat and cat != "all":
        conds.append("files.category = ?")
        args.append(cat)
    base = " FROM files"
    if q and FTS_OK and len(q) >= 3:
        base = " FROM fts JOIN files ON files.id = fts.rowid"
    conds_sql = (" WHERE " + " AND ".join(conds)) if conds else ""

    rows = []
    FETCH_CAP = 20000  # hard ceiling before in-Python ranking
    if q:
        if base.startswith(" FROM fts"):
            m = '"' + q.replace('"', '""') + '"'
            try:
                cur = conn.execute("SELECT " + ROW_FIELDS_F + base + conds_sql +
                                   (" AND " if conds else " WHERE ") + "fts.name MATCH ?" +
                                   " LIMIT ?",
                                   args + [m, FETCH_CAP])
                rows = [_row_dict(r) for r in cur.fetchall()]
            except sqlite3.Error:
                rows = []
            if not rows:
                # trigram found nothing (rare) — fall back to plain LIKE
                base = " FROM files"
                cur = conn.execute("SELECT " + ROW_FIELDS + base + conds_sql +
                                   (" AND " if conds else " WHERE ") +
                                   "files.name LIKE ? ESCAPE '#' LIMIT ?",
                                   args + ["%" + like_escape(q) + "%", FETCH_CAP])
                rows = [_row_dict(r) for r in cur.fetchall()]
        else:
            cur = conn.execute("SELECT " + ROW_FIELDS + base + conds_sql +
                               (" AND " if conds else " WHERE ") +
                               "files.name LIKE ? ESCAPE '#' LIMIT ?",
                               args + ["%" + like_escape(q) + "%", FETCH_CAP])
            rows = [_row_dict(r) for r in cur.fetchall()]
    else:
        # browsing a place = its direct children, folders first (Finder-style);
        # browsing All Files = most recently modified across the whole index
        if place:
            order = " ORDER BY files.is_dir DESC, files.name COLLATE NOCASE ASC LIMIT ?"
        else:
            order = " ORDER BY files.mtime DESC LIMIT ?"
        cur = conn.execute("SELECT " + ROW_FIELDS + base + conds_sql + order,
                           args + [limit])
        rows = [_row_dict(r) for r in cur.fetchall()]

    if q:
        def rank(r):
            n = r["name"].lower()
            if n == q.lower():
                g = 0
            elif n.startswith(q.lower()):
                g = 1
            else:
                g = 2
            return (g, len(n), -(r["mtime"] or 0))
        rows.sort(key=rank)
        truncated = len(rows) >= limit
        rows = rows[:limit]
        note = "search"
    else:
        truncated = len(rows) >= limit
        note = "browse"
    return rows, ("truncated" if truncated else "")
