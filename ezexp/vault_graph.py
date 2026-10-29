import os
import re
import math
import time
from collections import deque

from ezexp.fsutil import (
    join_path, split_path, category_for, drive_label, fixed_drives,
    human_size, like_escape
)
from ezexp.settings_store import get_vaults
from ezexp.db import db_connect

WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")

def parse_wikilinks(text):
    """Extract wikilink targets: [[Note]], [[Note|alias]], [[Note#Heading]]."""
    targets = []
    for m in WIKILINK_RE.finditer(text):
        t = m.group(1).split("|", 1)[0].split("#", 1)[0].split("^", 1)[0].strip()
        if t:
            targets.append(t)
    return targets

def active_vault_for(place):
    """The saved vault that contains this place (or None)."""
    if not place:
        return None
    p = place.rstrip("\\").lower()
    for v in get_vaults():
        vp = v.rstrip("\\")
        vl = vp.lower()
        if p == vl or p.startswith(vl + "\\"):
            return vp
    return None

def pending_link_count(conn, vaults):
    """Markdown notes whose [[wikilinks]] have not been parsed yet (or whose
    mtime changed since they were). Empty list → 0."""
    vaults = [v.rstrip("\\") for v in (vaults or []) if v]
    if not vaults:
        return 0
    cond_parts, args = [], []
    for v in vaults:
        cond_parts.append("(f.parent = ? COLLATE NOCASE OR f.parent LIKE ? ESCAPE '#')")
        args += [v, like_escape(v) + "\\%"]
    row = conn.execute(
        "SELECT COUNT(*) FROM files f LEFT JOIN note_links l"
        " ON l.parent = f.parent COLLATE NOCASE AND l.name = f.name COLLATE NOCASE"
        " AND l.mtime = f.mtime"
        " WHERE f.ext = 'md' AND l.name IS NULL AND (" + " OR ".join(cond_parts) + ")",
        args).fetchone()
    return row[0] if row else 0

def analyze_pending_links(conn, vaults, time_budget=6.0, progress=None):
    """Parse [[wikilinks]] out of not-yet-analyzed markdown notes and store
    them in note_links. Runs at most time_budget seconds per call (cloud
    placeholders can be slow); unchanged notes are never re-read. A note with
    zero links gets an empty-target marker row so it is not re-read forever.
    Returns (analyzed, still_pending)."""
    vaults = [v.rstrip("\\") for v in (vaults or []) if v]
    if not vaults:
        return 0, 0
    cond_parts, args = [], []
    for v in vaults:
        cond_parts.append("(f.parent = ? COLLATE NOCASE OR f.parent LIKE ? ESCAPE '#')")
        args += [v, like_escape(v) + "\\%"]
    rows = conn.execute(
        "SELECT f.parent, f.name, f.mtime FROM files f LEFT JOIN note_links l"
        " ON l.parent = f.parent COLLATE NOCASE AND l.name = f.name COLLATE NOCASE"
        " AND l.mtime = f.mtime"
        " WHERE f.ext = 'md' AND l.name IS NULL AND (" + " OR ".join(cond_parts) + ")",
        args).fetchall()
    remaining = len(rows)
    t0 = time.time()
    analyzed = 0
    for parent, name, mtime in rows:
        if time.time() - t0 > time_budget:
            break
        targets = []
        try:
            with open(join_path(parent, name), "rb") as f:
                text = f.read(512_000).decode("utf-8", "replace")
            targets = parse_wikilinks(text)
        except OSError:
            # unreadable right now (cloud placeholder etc.) — stays pending
            if progress and analyzed % 25 == 0:
                progress(analyzed, remaining)
            continue
        conn.execute("DELETE FROM note_links WHERE parent=? AND name=?", (parent, name))
        conn.executemany(
            "INSERT INTO note_links VALUES(?,?,?,?)",
            [(parent, name, mtime, t) for t in targets] or [(parent, name, mtime, "")])
        analyzed += 1
        remaining -= 1
        if progress and analyzed % 25 == 0:
            progress(analyzed, remaining)
    conn.commit()
    return analyzed, remaining

