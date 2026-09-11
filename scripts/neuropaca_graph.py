#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Render `data/graph.json` as a browsable force-directed graph (B9 companion).

    python scripts/neuropaca_graph.py              # write + open in the browser
    python scripts/neuropaca_graph.py --no-open    # just write it
    python scripts/neuropaca_graph.py --out /tmp/g.html --graph data/graph.json

WHY THIS LIVES IN scripts/ AND NOT src/
    The venv is an editable install of `src/`, so anything under `src/` is the
    running daemon's code. This is a read-only viewer built to be safe to add,
    edit, and run *while a 7-day soak is in flight* -- it imports nothing from
    the package, opens no socket, and only reads the graph file the daemon
    already writes on its save interval.

ZERO EGRESS (rules.md §6)
    The output is one self-contained HTML file: no CDN, no webfont, no fetch.
    `graph_view_template.html` carries that guarantee; this module only splices
    a JSON payload into it.

WHAT IT SHOWS
    Every node is drawn in a colour *and* a size band set by its class -- the
    person (`YOU`), the ten routing hubs, then apps / web apps / concepts /
    insights / idle thoughts. Size within a class tracks `relevance_score` and
    degree; edge thickness tracks `weight`. Every node and edge carries
    `created_at`, so the page can replay the window: drag the scrubber and watch
    the graph build itself.

    The layout is a force simulation that COOLS AND STOPS -- it settles in a
    couple of seconds and then the canvas stops repainting, so the picture holds
    still. A collision force keeps nodes off each other. Dragging a node,
    scrubbing time or toggling a filter re-heats it briefly.

STALENESS
    `GraphMemory.save()` runs on the scheduler's interval (default 300 s), so the
    file is up to five minutes behind the live daemon. Irrelevant over a week;
    worth knowing if you regenerate twice in a minute and see nothing change.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "graph_view_template.html"
PLACEHOLDER = "/*__DATA__*/null"

DEFAULT_GRAPH = REPO / "data" / "graph.json"
DEFAULT_OUT = REPO / "data" / "graph_view.html"

ROOT_ID = "YOU"
DOMAIN_PREFIX = "domain:"
# Labels are decluttered by collision in the page, but a floor keeps the
# candidate list short on a large graph -- 0 would offer every node and throw
# nearly all of them away.
# V-2 rescaled the score (a no-activity node now sits near 0, not ~3): 1.0 is
# about the live graph's median.
LABEL_THRESHOLD = 1.0


def _parse_dt(value: str) -> datetime:
    """ISO-8601 as GraphMemory writes it (`...+00:00`, microseconds included)."""
    return datetime.fromisoformat(value)


def load_graph(path: Path) -> dict[str, Any]:
    """Read the graph file. Exits with a usable message rather than a traceback."""
    try:
        payload = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise SystemExit(
            f"no graph at {path}\n"
            "The daemon writes it on its save interval — start neuropacad and wait a tick."
        ) from None
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from None
    if not isinstance(payload, dict) or "nodes" not in payload:
        raise SystemExit(f"{path} is not a NeuroPACA graph (no 'nodes' key)")
    return payload


def derive_domains(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, str]:
    """Map each node id to the `domain:*` hub it is wired to, if any.

    Layout only: clustering members around their own domain is what stops the
    whole graph collapsing into one hairball. First edge wins, so the result is
    deterministic for a given file. A node touching several domains is a bridge
    and gets whichever came first -- `bridge_value` already scores that properly,
    the layout just needs somewhere to put it.
    """
    ids = {n["id"] for n in nodes}
    hubs = {i for i in ids if i.startswith(DOMAIN_PREFIX)}
    out: dict[str, str] = {}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s in hubs and t in ids and t not in hubs and t not in out:
            out[t] = s
        elif t in hubs and s in ids and s not in hubs and s not in out:
            out[s] = t
    return out


def build_ticks(t0: datetime, span: float, count: int = 7) -> list[str]:
    """Evenly spaced axis labels across the recorded window, in local time."""
    if span <= 0:
        return [t0.astimezone().strftime("%H:%M")]
    fmt = "%d %b" if span > 172800 else "%H:%M"
    step = span / (count - 1)
    return [
        datetime.fromtimestamp(t0.timestamp() + step * i).astimezone().strftime(fmt)
        for i in range(count)
    ]


