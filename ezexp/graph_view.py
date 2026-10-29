import os
import math
import time
import zlib

from PySide6.QtCore import Qt, QPointF, QRectF, QLineF, Signal, QSize, QTimer
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPixmap, QPen, QCursor
)
from PySide6.QtWidgets import (
    QGraphicsItem, QGraphicsLineItem, QGraphicsObject, QGraphicsScene, QGraphicsView,
    QPushButton, QMenu, QLabel
)

from ezexp.fsutil import join_path, split_path, human_size, fmt_date, category_for
from ezexp.vault_graph import build_vault_graph, build_structure_graph, active_vault_for
from ezexp.widgets import place_icon, app_icon, app_icon_for, category_icon

class GraphNode(QGraphicsObject):
    KIND_COLORS = {
        "drive": (QColor("#9aa0a7"), QColor("#787e86")),
        "folder": (QColor("#b9bfc6"), QColor("#989ea6")),
        "file": (QColor("#cdd2d7"), QColor("#b0b6bc")),
        "note": (QColor("#b9bfc6"), QColor("#989ea6")),
    }

    def __init__(self, view, index, label, radius, parent_path, file_name, path,
                 kind="note", sub=None):
        super().__init__()
        self.view = view
        self.index = index
        self.label = label
        self.radius = radius
        self.parent_path = parent_path
        self.file = file_name
        self.path = path
        self.kind = kind
        self.sub = sub
        self.show_label = True
        self.can_expand = False
        self.icon_pm = None
        self.state = 0      # 0 normal, 1 neighbor/hover, 2 selected, 3 dimmed
        self._hover = False
        self._dragging = False
        self._press_pos = None
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setZValue(1)

    def boundingRect(self):
        show_text = self.show_label or self._hover or self.state == 2
        extra = (14 if self.sub else 0) + (20 if show_text else 0) + 10
        return QRectF(-118, -self.radius - 10, 236, self.radius * 2 + 10 + extra)

    def paint(self, p, option, widget):
        st = self.state
        if st == 0 and self._hover:
            st = 1
        if st == 0:
            fill, border = self.KIND_COLORS.get(self.kind, self.KIND_COLORS["note"])
            text = QColor("#6e6e73")
        elif st == 1:
            fill, border, text = QColor("#9ba2a9"), QColor("#7c828a"), QColor("#3a3a3c")
        elif st == 2:
            fill, border, text = QColor("#8b5cf6"), QColor("#7c3aed"), QColor("#6d28d9")
        else:
            fill, border, text = QColor("#dfe2e6"), QColor("#e4e7ea"), QColor("#c7c7cc")
        p.setBrush(fill)
        p.setPen(QPen(border, 1.4))
        p.drawEllipse(QPointF(0, 0), self.radius, self.radius)
        if self.icon_pm is not None and not self.icon_pm.isNull():
            d = self.radius * 2.0
            p.drawPixmap(QRectF(-self.radius, -self.radius, d, d),
                         self.icon_pm, QRectF(self.icon_pm.rect()))
        if self.can_expand:
            bx, by = self.radius * 0.82, -self.radius * 0.82
            p.setBrush(QColor("#8b5cf6"))
            p.setPen(QPen(QColor("#7c3aed"), 1))
            p.drawEllipse(QPointF(bx, by), 6.5, 6.5)
            p.setPen(QPen(QColor("#ffffff"), 1.5))
            p.drawLine(QPointF(bx - 3, by), QPointF(bx + 3, by))
            p.drawLine(QPointF(bx, by - 3), QPointF(bx, by + 3))
        if not (self.show_label or self._hover or self.state == 2):
            return
        fm = QFontMetrics(self.view.label_font)
        p.setPen(QPen(text, 1))
        p.drawText(QRectF(-110, self.radius + 3, 220, 14),
                   Qt.AlignHCenter | Qt.AlignTop,
                   fm.elidedText(self.label, Qt.ElideRight, 200))
        if self.sub:
            p.setPen(QPen(QColor("#a6acb2") if st != 3 else QColor("#d5d8dc"), 1))
            p.drawText(QRectF(-110, self.radius + 16, 220, 12),
                       Qt.AlignHCenter | Qt.AlignTop,
                       fm.elidedText(self.sub, Qt.ElideRight, 200))

    def hoverEnterEvent(self, e):
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._dragging = True
            self._press_pos = e.pos()
            self.view.kick()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        moved = (self._press_pos is not None and e.button() == Qt.LeftButton
                 and (e.pos() - self._press_pos).manhattanLength() > 4)
        self._dragging = False
        self._press_pos = None
        super().mouseReleaseEvent(e)
        if not moved and e.button() == Qt.LeftButton:
            self.view.select_node(self)

    def mouseDoubleClickEvent(self, e):
        self.view.node_activated.emit(self)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and not self.view._simulating:
            self.view.update_edges_for(self.index)
        return super().itemChange(change, value)

    def contextMenuEvent(self, event):
        event.accept()
        self.view.show_node_menu.emit(self, event.screenPos())

