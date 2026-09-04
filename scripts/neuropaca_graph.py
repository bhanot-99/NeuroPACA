#!/usr/bin/env python3
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
    Node colour is `node_type`, node size is `relevance_score`, edge thickness is
    `weight` -- so a thick line is a pair the Hebbian update has reinforced many
    times, which is the one thing a node/edge count cannot show you. Every node
    and edge carries `created_at`, so the page can replay the window: drag the
    scrubber and watch the graph build itself.

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
from datetime import datetime
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
LABEL_THRESHOLD = 3.0


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


def build_payload(graph: dict[str, Any]) -> dict[str, Any]:
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