# --------------------------------------------------------------------------- #
# the node detail panel: what a click shows, and the plain-English "what the   #
# system is learning here" line. Built here, not in the page, so it is tested  #
# and deterministic — no model writes it (rules: extractive before generative) #
# --------------------------------------------------------------------------- #
_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ephemeral:", "probe"),
    ("insight:", "insight"),
    ("idle:", "thought"),
    ("action:", "action"),
    ("webapp:", "webapp"),
    ("app:", "app"),
)
_SIGNAL_WORDS = {
    "idle": "away from the computer",
    "high_load": "under heavy load",
    "distraction": "switching around a lot",
    "focus_session": "in a focused stretch",
    "working_set_change": "opening and closing heavy apps",
    "memory_pressure": "short on memory",
    "heavy_app_started": "starting a heavy app",
}
_LAYER_WORDS = {
    "L3": "pattern detector",
    "L4": "learning layer",
    "diagnosis": "pattern detector",
    "learning": "learning layer",
}
_FACT_KINDS = ("probe", "insight", "thought")


def node_kind(node_id: str, node_type: str) -> str:
    """What a node *is*, for the panel: root, domain, app, webapp, insight,
    thought, probe, action — or its stored node_type for anything else."""
    if node_id == ROOT_ID:
        return "root"
    if node_id.startswith(DOMAIN_PREFIX):
        return "domain"
    for prefix, kind in _KIND_PREFIXES:
        if node_id.startswith(prefix):
            return kind
    return node_type