class GraphView(QGraphicsView):
    node_clicked = Signal(object)
    node_activated = Signal(object)
    show_node_menu = Signal(object, object)

    CELL = 120.0
    CUTOFF = 300.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing)
        self.setBackgroundBrush(QColor("#ffffff"))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.label_font = QFont()
        self.label_font.setPointSize(8)
        self.nodes = []
        self.edge_items = []   # (line_item, a, b)
        self.edge_pairs = []
        self.vx = []
        self.vy = []
        self.folder_of = []
        self.sel_index = None
        self._mode = "links"
        self._simulating = False
        # structure mode: progressive disclosure bookkeeping
        self.child_provider = None   # set by MainWindow (on-demand expansion)
        self._items = {}             # node idx -> visible GraphNode
        self._visible = set()
        self._tree_children = {}     # idx -> [visible child idxs]
        self._edge_lines = {}        # child idx -> its line to the parent
        self._parent_of = {}         # child idx -> parent idx (visible tree)
        self._data_nodes = []
        self._children_data = {}     # idx -> [child idxs] (built + on-demand)
        self._parent_of_data = {}
        self._data_key = {}          # (parent_lower, file_lower) -> node idx
        self._roots_idx = []
        self._link_src_dst = []      # vault mode: resolved note-link paths
        self._link_lines = []        # vault mode: (line_item, a_idx, b_idx)
        self._pm_cache = {}          # (category, size) -> QPixmap for nodes
        self._zoom = 1.0
        self._settle = 0
        self._fitted = False
        self._pan_from = None
        self._msg = None
        self._timer = QTimer(self)
        self._timer.setInterval(35)
        self._timer.timeout.connect(self._tick)
        self._float_timer = QTimer(self)
        self._float_timer.setInterval(50)
        self._float_timer.timeout.connect(self._float_tick)
        self._anchors = {}

    # -- data ----------------------------------------------------------------
    def clear_graph(self):
        self._timer.stop()
        self._float_timer.stop()
        self._anchors = {}
        self.nodes = []
        self.edge_items = []
        self.edge_pairs = []
        self.vx = []
        self.vy = []
        self.folder_of = []
        self.sel_index = None
        self._fitted = False
        self._settle = 0
        self._simulating = False
        self._items = {}
        self._visible = set()
        self._tree_children = {}
        self._edge_lines = {}
        self._parent_of = {}
        self._data_nodes = []
        self._children_data = {}
        self._parent_of_data = {}
        self._data_key = {}
        self._roots_idx = []
        self._link_src_dst = []
        self._link_lines = []
        self.scene().clear()
        if self._msg is not None:
            self._msg.deleteLater()
            self._msg = None

    def show_message(self, text):
        self.clear_graph()
        self._msg = QLabel(text, self.viewport())
        self._msg.setObjectName("graphMsg")
        self._msg.setWordWrap(True)
        self._msg.setAlignment(Qt.AlignCenter)
        self._msg.show()
        self._place_msg()

    def _place_msg(self):
        if self._msg is not None:
            self._msg.setGeometry(self.viewport().rect().adjusted(40, 40, -40, -40))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_msg()

    def set_graph(self, data):
        self.clear_graph()
        nodes, edges = data["nodes"], data["edges"]
        self._mode = data.get("mode", "links")
        if not nodes:
            self.show_message("Nothing to graph here yet.")
            return
        scene = self.scene()
        if self._mode in ("structure", "vault"):
            self._data_nodes = [dict(nd) for nd in nodes]
            incoming = set()
            for a, b in edges:
                self._children_data.setdefault(a, []).append(b)
                self._parent_of_data[b] = a
                incoming.add(b)
            self._roots_idx = [i for i in range(len(nodes)) if i not in incoming]
            for i, nd in enumerate(self._data_nodes):
                if nd.get("file"):
                    self._data_key[(nd.get("parent_path") or "").lower(),
                                   nd["file"].lower()] = i
            visible = set(self._roots_idx)
            for r in self._roots_idx:
                for c in self._children_data.get(r, []):
                    visible.add(c)
            for i in sorted(visible):
                self._add_visible(i, self._parent_of_data.get(i))
            self._link_src_dst = [(a.lower(), b.lower())
                                  for a, b in data.get("link_edges", [])] \
                if self._mode == "vault" else []
            self._apply_radial_layout()
            self._recompute_link_lines()
            return
        small = len(nodes) <= 120
        for i, nd in enumerate(nodes):
            r = 6.0 + min(12.0, (nd.get("degree", 0) ** 0.5) * 2.4)
            item = GraphNode(self, i, nd.get("label") or nd.get("name", ""), r,
                             nd.get("parent_path") or nd.get("parent"),
                             nd.get("file"), nd.get("path"),
                             nd.get("kind", "note"), nd.get("sub"))
            item.show_label = small or nd.get("degree", 0) >= 3
            item.setToolTip((nd.get("label") or nd.get("name", ""))
                            + (("\n" + nd["sub"]) if nd.get("sub") else ""))
            scene.addItem(item)
            self.nodes.append(item)
            self.vx.append(0.0)
            self.vy.append(0.0)
            self.folder_of.append((nd.get("parent_path") or nd.get("parent") or "").lower())
        pen = QPen(QColor("#e3e5e8"), 1.4)
        for a, b in edges:
            li = QGraphicsLineItem(0, 0, 0, 0)
            li.setPen(pen)
            li.setZValue(0)
            scene.addItem(li)
            self.edge_items.append((li, a, b))
            self.edge_pairs.append((a, b))
        self.apply_highlights()
        self._cluster_init()
        self.kick()

    # ---- structure mode: progressive disclosure --------------------------------
    def node_icon_pm(self, cat, radius):
        size = max(16, min(56, int(radius * 2)))
        key = (cat, size)
        if key not in self._pm_cache:
            if cat == "drive":
                base = place_icon("drive")
            elif cat == "app":
                base = app_icon()
            else:
                base = category_icon(cat)
            self._pm_cache[key] = base.pixmap(size, size)
        return self._pm_cache[key]

    def _add_visible(self, i, parent_idx=None):
        nd = self._data_nodes[i]
        kind = nd.get("kind", "folder")
        if kind == "drive":
            r = 15.0
        elif kind == "file":
            r = 5.0
        else:
            r = 7.0 + min(13.0, math.log10((nd.get("weight") or 0) + 1) * 3.4)
        item = GraphNode(self, i, nd.get("label") or "", r, nd.get("parent_path"),
                         nd.get("file"), nd.get("path"), kind, nd.get("sub"))
        cat = nd.get("category")
        if cat is None:
            if kind == "drive":
                cat = "drive"
            elif kind == "note":
                cat = "doc"
            elif kind == "folder":
                cat = "folder"
            elif nd.get("file"):
                cat = category_for(nd["file"], False)
        item.icon_pm = self.node_icon_pm(cat or "other", r)
        item.setToolTip((nd.get("label") or "")
                        + (("\n" + nd["sub"]) if nd.get("sub") else ""))
        self.scene().addItem(item)
        self._items[i] = item
        self._visible.add(i)
        if parent_idx is not None and parent_idx in self._visible:
            li = QGraphicsLineItem(0, 0, 0, 0)
            li.setPen(QPen(QColor("#e3e5e8"), 1.4))
            li.setZValue(0)
            self.scene().addItem(li)
            self._edge_lines[i] = li
            self._parent_of[i] = parent_idx
            self._tree_children.setdefault(parent_idx, []).append(i)
        item.can_expand = (bool(nd.get("has_children"))
                           and not self._tree_children.get(i))

    def expand_node(self, idx):
        """Reveal the direct children of this node — from the built data when
        available, otherwise fetched live from the index, so any depth of any
        drive can be explored."""
        if self._mode not in ("structure", "vault"):
            return
        parent_path = self._data_nodes[idx].get("path")
        if parent_path and self.child_provider:
            for nd in self.child_provider(parent_path):
                if self._mode == "vault":
                    # the link graph only shows folders and markdown notes
                    if nd.get("kind") == "file":
                        fn = (nd.get("file") or "").lower()
                        if not fn.endswith(".md"):
                            continue
                        stem = nd["file"][:-3] if fn.endswith(".md") else nd["file"]
                        nd = dict(nd)
                        nd["kind"] = "note"
                        nd["label"] = stem
                        nd["weight"] = 0
                        nd["sub"] = None
                        nd["has_children"] = False
                key = ((nd.get("parent_path") or "").lower(),
                       (nd.get("file") or "").lower())
                if key in self._data_key:
                    continue
                ni = len(self._data_nodes)
                self._data_nodes.append(nd)
                self._data_key[key] = ni
                self._children_data.setdefault(idx, []).append(ni)
        added = False
        for c in self._children_data.get(idx, []):
            if c in self._visible:
                continue
            self._add_visible(c, parent_idx=idx)
            added = True
        item = self._items.get(idx)
        if item:
            item.can_expand = False
            item.update()
        if added:
            self._apply_radial_layout()

    def collapse_node(self, idx):
        if self._mode not in ("structure", "vault"):
            return

        def rm(i):
            for c in list(self._tree_children.get(i, [])):
                rm(c)
            it = self._items.pop(i, None)
            if it is not None:
                self.scene().removeItem(it)
            li = self._edge_lines.pop(i, None)
            if li is not None:
                self.scene().removeItem(li)
            self._visible.discard(i)
            self._tree_children.pop(i, None)

        for c in list(self._tree_children.get(idx, [])):
            rm(c)
        self._tree_children.pop(idx, None)
        if self.sel_index not in self._visible:
            self.sel_index = None
        item = self._items.get(idx)
        if item:
            item.can_expand = True
            item.update()
        self._apply_radial_layout()

    def _recompute_link_lines(self):
        """Vault mode: draw one line per resolved note link. Both endpoints
        visible → note-to-note; a hidden endpoint attaches to its nearest
        visible ancestor bubble, so collapsed folders still show their
        connections."""
        scene = self.scene()
        for li, a, b in self._link_lines:
            scene.removeItem(li)
        self._link_lines = []
        if self._mode != "vault":
            return
        vis = {}
        for i in self._visible:
            p = (self._data_nodes[i].get("path") or "").lower()
            if p:
                vis[p] = i

        def endpoint(pl):
            while pl:
                if pl in vis:
                    return vis[pl]
                if "\\" not in pl:
                    break
                pl = pl.rsplit("\\", 1)[0]
            return None

        pairs = set()
        deg = {}
        for s, d in self._link_src_dst:
            a = endpoint(s)
            b = endpoint(d)
            if a is None or b is None or a == b:
                continue
            key = (min(a, b), max(a, b))
            if key in pairs:
                continue
            pairs.add(key)
            deg[a] = deg.get(a, 0) + 1
            deg[b] = deg.get(b, 0) + 1
        for a, b in sorted(pairs):
            li = QGraphicsLineItem(0, 0, 0, 0)
            li.setPen(QPen(QColor("#c3b8e2"), 1.2))
            li.setZValue(0)
            scene.addItem(li)
            self._link_lines.append((li, a, b))
            self._update_line(li, a, b)
        for i, it in self._items.items():
            if it.kind == "note" and deg.get(i, 0) >= 3:
                it.show_label = True

    def _apply_radial_layout(self):
        """Deterministic radial tidy-tree over the visible nodes: centre root,
        branches clockwise A→Z, ring radius by depth, angular width by subtree
        size. No simulation involved."""
        if self._mode not in ("structure", "vault") or not self._items:
            return
        children = {i: list(c) for i, c in self._tree_children.items() if c}
        roots_idx = [i for i in self._visible if i not in self._edge_lines]
        if not roots_idx:
            return
        depth_of = {}
        post = []
        visited = set()
        stack = [(r, 0, False) for r in reversed(roots_idx)]
        while stack:
            node, depth, done = stack.pop()
            if done:
                post.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            depth_of[node] = depth
            stack.append((node, depth, True))
            for c in children.get(node, []):
                if c not in visited and c in self._visible:
                    stack.append((c, depth + 1, False))
        disp_leaf = {i: 1 for i in self._visible}
        for node in post:  # children accumulate before parents
            kids = [c for c in children.get(node, []) if c in self._visible]
            if kids:
                disp_leaf[node] = sum(disp_leaf[c] for c in kids)
        total_leaves = max(1, sum(disp_leaf[r] for r in roots_idx))
        max_depth = max([1] + list(depth_of.values()))
        ring = min(9000.0, max(320.0, total_leaves * 30.0 / (2 * math.pi)))
        small = len(self._visible) <= 120

        def assign(idxs, a0, a1, depth):
            total = sum(disp_leaf[i] for i in idxs)
            ang = a0
            for i in sorted(idxs, key=lambda x: self._items[x].label.lower()):
                w = (a1 - a0) * disp_leaf[i] / total
                mid = ang + w / 2
                rad = ring * depth / max_depth
                it = self._items[i]
                if depth > 0:
                    # a little deterministic "floating" so the tree doesn't
                    # look mechanically perfect (±4% radius, ±15% of wedge)
                    h = zlib.crc32(it.label.encode("utf-8"))
                    j1 = ((h & 0xFFFF) / 0xFFFF - 0.5)
                    j2 = (((h >> 16) & 0xFFFF) / 0xFFFF - 0.5)
                    rad *= 1.0 + j1 * 0.08
                    mid += w * j2 * 0.3
                it.setPos(rad * math.cos(mid), rad * math.sin(mid))
                it.show_label = small or depth <= 2 or it.kind == "drive"
                kids = children.get(i)
                if kids:
                    assign(kids, ang, ang + w, depth + 1)
                ang += w

        assign(roots_idx, 0.0, 2 * math.pi, 0)
        for c, li in self._edge_lines.items():
            self._update_line(li, self._parent_of[c], c)
        self._anchors = {i: it.pos() for i, it in self._items.items()}
        if not self._float_timer.isActive():
            self._float_timer.start()
        self._fitted = True
        self.fit_view()

    def _float_tick(self):
        """Gentle idle drift for structure/vault graphs: each node bobs a
        few px around its radial-layout anchor. The layout itself stays
        deterministic; this is a visual offset only."""
        if self._mode not in ("structure", "vault") or not self._items:
            return
        t = time.time()
        for i, it in self._items.items():
            anchor = self._anchors.get(i)
            if anchor is None:
                continue
            if getattr(it, "_dragging", False):
                self._anchors[i] = it.pos()
                continue
            ph = i * 2.399963  # golden-angle phase spread
            it.setPos(anchor.x() + math.sin(t * 0.9 + ph) * 3.5,
                      anchor.y() + math.cos(t * 0.7 + ph * 1.3) * 3.5)
        if self._mode == "vault":
            self._recompute_link_lines()

    def _cluster_init(self):
        """Place notes near their folder's group centre so the link graph
        forms communities instead of one hairball."""
        groups = {}
        for i, key in enumerate(self.folder_of):
            groups.setdefault(key, []).append(i)
        for gi, key in enumerate(sorted(groups.keys())):
            ga = gi * 2.39996
            gr = 70.0 * ((gi + 1) ** 0.6)
            cx, cy = gr * math.cos(ga), gr * math.sin(ga)
            for k, i in enumerate(groups[key]):
                a2 = k * 2.39996
                rr = 26.0 * ((k + 1) ** 0.5)
                self.nodes[i].setPos(cx + rr * math.cos(a2), cy + rr * math.sin(a2))

    def select_node(self, node):
        self.sel_index = None if node.index == self.sel_index else node.index
        self.apply_highlights()
        self.node_clicked.emit(node if self.sel_index is not None else None)
        if self._mode in ("structure", "vault") and self.sel_index is not None \
                and node.can_expand:
            self.expand_node(node.index)

    def apply_highlights(self):
        pen_norm = QPen(QColor("#e3e5e8"), 1.4)
        pen_hot = QPen(QColor("#a7adb4"), 2.0)
        pen_dim = QPen(QColor("#f0f1f4"), 1.2)
        if self._mode in ("structure", "vault"):
            link_norm = QPen(QColor("#c3b8e2"), 1.2)
            link_hot = QPen(QColor("#8b5cf6"), 2.0)
            link_dim = QPen(QColor("#eeeaf6"), 1.0)
            neigh = set()
            if self.sel_index is not None:
                neigh.update(self._tree_children.get(self.sel_index, []))
                par = self._parent_of.get(self.sel_index)
                if par is not None:
                    neigh.add(par)
            for i, it in self._items.items():
                if self.sel_index is None:
                    it.state = 0
                elif i == self.sel_index:
                    it.state = 2
                elif i in neigh:
                    it.state = 1
                else:
                    it.state = 3
                it.update()
            for c, li in self._edge_lines.items():
                if self.sel_index is None:
                    li.setPen(pen_norm)
                elif c == self.sel_index or self._parent_of.get(c) == self.sel_index:
                    li.setPen(pen_hot)
                else:
                    li.setPen(pen_dim)
            for li, a, b in self._link_lines:
                if self.sel_index is None:
                    li.setPen(link_norm)
                elif a == self.sel_index or b == self.sel_index:
                    li.setPen(link_hot)
                else:
                    li.setPen(link_dim)
            return
        neigh = set()
        if self.sel_index is not None:
            for a, b in self.edge_pairs:
                if a == self.sel_index:
                    neigh.add(b)
                elif b == self.sel_index:
                    neigh.add(a)
        for i, it in enumerate(self.nodes):
            if self.sel_index is None:
                it.state = 0
            elif i == self.sel_index:
                it.state = 2
            elif i in neigh:
                it.state = 1
            else:
                it.state = 3
            it.update()
        for li, a, b in self.edge_items:
            if self.sel_index is None:
                li.setPen(pen_norm)
            elif a == self.sel_index or b == self.sel_index:
                li.setPen(pen_hot)
            else:
                li.setPen(pen_dim)

    def pause(self):
        self._timer.stop()

    # -- simulation ------------------------------------------------------------
    def kick(self):
        self._settle = 0
        if self.nodes and not self._timer.isActive():
            self._timer.start()

    def update_edges_for(self, idx):
        if self._mode in ("structure", "vault"):
            li = self._edge_lines.get(idx)
            if li is not None:
                self._update_line(li, self._parent_of[idx], idx)
            return
        for li, a, b in self.edge_items:
            if a == idx or b == idx:
                self._update_line(li, a, b)

    def _update_line(self, li, a, b):
        if self._mode in ("structure", "vault"):
            pa = self._items[a].pos()
            pb = self._items[b].pos()
        else:
            pa, pb = self.nodes[a].pos(), self.nodes[b].pos()
        li.setLine(QLineF(pa.x(), pa.y(), pb.x(), pb.y()))

    def _tick(self):
        n = len(self.nodes)
        if n == 0:
            self._timer.stop()
            return
        items = self.nodes
        xs = [it.x() for it in items]
        ys = [it.y() for it in items]
        fx = [0.0] * n
        fy = [0.0] * n
        cell = self.CELL
        cutoff2 = self.CUTOFF * self.CUTOFF
        grid = {}
        for i in range(n):
            grid.setdefault((int(xs[i] // cell), int(ys[i] // cell)), []).append(i)
        kr = 26000.0
        for i in range(n):
            cx, cy = int(xs[i] // cell), int(ys[i] // cell)
            xi, yi = xs[i], ys[i]
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    for j in grid.get((gx, gy), ()):
                        if j == i:
                            continue
                        dx, dy = xi - xs[j], yi - ys[j]
                        d2 = dx * dx + dy * dy
                        if d2 == 0.0:
                            fx[i] += 2.0
                            fy[i] += 1.5
                            continue
                        if d2 > cutoff2:
                            continue
                        f = kr / d2
                        if f > 60.0:
                            f = 60.0
                        d = d2 ** 0.5
                        fx[i] += dx / d * f
                        fy[i] += dy / d * f
        ks, rest = 0.028, 115.0
        for a, b in self.edge_pairs:
            dx, dy = xs[b] - xs[a], ys[b] - ys[a]
            d = (dx * dx + dy * dy) ** 0.5
            if d == 0.0:
                continue
            f = ks * (d - rest)
            ux, uy = dx / d, dy / d
            fx[a] += ux * f
            fy[a] += uy * f
            fx[b] -= ux * f
            fy[b] -= uy * f
        # mild pull toward each folder's centroid → visible communities
        if self._mode == "links" and self.folder_of:
            cen, cnt = {}, {}
            for i in range(n):
                k = self.folder_of[i]
                if k:
                    c = cen.get(k)
                    if c is None:
                        cen[k] = [xs[i], ys[i]]
                        cnt[k] = 1
                    else:
                        c[0] += xs[i]
                        c[1] += ys[i]
                        cnt[k] += 1
            for i in range(n):
                k = self.folder_of[i]
                if k and cnt[k] > 1:
                    fx[i] += (cen[k][0] / cnt[k] - xs[i]) * 0.012
                    fy[i] += (cen[k][1] / cnt[k] - ys[i]) * 0.012
        moved = 0.0
        damp, maxv = 0.80, 11.0
        self._simulating = True
        for i in range(n):
            fx[i] += -xs[i] * 0.0016
            fy[i] += -ys[i] * 0.0016
            if items[i]._dragging:
                self.vx[i] = self.vy[i] = 0.0
                continue
            vx = (self.vx[i] + fx[i]) * damp
            vy = (self.vy[i] + fy[i]) * damp
            sp = (vx * vx + vy * vy) ** 0.5
            if sp > maxv:
                vx, vy = vx / sp * maxv, vy / sp * maxv
            self.vx[i], self.vy[i] = vx, vy
            items[i].setPos(xs[i] + vx, ys[i] + vy)
            moved += abs(vx) + abs(vy)
        self._simulating = False
        for li, a, b in self.edge_items:
            self._update_line(li, a, b)
        if moved / max(1, n) < 0.15:
            self._settle += 1
            if self._settle > 18:
                self._timer.stop()
                if not self._fitted:
                    self._fitted = True
                    self.fit_view()
        else:
            self._settle = 0

    def fit_view(self):
        br = self.scene().itemsBoundingRect().adjusted(-80, -80, 80, 80)
        if br.width() < 10:
            return
        self.fitInView(br, Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()

    # -- interaction -----------------------------------------------------------
    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        factor = 1.18 if delta > 0 else 1 / 1.18
        nz = self._zoom * factor
        if 0.08 <= nz <= 8.0:
            self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
            self.scale(factor, factor)
            self._zoom = nz
        e.accept()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not isinstance(self.itemAt(e.pos()), GraphNode):
            self._pan_from = e.pos()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._pan_from is not None:
            d = e.pos() - self._pan_from
            self._pan_from = e.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._pan_from is not None and e.button() == Qt.LeftButton:
            self._pan_from = None
            self.viewport().unsetCursor()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        if isinstance(self.itemAt(e.pos()), GraphNode):
            super().contextMenuEvent(e)
            return
        menu = QMenu(self)
        menu.addAction("Fit to view", self.fit_view)
        if self._mode == "links":
            menu.addAction("Re-run layout", self.kick)
        else:
            menu.addAction("Reapply radial layout", self._apply_radial_layout)
        menu.exec(e.globalPos())

class QLaunchButton(QPushButton):
    """Sidebar quick-launch entry: click = inspect, double-click = launch."""
    launch = Signal()

    def __init__(self, path, parent):
        stem = os.path.splitext(os.path.basename(path))[0]
        super().__init__("  " + stem, parent)
        self._path = path
        self.setObjectName("sideBtn")
        self.setIcon(app_icon_for(path))
        self.setIconSize(QSize(16, 16))
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip(path)

    def path(self):
        return self._path

    def mouseDoubleClickEvent(self, e):
        self.launch.emit()
