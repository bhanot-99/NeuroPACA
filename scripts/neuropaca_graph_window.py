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

Imports nothing from `src/` (the venv's editable install *is* the running
daemon), opens no socket, and only ever reads the graph file. A single-instance
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

    def sync(self, data: GraphData) -> None:
        ids = set(data.nodes)
        for gone in set(self.pos) - ids:
            del self.pos[gone]
            self.pinned.discard(gone)
        n = max(len(ids), 1)
        radius = 24.0 * math.sqrt(n)
        for node_id in ids - set(self.pos):
            anchor = data.domain_of.get(node_id)
            if anchor and anchor in self.pos:
                ax, ay = self.pos[anchor]
                self.pos[node_id] = [
                    ax + self._rng.uniform(-40, 40),
                    ay + self._rng.uniform(-40, 40),
                ]
            elif node_id == ROOT_ID:
                self.pos[node_id] = [0.0, 0.0]
            else:
                ang = self._rng.uniform(0, 2 * math.pi)
                self.pos[node_id] = [
                    radius * math.cos(ang) * self._rng.uniform(0.3, 1.0),
                    radius * math.sin(ang) * self._rng.uniform(0.3, 1.0),
                ]
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
    }

    def reload_now(*, warm: int) -> None:
        if data.reload(graph_path):
            layout.sync(data)
            layout.temperature = max(layout.temperature, 0.9)
            layout.step(data, warm)
            state["settle_frames"] = 200
            if not state["fitted"] and data.nodes:
                fit_view()
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

            if node_id.startswith("ephemeral:"):
                cr.set_dash([2.0, 2.0])
            cr.set_source_rgba(*INK, 0.0 if dim else (0.9 if node_id == hover else 0.35))
            cr.set_line_width(1.5 if node_id == hover else 1.0)
            cr.arc(px, py, r, 0, 2 * math.pi)
            cr.stroke()
            cr.set_dash([])

            if data.is_hub(node_id) or node_id == hover or node_id in neighbours or show_all_labels:
                label = str(data.nodes[node_id].get("label") or node_id)
                _label(
                    cr, PangoCairo, Pango, px, py + r + 3, label, bold=data.is_hub(node_id), dim=dim
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

    # ---- interaction --------------------------------------------------- #
    def on_press(_w: Any, ev: Any) -> bool:
        if ev.button == 1:
            hit = node_at(ev.x, ev.y)
            if hit:
                state["drag_node"] = hit
                layout.pinned.add(hit)
            else:
                state["pan"] = (ev.x, ev.y, state["tx"], state["ty"])
        elif ev.button == 3:
            hit = node_at(ev.x, ev.y)
            if hit:
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
    win.add(area)

    def _update_title() -> None:
        if data.error:
            header.set_subtitle(data.error)
            return
        stamp = time.strftime("%H:%M:%S")
        header.set_subtitle(f"{len(data.nodes)} nodes · {len(data.edges)} edges · read {stamp}")

    win.connect("destroy", Gtk.main_quit)
    win.connect("map", lambda *_: reload_now(warm=220))

    win.show_all()
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