def build_vault_graph(conn, vault, max_nodes=4000):
    """Obsidian-style graph with progressive disclosure: the displayed tree
    is the vault root + its top-level folders (bubbles, sized by note count)
    + notes that live at the root; deeper folders/notes arrive on demand via
    the child provider. link_edges holds resolved [src_path, dst_path] note
    links from the note_links table — no file reads, instant."""
    vp = vault.rstrip("\\")
    scope = "(f.parent = ? COLLATE NOCASE OR f.parent LIKE ? ESCAPE '#')"
    sargs = [vp, like_escape(vp) + "\\%"]
    rows = conn.execute(
        f"SELECT f.parent, f.name FROM files f WHERE {scope} AND f.ext = 'md'",
        sargs).fetchall()
    total = len(rows)
    rows = rows[:max_nodes]
    nodes = []
    for parent, name in rows:
        path = join_path(parent, name)
        nodes.append({
            "name": name[:-3] if name.lower().endswith(".md") else name,
            "file": name,
            "parent": parent, "path": path,
        })

    by_base, by_rel = {}, {}
    note_paths = []
    for i, (parent, name) in enumerate(rows):
        path = join_path(parent, name)
        note_paths.append(path)
        stem = name[:-3] if name.lower().endswith(".md") else name
        by_base.setdefault(stem.lower(), []).append(i)
        rel = os.path.relpath(path, vp)
        rstem = rel[:-3] if rel.lower().endswith(".md") else rel
        by_rel.setdefault(rel.lower(), []).append(i)
        by_rel.setdefault(rstem.lower(), []).append(i)

    def resolve(target, cur_parent):
        t = target.replace("/", "\\").strip().lower()
        if t.endswith(".md"):
            t = t[:-3]
        for key in (t, t + ".md"):
            lst = by_rel.get(key)
            if lst:
                for i in lst:  # same-folder notes win on ambiguous names
                    if note_paths[i].rsplit("\\", 1)[0].lower() == cur_parent.lower():
                        return i
                return lst[0]
        lst = by_base.get(t)
        if lst:
            for i in lst:
                if note_paths[i].rsplit("\\", 1)[0].lower() == cur_parent.lower():
                    return i
            return lst[0]
        return None

    link_edges = []
    seen = set()
    link_rows = conn.execute(
        "SELECT l.parent, l.name, l.target FROM note_links l JOIN files f"
        " ON f.parent = l.parent COLLATE NOCASE AND f.name = l.name COLLATE NOCASE"
        " WHERE l.target != '' AND f.ext = 'md' AND " + scope, sargs).fetchall()
    for lparent, lname, ltarget in link_rows:
        sp = join_path(lparent, lname).lower()
        j = resolve(ltarget, lparent)
        if j is None:
            continue
        dp = note_paths[j].lower()
        if sp == dp:
            continue
        key = tuple(sorted((sp, dp)))
        if key in seen:
            continue
        seen.add(key)
        link_edges.append([sp, dp])
    degree = {}
    for a, b in link_edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1

    # displayed tree: vault root + top-level folder bubbles + root notes
    nodes = [{"label": vp.rsplit("\\", 1)[-1] or vp, "kind": "folder",
              "category": "folder",
              "weight": total, "sub": f"{total:,} notes", "path": vp,
              "parent_path": None, "file": None, "has_children": True}]
    edges = []
    root_idx = 0
    for (name,) in conn.execute(
            "SELECT name FROM files WHERE parent = ? COLLATE NOCASE AND is_dir = 1"
            " ORDER BY name COLLATE NOCASE", (vp,)).fetchall():
        full = join_path(vp, name)
        fl = full.lower()
        n = sum(1 for p in note_paths if p.lower().startswith(fl + "\\"))
        nodes.append({"label": name, "kind": "folder", "category": "folder",
                      "weight": n, "sub": f"{n:,} notes", "path": full,
                      "parent_path": vp, "file": name, "has_children": n > 0})
        edges.append([root_idx, len(nodes) - 1])
    for parent, name in rows:
        if parent.lower() != vp.lower():
            continue
        stem = name[:-3] if name.lower().endswith(".md") else name
        p = join_path(parent, name)
        d = degree.get(p.lower(), 0)
        nodes.append({"label": stem, "kind": "note", "category": "doc",
                      "weight": d, "sub": f"{d} links" if d else None, "path": p,
                      "parent_path": vp, "file": name, "has_children": False})
        edges.append([root_idx, len(nodes) - 1])
    return {"nodes": nodes, "edges": edges, "mode": "vault",
            "link_edges": link_edges, "total_md": total, "vault": vp,
            "analyzed": total - pending_link_count(conn, [vp])}

