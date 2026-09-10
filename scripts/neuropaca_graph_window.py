#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A live, native force-directed view of the behavioural graph (B9 companion).

    /usr/bin/python3 scripts/neuropaca_graph_window.py
    /usr/bin/python3 scripts/neuropaca_graph_window.py --graph data/graph.json

WHY A NATIVE WINDOW AND NOT THE HTML VIEWER

`scripts/neuropaca_graph.py` writes a self-contained HTML file and hands it to
the browser. This is the same picture without the browser: a GTK window drawing
the graph with Cairo directly. It is meant to be opened straight from the soak
tray widget and to *stay* open next to the panel for the whole week, redrawing
itself as the daemon's graph grows.

WHAT IT SHOWS  (matches graph_view_template.html so the two never disagree)

    node colour   node_type            (concept / app / insight / idle_thought …)
    node radius   relevance_score      the 0-10 score, hubs drawn larger
    edge opacity  weight               a bright line is a Hebbian-reinforced pair
    label         hubs always; others on zoom-in or hover

LIVE, AUTOMATICALLY

`GraphMemory.save()` runs on the daemon's scheduler interval (default 300 s), so
`data/graph.json` is at most five minutes behind the live daemon. This window
stats that file every few seconds and, when its mtime moves, re-reads it and
runs a short warm relayout from the current node positions -- so new nodes ease
into place rather than the whole graph jumping. It also force-reloads every time
the window is shown. A couple of seconds of settling after a reload is expected
and fine.

SAFE TO RUN DURING A SOAK

Imports nothing from the `neuropaca` package (the venv's editable install *is*
the running daemon), opens no socket, and only ever reads the graph file. The
one exception is by design: `src/neuropaca/core/labels.py` — pure stdlib, no
package imports — is loaded *by file path*, so every caption here is rendered by
the same code as the daemon's labels (B18). A single-instance
lock keeps a double-click from stacking windows. Pure stdlib + PyGObject + Cairo
-- runs under system python3, same split as scripts/soak_tray.py.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DEFAULT_GRAPH = REPO / "data" / "graph.json"
LOCK_PATH = REPO / "data" / "soak" / ".graph-window.lock"

ROOT_ID = "YOU"
DOMAIN_PREFIX = "domain:"

# The 10 routing domains, in `graph_memory.DOMAIN_SLUGS` order — B17 pins the
# hubs on a fixed ring in this order so the structure never reshuffles.
DOMAIN_ORDER: tuple[str, ...] = (
    "engineering",
    "research",
    "tools",
    "system",
    "habits",
    "projects",
    "meetings",
    "comms",
    "mental_models",
    "learning",
)
HUB_RING = 260.0  # world-unit radius of the domain ring around YOU


def _load_labels() -> Any:
    """B18 · the daemon's one renderer, loaded by path (see module docstring)."""
    import importlib.util

    name = "_neuropaca_labels"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, REPO / "src" / "neuropaca" / "core" / "labels.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


labels = _load_labels()


def pretty_label(
    node_id: str,
    node: dict[str, Any],
    nodes: dict[str, dict[str, Any]] | None = None,
    mode: str = "full",
) -> str:
    """A human name for any node — `labels.display`, over the graph file's
    dicts. `nodes` (the whole graph) lets a spec'd node name its refs."""
    pool = nodes or {}

    def lookup(ref: str) -> tuple[str, Any] | None:
        raw = pool.get(ref)
        if raw is None:
            return None
        return str(raw.get("label") or ""), labels.LabelSpec.from_record(raw.get("spec"))

    spec = labels.LabelSpec.from_record(node.get("spec"))
    return str(
        labels.display(node_id, str(node.get("label") or ""), spec, labels.ref_namer(lookup), mode)
    )


def is_fact(node: dict[str, Any]) -> bool:
    """A generated node (B18) — its `spec` is what it is about."""
    return labels.LabelSpec.from_record(node.get("spec")) is not None