class _Context:
    """One pass over the graph: names, kinds, adjacency, ranks, back-references."""

    def __init__(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
        self.nodes = {n["id"]: n for n in nodes}
        self.kind = {n["id"]: node_kind(n["id"], str(n["node_type"])) for n in nodes}
        # (other, relation, "out" | "in", weight)
        self.adj: dict[str, list[tuple[str, str, str, float]]] = {i: [] for i in self.nodes}
        for e in edges:
            s, t = e["source"], e["target"]
            if s in self.adj and t in self.adj and s != t:
                rel, w = str(e.get("relation", "related_to")), float(e.get("weight", 0.0))
                self.adj[s].append((t, rel, "out", w))
                self.adj[t].append((s, rel, "in", w))
        used = [i for i in self.nodes if self.kind[i] in ("app", "webapp")]
        used.sort(key=lambda i: (-float(self.nodes[i].get("relevance_score", 0.0)), i))
        self.rank = {i: r + 1 for r, i in enumerate(used)}
        self.ranked = len(used)
        self.about: dict[str, list[str]] = {}  # node id -> generated facts about it
        for i, n in self.nodes.items():
            spec = n.get("spec")
            if isinstance(spec, dict):
                for ref in spec.get("refs", ()):
                    self.about.setdefault(str(ref), []).append(i)

    def name(self, node_id: str) -> str:
        node = self.nodes.get(node_id)
        if node is not None:
            return str(node.get("label") or node_id)
        return node_id.split(":", 1)[-1] or node_id


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _ago(value: Any, now: datetime) -> str | None:
    if not value:
        return None
    try:
        then = _parse_dt(str(value))
    except ValueError:
        return None
    gap = now - then
    if gap < timedelta(minutes=2):
        return "just now"
    if gap < timedelta(hours=1):
        return f"{int(gap.total_seconds() // 60)} min ago"
    if gap < timedelta(days=1):
        return f"{int(gap.total_seconds() // 3600)} h ago"
    return _plural(gap.days, "day") + " ago"


def _article(noun: str) -> str:
    """'anomaly' -> 'an anomaly', 'distraction' -> 'a distraction'."""
    if noun in ("something", ""):
        return "something"
    return ("an " if noun[:1].lower() in "aeiou" else "a ") + noun


def _strength(w: float) -> str:
    return "strongly" if w >= 0.5 else "often" if w >= 0.2 else "sometimes"


def describe_node(node: dict[str, Any], ctx: _Context, now: datetime) -> str:
    """Plain English: what this node is, and what the system is learning from
    it. Built only from the node's own recorded data and its edges."""
    nid, kind, name = node["id"], ctx.kind[node["id"]], ctx.name(node["id"])
    links = ctx.adj.get(nid, [])
    uses = int(node.get("access_count", 0))

    if kind == "root":
        topics = [ctx.name(o) for o, _r, _d, _w in links if ctx.kind.get(o) == "domain"]
        direct = [o for o, _r, _d, _w in links if ctx.kind.get(o) not in ("domain",)]
        parts = ["This is you — the centre of everything the system learns."]
        if topics:
            count = _plural(len(topics), "topic")
            parts.append(f"It sorts your activity into {count}: {_join(sorted(topics))}.")
        if direct:
            parts.append(
                f"{_plural(len(direct), 'item')} hang off you directly; those are things "
                "it has not yet filed under a topic."
            )
        return " ".join(parts)

    if kind == "domain":
        members = sorted({ctx.name(o) for o, r, d, _w in links if r == "part_of" and d == "in"})
        if not members:
            return (
                f"A topic the system can file your activity under: {name}. Nothing is "
                "filed here yet — it appears once you use an app that belongs to it."
            )
        shown = members[:6] + ([f"{len(members) - 6} more"] if len(members) > 6 else [])
        return (
            f"A topic the system files your activity under: {name}. "
            f"{len(members)} {'app or site' if len(members) == 1 else 'apps or sites'} "
            f"belong{'s' if len(members) == 1 else ''} here: {_join(shown)}. "
            f"Every time you use one of them it counts as {name.lower()} activity — "
            "that is how the system learns what kind of work you are doing."
        )

    if kind in ("app", "webapp"):
        parts = []
        when = _ago(node.get("last_accessed"), now)
        parts.append(
            f"You have used {name} {_plural(uses, 'time')}"
            + (f", most recently {when}." if when else ".")
        )
        if nid in ctx.rank:
            parts.append(
                f"It is #{ctx.rank[nid]} of your {ctx.ranked} apps and sites by relevance "
                f"({float(node.get('relevance_score', 0.0)):.1f} out of 10)."
            )
        topics = [
            ctx.name(o)
            for o, r, d, _w in links
            if r == "part_of" and d == "out" and ctx.kind.get(o) == "domain"
        ]
        hosts = [
            ctx.name(o)
            for o, r, d, _w in links
            if r == "part_of" and d == "out" and ctx.kind.get(o) == "app"
        ]
        if kind == "webapp" and hosts:
            parts.append(f"It is a site you use inside {_join(hosts)}.")
        parts.append(
            f"The system files it under {_join(topics)}."
            if topics
            else "It is not filed under a topic yet."
        )
        partners = sorted(
            (
                (w, ctx.name(o))
                for o, r, _d, w in links
                if r == "related_to" and w > 0 and ctx.kind.get(o) in ("app", "webapp")
            ),
            reverse=True,
        )
        if partners:
            said = [f"{p} ({_strength(w)})" for w, p in partners[:4]]
            parts.append(
                f"From how you switch between apps, it has learned that {name} goes "
                f"with {_join(said)}."
            )
        else:
            parts.append(
                f"It has not yet learned what {name} goes with — no regular co-use so far."
            )
        facts = ctx.about.get(nid, [])
        insights = sum(1 for f in facts if ctx.kind.get(f) == "insight")
        thoughts = sum(1 for f in facts if ctx.kind.get(f) == "thought")
        if insights:
            parts.append(f"It has noticed {_plural(insights, 'unusual pattern')} around it.")
        if thoughts:
            parts.append(
                f"While you were away it asked itself {_plural(thoughts, 'question')} about it."
            )
        if node.get("ram_mb"):
            measured = _ago(node.get("resources_at"), now)
            parts.append(
                f"Last measured using about {float(node['ram_mb']):.0f} MiB of memory"
                + (f" ({measured})." if measured else ".")
            )
        return " ".join(parts)

    spec = node.get("spec") if isinstance(node.get("spec"), dict) else {}
    refs = [ctx.name(str(r)) for r in spec.get("refs", ())]
    facet = str(spec.get("facet", ""))
    value = spec.get("value")

    if kind == "insight":
        category, _, signal = facet.partition("/")
        cited = sum(1 for _o, r, d, _w in links if r == "caused_by" and d == "in")
        parts = [
            f"The system noticed {'an' if category[:1] in 'aeiou' else 'a'} "
            f"{category or 'pattern'} involving {_join(refs) or 'your activity'} while you were "
            f"{_SIGNAL_WORDS.get(signal, signal.replace('_', ' ') or 'active')}"
            + (f" (confidence {float(value):.2f})." if value is not None else ".")
        ]
        if cited:
            parts.append(f"{_plural(cited, 'follow-up note')} back it up.")
        parts.append(
            "Insights like this add pressure to an app; when two independent sources "
            "agree, the system tells you."
        )
        return " ".join(parts)

    if kind == "thought":
        question = str(spec.get("text") or node.get("label") or "")
        return (
            f"While you were away, the system asked itself: “{question}” It is exploring "
            f"how {_join(refs)} relate. It has come back to this "
            f"{_plural(max(uses, 1), 'time')}. Idle thoughts are kept for 48 hours."
        )

    if kind == "probe":
        group, _, detail = facet.partition("/")
        subject = _join(refs) or "an app"
        if group == "summary":
            layer, _, cause = detail.partition(".")
            who = _LAYER_WORDS.get(layer, "system")
            amount = f"of {float(value):.2f} " if value is not None else ""
            return (
                f"A short-lived investigation note: pressure {amount}built up on {subject} "
                f"because the {who} spotted {_article(cause.replace('_', ' ') or 'something')}. "
                "Notes like this help the system decide whether to act; they are deleted "
                "after 14 days."
            )
        return (
            f"A short-lived note recording that the {_LAYER_WORDS.get(detail, detail or 'system')} "
            f"contributed evidence about {subject}. Deleted after 14 days."
        )

    if kind == "action":
        return f"A record of something the system did: {name}."
    what = str(node.get("node_type", "item")).replace("_", " ")
    return f"The system has seen this {what} {_plural(uses, 'time')}."


def _spec_view(spec: Any, ctx: _Context) -> dict[str, Any] | None:
    if not isinstance(spec, dict):
        return None
    return {
        "kind": spec.get("kind"),
        "facet": spec.get("facet"),
        "value": spec.get("value"),
        "text": spec.get("text"),
        "refs": [{"id": str(r), "label": ctx.name(str(r))} for r in spec.get("refs", ())],
    }


def build_payload(graph: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Turn the on-disk graph into what the template needs.

    Times become seconds offset from the earliest `created_at`, so the scrubber
    is a plain number line; the absolute `t0` rides along for display.
    """
    nodes = list(graph.get("nodes", []))
    edges = list(graph.get("edges", []))
    if not nodes:
        raise SystemExit("the graph has no nodes at all — nothing to draw")

    stamps = [_parse_dt(n["created_at"]) for n in nodes]
    stamps += [_parse_dt(e["created_at"]) for e in edges if e.get("created_at")]
    t0 = min(stamps)
    span = max((s - t0).total_seconds() for s in stamps)

    domains = derive_domains(nodes, edges)
    ctx = _Context(nodes, edges)
    moment = now or datetime.now().astimezone()
    hub_ids = [n["id"] for n in nodes if n["id"] == ROOT_ID or n["id"].startswith(DOMAIN_PREFIX)]
    domain_ids = sorted(i for i in hub_ids if i.startswith(DOMAIN_PREFIX))

    out_nodes = [
        {
            "id": n["id"],
            "node_type": str(n["node_type"]),
            "label": str(n.get("label", n["id"])),
            "t": round((_parse_dt(n["created_at"]) - t0).total_seconds(), 1),
            "score": round(float(n.get("relevance_score", 0.0)), 3),
            "access": int(n.get("access_count", 0)),
            "domain": domains.get(n["id"]),
            "ram": round(float(n["ram_mb"]), 1) if n.get("ram_mb") else None,
            "cpu": round(float(n["cpu_percent"]), 1) if n.get("cpu_percent") else None,
            # the detail panel
            "kind": ctx.kind[n["id"]],
            "about": describe_node(n, ctx, moment),
            "activity": round(float(n.get("activity", 0.0)), 2),
            "created": n.get("created_at"),
            "last_accessed": n.get("last_accessed"),
            "first_seen": n.get("first_seen_at"),
            "last_seen": n.get("last_seen_at"),
            "resources_at": n.get("resources_at"),
            "spec": _spec_view(n.get("spec"), ctx),
        }
        for n in nodes
    ]
    out_edges = [
        {
            "s": e["source"],
            "t": e["target"],
            "rel": str(e.get("relation", "related_to")),
            "at": (
                round((_parse_dt(e["created_at"]) - t0).total_seconds(), 1)
                if e.get("created_at")
                else 0.0
            ),
            "w": round(float(e.get("weight", 0.0)), 3),
        }
        for e in edges
    ]

    days = span / 86400.0
    window = f"{days:.1f} d" if days >= 1 else f"{span / 3600.0:.1f} h"
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "t0": t0.isoformat(),
        "span_seconds": round(span, 1),
        "subtitle": f"{len(out_nodes)} nodes · {len(out_edges)} edges · {window}",
        "ticks": build_ticks(t0, span),
        "hub_ids": hub_ids,
        "domain_ids": domain_ids,
        "root_id": ROOT_ID,
        "label_threshold": LABEL_THRESHOLD,
        "max_weight": round(max((e["w"] for e in out_edges), default=0.0), 3),
        "nodes": out_nodes,
        "edges": out_edges,
    }


def render(payload: dict[str, Any], template: Path = TEMPLATE) -> str:
    """Splice the payload into the template. Returns the finished HTML."""
    try:
        html = template.read_text("utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read the template {template}: {exc}") from None
    if PLACEHOLDER not in html:
        raise SystemExit(f"{template} has no {PLACEHOLDER} placeholder — wrong template?")
    # A literal `</script>` inside a JSON string would close the host <script>
    # element and spill the rest of the payload into the document as markup.
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return html.replace(PLACEHOLDER, blob, 1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render the NeuroPACA graph as an HTML view.")
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH, help="path to graph.json")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the HTML")
    ap.add_argument("--no-open", action="store_true", help="write the file, do not open a browser")
    args = ap.parse_args(argv)

    payload = build_payload(load_graph(args.graph))
    html = render(payload)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    # The graph is behavioural data about a person (rules.md §6) -- same 0600 the
    # daemon gives graph.json itself, not world-readable in a shared /tmp.
    args.out.chmod(0o600)

    print(f"{payload['subtitle']}  ->  {args.out}")
    if not args.no_open:
        webbrowser.open(args.out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())

# gen-ref: 077c99f9