def build_structure_graph(conn, place=None, roots=None, max_nodes=2500):
    """Folder-containment graph built purely from the index (no file reads,
    so it is instant even for whole drives). place=None → one node per fixed
    drive with two levels of folders; otherwise the place's own subtree,
    three levels deep, with its direct files as leaf nodes. Node 'weight' is
    the number of indexed items in the folder's subtree."""
    if place:
        roots = [place.rstrip("\\")]
        depth_cap = 3
    else:
        roots = [r.rstrip("\\") for r in (roots or fixed_drives())]
        depth_cap = 2

    cond_parts, args = [], []
    for r in roots:
        cond_parts.append("(files.parent = ? COLLATE NOCASE OR files.parent LIKE ? ESCAPE '#')")
        args += [r, like_escape(r) + "\\%"]
    where = "(" + " OR ".join(cond_parts) + ")"

    # direct item count per directory
    counts = {}
    for parent, cnt in conn.execute(
            f"SELECT parent, COUNT(*) FROM files WHERE {where} GROUP BY parent COLLATE NOCASE",
            args):
        counts[parent.lower()] = cnt

    kids = {}  # parent_path_lower -> [(name, full_lower, full_orig)]
    orig_of = {r.lower(): r for r in roots}
    for parent, name in conn.execute(
            f"SELECT parent, name FROM files WHERE {where} AND is_dir = 1", args):
        pl = parent.lower()
        full = join_path(parent, name)
        kids.setdefault(pl, []).append((name, full.lower(), full))

    # walk the directory tree (BFS keeps parents before children)
    order = []        # non-root dirs, BFS order (lower-case keys)
    parent_of = {}
    depth_of = {}
    dq = deque()
    for r in roots:
        for name, fl, fo in kids.get(r.lower(), []):
            dq.append((fl, fo, r.lower(), 1))
    seen = set()
    while dq:
        pl, po, par, depth = dq.popleft()
        if pl in seen:
            continue
        seen.add(pl)
        orig_of[pl] = po
        order.append(pl)
        parent_of[pl] = par
        depth_of[pl] = depth
        for name, fl, fo in kids.get(pl, []):
            if fl not in seen:
                dq.append((fl, fo, pl, depth + 1))

    # subtree totals: accumulate deepest-first
    total = {p.lower(): counts.get(p.lower(), 0) for p in roots}
    for pl in order:
        total[pl] = counts.get(pl, 0)
    for pl in reversed(order):
        par = parent_of.get(pl)
        if par is not None:
            total[par] = total.get(par, 0) + total[pl]

    # pick which dirs become nodes: every level-1 dir, then heavier dirs per
    # level until the budget is spent
    kept = {r.lower() for r in roots}
    budget = max_nodes
    by_level = {}
    for pl in order:
        by_level.setdefault(depth_of[pl], []).append(pl)
    for depth in range(1, depth_cap + 1):
        lvl = by_level.get(depth, [])
        lvl.sort(key=lambda pl: -total.get(pl, 0))
        room = max(0, budget - len(kept))
        for pl in lvl[:room]:
            kept.add(pl)
        if len(kept) >= budget:
            break

    # nodes: optional virtual root (multi-drive view), then roots, then kept dirs
    nodes = []
    idx_of = {}
    virtual = None
    if len(roots) > 1:
        tot = sum(total.get(r.lower(), 0) for r in roots)
        nodes.append({"label": "This PC", "kind": "drive", "weight": tot,
                      "sub": f"{tot:,} items", "path": None,
                      "parent_path": None, "file": None, "has_children": True})
        virtual = len(nodes) - 1
    for r in roots:
        rl = r.lower()
        if len(r) == 2:
            label, kind = drive_label(r + "\\"), "drive"
        else:
            label, kind = r.rsplit("\\", 1)[-1] or r, "folder"
        idx_of[rl] = len(nodes)
        nodes.append({"label": label, "kind": kind, "weight": total.get(rl, 0),
                      "sub": f"{total.get(rl, 0):,} items", "path": r,
                      "parent_path": None, "file": None, "has_children": True})
    for depth in range(1, depth_cap + 1):
        for pl in sorted((p for p in kept if depth_of.get(p) == depth),
                         key=lambda p: orig_of[p].lower()):
            po = orig_of[pl]
            idx_of[pl] = len(nodes)
            nodes.append({"label": po.rsplit("\\", 1)[-1], "kind": "folder",
                          "category": "folder",
                          "weight": total.get(pl, 0),
                          "sub": f"{total.get(pl, 0):,} items", "path": po,
                          "parent_path": orig_of.get(parent_of.get(pl, "")),
                          "file": po.rsplit("\\", 1)[-1],
                          "has_children": counts.get(pl, 0) > 0})

    # edges: each kept dir connects to its nearest kept ancestor
    edges = []
    for pl in idx_of:
        if pl in {r.lower() for r in roots}:
            if virtual is not None:
                edges.append((virtual, idx_of[pl]))
            continue
        anc = parent_of.get(pl)
        while anc is not None and anc not in idx_of:
            anc = parent_of.get(anc)
        if anc is not None:
            edges.append((idx_of[anc], idx_of[pl]))

    # direct files of each root as leaf nodes (suppressed on huge trees so
    # drive-wide diagrams stay readable)
    truncated = False
    if len(order) <= 400:
        for r in roots:
            ri = idx_of[r.lower()]
            rows = conn.execute(
                "SELECT name, size FROM files WHERE parent = ? COLLATE NOCASE AND is_dir = 0"
                " ORDER BY name COLLATE NOCASE LIMIT 250", (r,)).fetchall()
            for name, size in rows:
                idx = len(nodes)
                nodes.append({"label": name, "kind": "file",
                              "category": category_for(name, False),
                              "weight": size or 0,
                              "sub": human_size(size), "path": join_path(r, name),
                              "parent_path": r, "file": name, "has_children": False})
                edges.append((ri, idx))
            if len(rows) == 250:
                truncated = True

    return {"nodes": nodes, "edges": edges, "mode": "structure",
            "roots": roots, "truncated_files": truncated}