# Node-type fill colours -- lifted verbatim from graph_view_template.html's dark
# palette so this window and the HTML viewer render the same graph the same way.
TYPE_COLOUR: dict[str, tuple[float, float, float]] = {
    "concept": (0.878, 0.643, 0.345),
    "file": (0.298, 0.788, 0.831),
    "app": (0.655, 0.545, 0.980),
    "webapp": (0.541, 0.831, 0.941),
    "insight": (0.373, 0.808, 0.604),
    "idle_thought": (0.878, 0.478, 0.722),
    "session": (0.549, 0.604, 0.659),
    "metric": (0.816, 0.659, 0.416),
    "task": (0.416, 0.682, 0.910),
    "person": (0.906, 0.541, 0.659),
    "event_log": (0.604, 0.647, 0.694),
    "goal": (0.424, 0.769, 0.671),
}
FALLBACK_COLOUR = (0.60, 0.66, 0.73)

BG = (0.043, 0.055, 0.078)  # --ground
INK = (0.902, 0.925, 0.953)  # --ink
MUTED = (0.596, 0.647, 0.702)  # --muted
EDGE_RGB = (0.745, 0.804, 0.871)  # --edge base, alpha applied per edge


# --------------------------------------------------------------------------- #
# graph file -> plain dicts                                                    #
# --------------------------------------------------------------------------- #
class GraphData:
    """The parsed graph plus the file mtime it was read at."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str, float, str]] = []
        self.domain_of: dict[str, str] = {}
        self.mtime: float = 0.0
        self.error: str | None = None
        self._captions: dict[str, str] = {}
        self._captions_key: tuple[float, int] = (-1.0, -1)

    def captions(self) -> dict[str, str]:
        """Short, on-screen-distinct caption per node (B18), cached per file
        version. Only captions that still collide get a time hint appended."""
        key = (self.mtime, len(self.nodes))
        if key != self._captions_key:
            short = {
                nid: pretty_label(nid, n, self.nodes, "short") for nid, n in self.nodes.items()
            }
            hints = {nid: str(n.get("created_at") or "")[11:16] for nid, n in self.nodes.items()}
            self._captions = labels.disambiguate(short, hints)
            self._captions_key = key
        return self._captions

    def reload(self, path: Path) -> bool:
        """Re-read `path`. Returns True if the content changed (or first load)."""
        try:
            st = path.stat()
        except OSError:
            changed = self.error != "no-file"
            self.error, self.nodes, self.edges = "no-file", {}, []
            return changed
        if st.st_mtime == self.mtime and self.error is None:
            return False
        try:
            payload = json.loads(path.read_text("utf-8"))
            raw_nodes = payload["nodes"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.error = f"unreadable: {exc}"
            return True

        nodes = {n["id"]: n for n in raw_nodes}
        edges: list[tuple[str, str, float, str]] = []
        for e in payload.get("edges", []):
            s, t = e.get("source"), e.get("target")
            if s in nodes and t in nodes:
                edges.append((s, t, float(e.get("weight", 0.0)), str(e.get("relation", ""))))

        hubs = {i for i in nodes if i == ROOT_ID or i.startswith(DOMAIN_PREFIX)}
        domain_of: dict[str, str] = {}
        for s, t, _w, _r in edges:
            if s in hubs and t not in hubs and t not in domain_of:
                domain_of[t] = s
            elif t in hubs and s not in hubs and s not in domain_of:
                domain_of[s] = t

        self.nodes, self.edges, self.domain_of = nodes, edges, domain_of
        self.mtime, self.error = st.st_mtime, None
        return True

    @staticmethod
    def is_hub(node_id: str) -> bool:
        return node_id == ROOT_ID or node_id.startswith(DOMAIN_PREFIX)

    def degree(self, node_id: str) -> int:
        return sum(1 for s, t, _w, _r in self.edges if node_id in (s, t))


# --------------------------------------------------------------------------- #
# force-directed layout                                                        #
# --------------------------------------------------------------------------- #
class Layout:
    """A small Fruchterman-Reingold layout with domain clustering.

    Positions persist across reloads: `sync()` keeps the coordinates of nodes
    that are still present and drops the rest, so a reload nudges the picture
    instead of reshuffling it. New nodes appear next to their domain hub.
    """

    def __init__(self) -> None:
        self.pos: dict[str, list[float]] = {}
        self.pinned: set[str] = set()
        self._rng = random.Random(7)
        self.temperature = 1.0

    @staticmethod
    def hub_position(node_id: str) -> tuple[float, float] | None:
        """Deterministic fixed position for one of the 11 master nodes, or None.
        YOU at the origin; the 10 domains evenly on a ring, first at 12 o'clock,
        clockwise in DOMAIN_ORDER (B17 — the structure never reshuffles)."""
        if node_id == ROOT_ID:
            return (0.0, 0.0)
        if not node_id.startswith(DOMAIN_PREFIX):
            return None
        slug = node_id[len(DOMAIN_PREFIX) :]
        if slug not in DOMAIN_ORDER:
            return None
        i = DOMAIN_ORDER.index(slug)
        ang = -math.pi / 2 + 2 * math.pi * i / len(DOMAIN_ORDER)
        return (HUB_RING * math.cos(ang), HUB_RING * math.sin(ang))

    def pin_hubs(self, data: GraphData) -> None:
        """Force every present master node onto its fixed position and pin it —
        called every `sync()` so drift or a stale drag can never move a hub."""
        for node_id in data.nodes:
            fixed = self.hub_position(node_id)
            if fixed is not None:
                self.pos[node_id] = [fixed[0], fixed[1]]
                self.pinned.add(node_id)

    def sync(self, data: GraphData) -> None:
        ids = set(data.nodes)
        for gone in set(self.pos) - ids:
            del self.pos[gone]
            self.pinned.discard(gone)
        n = max(len(ids), 1)
        radius = max(24.0 * math.sqrt(n), HUB_RING * 1.15)
        for node_id in ids - set(self.pos):
            if self.hub_position(node_id) is not None:
                continue  # placed by pin_hubs below
            anchor = data.domain_of.get(node_id)
            if anchor and anchor in self.pos:
                ax, ay = self.pos[anchor]
                self.pos[node_id] = [
                    ax + self._rng.uniform(-40, 40),
                    ay + self._rng.uniform(-40, 40),
                ]
            else:
                ang = self._rng.uniform(0, 2 * math.pi)
                self.pos[node_id] = [
                    radius * math.cos(ang) * self._rng.uniform(0.3, 1.0),
                    radius * math.sin(ang) * self._rng.uniform(0.3, 1.0),
                ]
        self.pin_hubs(data)
        if ids - set(self.pos) == set():
            self.temperature = max(self.temperature, 0.6)

    K = 70.0  # ideal edge length in world units -- sets the overall scale
    GRAVITY = 0.06  # pull toward the origin; keeps unconnected nodes in frame
    CLUSTER = 0.9  # extra spring from a member node to its domain hub

    def step(self, data: GraphData, iterations: int = 1) -> None:
        ids = list(data.nodes)
        if len(ids) < 2:
            return
        k = self.K
        cutoff = 3.6 * k  # nodes further apart than this do not repel --
        cutoff_sq = cutoff * cutoff  # this is what keeps a sparse graph compact
        for _ in range(iterations):
            disp: dict[str, list[float]] = {i: [0.0, 0.0] for i in ids}

            # bounded all-pairs repulsion  (O(n^2), fine for a graph this size)
            for a_idx, a in enumerate(ids):
                ax, ay = self.pos[a]
                a_hub = data.is_hub(a)
                for b in ids[a_idx + 1 :]:
                    bx, by = self.pos[b]
                    dx, dy = ax - bx, ay - by
                    d_sq = dx * dx + dy * dy
                    both_hub = a_hub and data.is_hub(b)
                    if d_sq > cutoff_sq and not both_hub:
                        continue
                    dist = math.sqrt(d_sq) or 0.01
                    force = (k * k) / dist
                    if both_hub:
                        force *= 2.6  # push the domain hubs well apart
                    ux, uy = dx / dist, dy / dist
                    disp[a][0] += ux * force
                    disp[a][1] += uy * force
                    disp[b][0] -= ux * force
                    disp[b][1] -= uy * force

            # edge springs
            for s, t, _w, _r in data.edges:
                sx, sy = self.pos[s]
                tx, ty = self.pos[t]
                dx, dy = sx - tx, sy - ty
                dist = math.hypot(dx, dy) or 0.01
                force = (dist * dist) / k
                ux, uy = dx / dist, dy / dist
                disp[s][0] -= ux * force
                disp[s][1] -= uy * force
                disp[t][0] += ux * force
                disp[t][1] += uy * force

            # cluster members around their own domain hub (Obsidian-ish grouping)
            for node_id, hub in data.domain_of.items():
                if node_id not in self.pos or hub not in self.pos:
                    continue
                hx, hy = self.pos[hub]
                nx, ny = self.pos[node_id]
                disp[node_id][0] -= (nx - hx) * self.CLUSTER * 0.02
                disp[node_id][1] -= (ny - hy) * self.CLUSTER * 0.02

            limit = 10.0 * self.temperature + 0.35
            for node_id in ids:
                if node_id in self.pinned:
                    continue
                dx, dy = disp[node_id]
                cx, cy = self.pos[node_id]
                grav = self.GRAVITY * (0.3 if data.is_hub(node_id) else 1.0)
                dx -= cx * grav
                dy -= cy * grav
                mag = math.hypot(dx, dy) or 0.01
                capped = min(mag, limit)
                self.pos[node_id][0] += dx / mag * capped
                self.pos[node_id][1] += dy / mag * capped

            self.temperature = max(0.02, self.temperature * 0.985)

    def bounds(self) -> tuple[float, float, float, float]:
        if not self.pos:
            return -1.0, -1.0, 1.0, 1.0
        xs = [p[0] for p in self.pos.values()]
        ys = [p[1] for p in self.pos.values()]
        return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------------- #
# GTK window                                                                   #
# --------------------------------------------------------------------------- #
def _run(graph_path: Path) -> int:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo

    data = GraphData()
    layout = Layout()

    state = {
        "scale": 1.0,
        "tx": 0.0,
        "ty": 0.0,
        "hover": None,
        "drag_node": None,
        "pan": None,
        "last_reload_check": 0.0,
        "settle_frames": 0,
        "fitted": False,
        "selected": None,  # B17 · node whose detail panel is open
    }

    def reload_now(*, warm: int) -> None:
        if data.reload(graph_path):
            layout.sync(data)
            layout.temperature = max(layout.temperature, 0.9)
            layout.step(data, warm)
            state["settle_frames"] = 200
            if not state["fitted"] and data.nodes:
                fit_view()
            if state["selected"] and state["selected"] not in data.nodes:
                select_node(None)
            elif state["selected"]:
                populate_panel(state["selected"])
        _update_title()

    # ---- view helpers ------------------------------------------------------ #
    def fit_view() -> None:
        alloc = area.get_allocation()
        w, h = max(alloc.width, 50), max(alloc.height, 50)
        x0, y0, x1, y1 = layout.bounds()
        gw, gh = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
        state["scale"] = min(w / (gw + 220), h / (gh + 240), 2.2)
        state["scale"] = max(state["scale"], 0.05)
        state["tx"] = w / 2 - (x0 + x1) / 2 * state["scale"]
        # nudge up a touch: labels hang below their node
        state["ty"] = h / 2 - (y0 + y1) / 2 * state["scale"] - 14
        state["fitted"] = True

    def to_screen(x: float, y: float) -> tuple[float, float]:
        return x * state["scale"] + state["tx"], y * state["scale"] + state["ty"]

    def to_world(sx: float, sy: float) -> tuple[float, float]:
        return (sx - state["tx"]) / state["scale"], (sy - state["ty"]) / state["scale"]

    def node_at(sx: float, sy: float) -> str | None:
        best, best_d = None, 1e9
        for node_id, (wx, wy) in layout.pos.items():
            px, py = to_screen(wx, wy)
            d = math.hypot(px - sx, py - sy)
            if d <= _radius(node_id) + 4 and d < best_d:
                best, best_d = node_id, d
        return best

    def _radius(node_id: str) -> float:
        node = data.nodes.get(node_id, {})
        score = float(node.get("relevance_score", 0.0))
        base = 5.0 + score * 1.7
        if data.is_hub(node_id):
            base = 11.0 + score * 1.6
        return base * (0.55 + 0.45 * min(state["scale"], 1.6))

    def _colour(node_id: str) -> tuple[float, float, float]:
        ntype = str(data.nodes.get(node_id, {}).get("node_type", ""))
        return TYPE_COLOUR.get(ntype, FALLBACK_COLOUR)

    # ---- drawing --------------------------------------------------------- #
    def on_draw(_widget: Any, cr: Any) -> bool:
        alloc = area.get_allocation()
        w, h = alloc.width, alloc.height
        cr.set_source_rgb(*BG)
        cr.paint()

        if data.error == "no-file":
            _centre_text(cr, w, h, "waiting for the daemon to write data/graph.json …")
            return False
        if data.error:
            _centre_text(cr, w, h, data.error)
            return False
        if not data.nodes:
            _centre_text(cr, w, h, "the graph is empty")
            return False

        hover = state["hover"]
        neighbours: set[str] = set()
        if hover:
            for s, t, _w, _r in data.edges:
                if s == hover:
                    neighbours.add(t)
                elif t == hover:
                    neighbours.add(s)

        max_w = max((e[2] for e in data.edges), default=0.0)
        for s, t, weight, _relation in data.edges:
            sx, sy = to_screen(*layout.pos[s])
            tx, ty = to_screen(*layout.pos[t])
            hub_edge = data.is_hub(s) or data.is_hub(t)
            alpha = 0.10 if hub_edge else 0.22
            if max_w > 0:
                alpha += 0.42 * (weight / max_w)
            width = 0.8 + (1.8 * weight / max_w if max_w else 0.0)
            if hover:
                if hover in (s, t):
                    alpha, width = min(alpha + 0.4, 0.95), width + 1.1
                else:
                    alpha *= 0.25
            cr.set_source_rgba(*EDGE_RGB, alpha)
            cr.set_line_width(width)
            cr.move_to(sx, sy)
            cr.line_to(tx, ty)
            cr.stroke()

        show_all_labels = state["scale"] >= 1.35
        for node_id in sorted(layout.pos, key=lambda n: data.is_hub(n)):
            wx, wy = layout.pos[node_id]
            px, py = to_screen(wx, wy)
            r = _radius(node_id)
            cr_col = _colour(node_id)
            dim = bool(hover) and node_id != hover and node_id not in neighbours

            # glow
            grad = _radial(cr, px, py, r * 2.6)
            grad.add_color_stop_rgba(0.0, *cr_col, 0.30 if not dim else 0.08)
            grad.add_color_stop_rgba(1.0, *cr_col, 0.0)
            cr.set_source(grad)
            cr.arc(px, py, r * 2.6, 0, 2 * math.pi)
            cr.fill()

            # body
            cr.set_source_rgba(*cr_col, 0.28 if dim else 1.0)
            cr.arc(px, py, r, 0, 2 * math.pi)
            cr.fill()

            selected = node_id == state["selected"]
            if node_id.startswith("ephemeral:"):
                cr.set_dash([2.0, 2.0])
            if selected:
                cr.set_source_rgba(*INK, 1.0)
                cr.set_line_width(2.4)
            else:
                cr.set_source_rgba(*INK, 0.0 if dim else (0.9 if node_id == hover else 0.35))
                cr.set_line_width(1.5 if node_id == hover else 1.0)
            cr.arc(px, py, r, 0, 2 * math.pi)
            cr.stroke()
            cr.set_dash([])

            if (
                data.is_hub(node_id)
                or node_id in (hover, state["selected"])
                or node_id in neighbours
                or show_all_labels
            ):
                _label(
                    cr,
                    PangoCairo,
                    Pango,
                    px,
                    py + r + 3,
                    data.captions().get(node_id, node_id),
                    bold=data.is_hub(node_id),
                    dim=dim,
                )
        return False

    def _radial(cr: Any, x: float, y: float, r: float) -> Any:
        import cairo

        return cairo.RadialGradient(x, y, 0, x, y, r)

    def _label(
        cr: Any,
        PangoCairo: Any,
        Pango: Any,
        x: float,
        y: float,
        text: str,
        *,
        bold: bool,
        dim: bool,
    ) -> None:
        layout_p = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription("Sans %s 9" % ("Bold" if bold else ""))
        layout_p.set_font_description(desc)
        layout_p.set_text(text if len(text) <= 34 else text[:33] + "…", -1)
        tw, _th = layout_p.get_pixel_size()
        cr.move_to(x - tw / 2, y)
        cr.set_source_rgba(*(INK if bold else MUTED), 0.15 if dim else 0.92)
        PangoCairo.show_layout(cr, layout_p)

    def _centre_text(cr: Any, w: int, h: int, text: str) -> None:
        cr.set_source_rgba(*MUTED, 0.9)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(13)
        ext = cr.text_extents(text)
        cr.move_to(w / 2 - ext.width / 2, h / 2)
        cr.show_text(text)

    # ---- animation + live reload ---------------------------------------- #
    def tick() -> bool:
        now = time.monotonic()
        if now - state["last_reload_check"] > 3.5:
            state["last_reload_check"] = now
            if data.reload(graph_path):
                layout.sync(data)
                layout.temperature = max(layout.temperature, 0.85)
                state["settle_frames"] = 200
                _update_title()

        if state["settle_frames"] > 0 or layout.temperature > 0.05:
            layout.step(data, 2)
            state["settle_frames"] = max(0, state["settle_frames"] - 1)
            area.queue_draw()
        return True

    # ---- detail panel (B17) ------------------------------------------- #
    def select_node(node_id: str | None) -> None:
        state["selected"] = node_id if node_id in data.nodes else None
        if state["selected"]:
            populate_panel(state["selected"])
            panel.show()
        else:
            panel.hide()
        area.queue_draw()

    def _row(text: str, *, key: str = "", mono: bool = False, bold: bool = False) -> Any:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if key:
            k = Gtk.Label(label=key, xalign=0.0)
            k.get_style_context().add_class("dim-label")
            k.set_size_request(96, -1)
            row.pack_start(k, False, False, 0)
        v = Gtk.Label(label=text, xalign=0.0, selectable=mono, wrap=True)
        if mono:
            v.set_name("mono")
        if bold:
            v.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>")
        row.pack_start(v, True, True, 0)
        return row

    def populate_panel(node_id: str) -> None:
        for child in panel_box.get_children():
            panel_box.remove(child)
        node = data.nodes.get(node_id, {})
        panel_box.pack_start(
            _row(pretty_label(node_id, node, data.nodes), bold=True), False, False, 0
        )
        panel_box.pack_start(_row(node_id, mono=True), False, False, 0)
        panel_box.pack_start(Gtk.Separator(), False, False, 4)

        def num(key: str, fmt: str = "{}") -> str:
            val = node.get(key)
            return fmt.format(val) if val not in (None, "", 0, 0.0) else "—"

        def add(text: str, key: str = "") -> None:
            panel_box.pack_start(_row(text, key=key), False, False, 0)

        add(str(node.get("node_type", "—")), "type")
        add(num("relevance_score", "{:.2f}"), "relevance")
        if is_fact(node):
            # B18: a repeat reinforces the one node — this is how often it recurred
            add(f"{int(node.get('access_count') or 0) + 1} times", "seen")
            add(str(node.get("created_at") or "—").replace("T", " ")[:19], "first seen")
            add(str(node.get("last_accessed") or "—").replace("T", " ")[:19], "last seen")
        else:
            add(num("access_count"), "focused")
        add(num("priority"), "priority")
        if node.get("ram_mb"):
            add(f"{float(node['ram_mb']):.0f} MiB", "RAM")
        if node.get("cpu_percent"):
            add(f"{float(node['cpu_percent']):.0f}%", "CPU")
        for key, label in (("first_seen_at", "first seen"), ("last_seen_at", "last seen")):
            if node.get(key):
                add(str(node[key]).replace("T", " ")[:19], label)

        nbrs = sorted(
            {(r, t) for s, t, _w, r in data.edges if s == node_id}
            | {(r, s) for s, t, _w, r in data.edges if t == node_id}
        )
        panel_box.pack_start(Gtk.Separator(), False, False, 4)
        panel_box.pack_start(_row(f"Connected to ({len(nbrs)})", bold=True), False, False, 0)
        for relation, other in nbrs:
            other_name = data.captions().get(other) or pretty_label(
                other, data.nodes.get(other, {}), data.nodes
            )
            btn = Gtk.Button(label=f"{relation or 'related'} · {other_name}")
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.get_child().set_xalign(0.0)
            btn.connect("clicked", lambda _b, o=other: select_node(o))
            panel_box.pack_start(btn, False, False, 0)
        panel_box.show_all()

    # ---- interaction --------------------------------------------------- #
    def on_press(_w: Any, ev: Any) -> bool:
        if ev.button == 1:
            hit = node_at(ev.x, ev.y)
            if hit:
                select_node(hit)
                if not data.is_hub(hit):  # hubs are fixed — never draggable (B17)
                    state["drag_node"] = hit
                    layout.pinned.add(hit)
            else:
                select_node(None)
                state["pan"] = (ev.x, ev.y, state["tx"], state["ty"])
        elif ev.button == 3:
            hit = node_at(ev.x, ev.y)
            if hit and not data.is_hub(hit):
                layout.pinned.discard(hit)
                layout.temperature = max(layout.temperature, 0.4)
        return True

    def on_release(_w: Any, ev: Any) -> bool:
        state["drag_node"] = None
        state["pan"] = None
        return True

    def on_motion(_w: Any, ev: Any) -> bool:
        if state["drag_node"]:
            layout.pos[state["drag_node"]] = list(to_world(ev.x, ev.y))
            area.queue_draw()
        elif state["pan"]:
            ox, oy, otx, oty = state["pan"]
            state["tx"], state["ty"] = otx + (ev.x - ox), oty + (ev.y - oy)
            area.queue_draw()
        else:
            hv = node_at(ev.x, ev.y)
            if hv != state["hover"]:
                state["hover"] = hv
                area.queue_draw()
        return True

    def on_scroll(_w: Any, ev: Any) -> bool:
        direction = -1 if ev.direction == Gdk.ScrollDirection.DOWN else 1
        if ev.direction == Gdk.ScrollDirection.SMOOTH:
            direction = -1 if ev.delta_y > 0 else 1
        factor = 1.12 if direction > 0 else 1 / 1.12
        wx, wy = to_world(ev.x, ev.y)
        state["scale"] = max(0.05, min(state["scale"] * factor, 6.0))
        state["tx"] = ev.x - wx * state["scale"]
        state["ty"] = ev.y - wy * state["scale"]
        area.queue_draw()
        return True

    # ---- window scaffold --------------------------------------------- #
    win = Gtk.Window()
    win.set_title("NeuroPACA · graph")
    win.set_default_size(960, 680)
    win.set_role("neuropaca-graph")

    header = Gtk.HeaderBar(show_close_button=True)
    header.set_title("NeuroPACA · graph")
    win.set_titlebar(header)

    btn_fit = Gtk.Button.new_from_icon_name("zoom-fit-best-symbolic", Gtk.IconSize.BUTTON)
    btn_fit.set_tooltip_text("Fit graph to window")
    btn_fit.connect("clicked", lambda *_: (fit_view(), area.queue_draw()))
    header.pack_start(btn_fit)

    btn_reload = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
    btn_reload.set_tooltip_text("Re-read data/graph.json now")
    btn_reload.connect("clicked", lambda *_: reload_now(warm=160))
    header.pack_start(btn_reload)

    area = Gtk.DrawingArea()
    area.set_events(
        Gdk.EventMask.BUTTON_PRESS_MASK
        | Gdk.EventMask.BUTTON_RELEASE_MASK
        | Gdk.EventMask.POINTER_MOTION_MASK
        | Gdk.EventMask.SCROLL_MASK
        | Gdk.EventMask.SMOOTH_SCROLL_MASK
    )
    area.connect("draw", on_draw)
    area.connect("button-press-event", on_press)
    area.connect("button-release-event", on_release)
    area.connect("motion-notify-event", on_motion)
    area.connect("scroll-event", on_scroll)

    # B17 · the node detail panel, docked right. It starts hidden (`panel.hide()`
    # right after `win.show_all()` below) rather than via `set_no_show_all` —
    # `no_show_all` stops `show_all()` descending into the frame at all, which
    # left the inner ScrolledWindow unrealised, so `panel.show()` on a click
    # revealed an empty frame (T7 follow-up). Hiding only the frame keeps the
    # whole subtree shown, so a click just un-hides it.
    panel_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    panel_box.set_border_width(12)
    panel_scroll = Gtk.ScrolledWindow()
    panel_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    panel_scroll.add(panel_box)
    panel = Gtk.Frame()
    panel.set_size_request(300, -1)
    panel.set_shadow_type(Gtk.ShadowType.NONE)
    panel.add(panel_scroll)

    _css = Gtk.CssProvider()
    _css.load_from_data(
        b"#mono{font-family:monospace;font-size:9pt;}"
        b"frame{border-left:1px solid alpha(@theme_fg_color,0.15);}"
    )
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), _css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    hbox.pack_start(area, True, True, 0)
    hbox.pack_start(panel, False, False, 0)
    win.add(hbox)

    def on_key(_w: Any, ev: Any) -> bool:
        if ev.keyval == Gdk.KEY_Escape and state["selected"]:
            select_node(None)
            return True
        return False

    win.connect("key-press-event", on_key)

    def _update_title() -> None:
        if data.error:
            header.set_subtitle(data.error)
            return
        stamp = time.strftime("%H:%M:%S")
        header.set_subtitle(f"{len(data.nodes)} nodes · {len(data.edges)} edges · read {stamp}")

    win.connect("destroy", Gtk.main_quit)
    win.connect("map", lambda *_: reload_now(warm=220))

    win.show_all()
    panel.hide()  # realised by show_all above; a node click un-hides it (B17)
    reload_now(warm=260)
    GLib.timeout_add(33, tick)
    Gtk.main()
    return 0


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH, help="path to graph.json")
    args = ap.parse_args(argv)

    # single instance -- a second click should not stack a window
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("a graph window is already open", file=sys.stderr)
        return 0

    try:
        return _run(args.graph)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())

# gen-ref: 35ffdd4f
