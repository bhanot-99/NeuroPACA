#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""One-off manual tidy of `data/graph.json` (B17 companion).

    /usr/bin/python3 scripts/graph_cleanup.py              # dry run — show the diff
    /usr/bin/python3 scripts/graph_cleanup.py --apply      # write it back (atomic)

WHAT IT DOES

  1. folds duplicate `app:` / `webapp:` nodes onto one canonical id, using the
     [alias] table in data/app_identity.default.toml plus the same tiny
     normaliser the daemon uses (lowercase, strip -browser/-desktop, separators
     -> "-"). access_count sums; the census's ram_mb/cpu_percent are kept; every
     edge on a victim rewires to the survivor.
  2. drops nodes whose bare id is in [non_app] (thread labels, shells) and that
     were never focused (access_count 0).
  3. drops every non-hub node left with zero edges.

The daemon does (1) and (2) automatically at boot (orchestrator ->
`GraphMemory.canonicalise_app_nodes`). This script also does (3) — a harder
sweep than the daemon's `link_orphan_nodes`, which *links* orphans rather than
removing them — and lets you eyeball the result before the daemon touches it.

Stdlib only. Reads/writes the graph file; opens no socket; imports nothing from
`src/`. Stop the daemon before `--apply` so it does not save over the change.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DEFAULT_GRAPH = REPO / "data" / "graph.json"
DEFAULT_IDENTITY = REPO / "data" / "app_identity.default.toml"

HUB_IDS = {"YOU"} | {
    f"domain:{s}"
    for s in (
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
}
_STRIP_SUFFIXES = ("-browser", "-desktop", "-bin", "-gtk", "-wayland", "-stable", "-nightly")
_THREAD_RE = re.compile(
    r"^(MainThread|Thread-\d+.*|asyncio_\d+|ThreadPoolExecutor.*|tokio-runtime-w.*)$"
)


def _normalise(raw: str) -> str:
    s = raw.strip().lower()
    for suffix in _STRIP_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    s = re.sub(r"[\s._]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-") or raw.strip().lower()


def _load_identity(path: Path) -> tuple[dict[str, str], set[str]]:
    try:
        raw = tomllib.loads(path.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}, set()
    alias_raw = raw.get("alias") or {}
    alias: dict[str, str] = {}
    if isinstance(alias_raw, dict):
        for k, v in alias_raw.items():
            if isinstance(v, str):
                canon = _normalise(v)
                alias[str(k)] = canon
                alias.setdefault(str(k).lower(), canon)
    non_app = {str(x).lower() for x in (raw.get("non_app") or []) if isinstance(x, str)}
    return alias, non_app


def _resolve(raw: str, alias: dict[str, str]) -> str:
    return alias.get(raw) or alias.get(raw.lower()) or _normalise(raw)


def _is_non_app(raw: str, non_app: set[str]) -> bool:
    low = raw.strip().lower()
    if low in non_app or low in {"sh", "bash", "zsh", "?"}:
        return True
    return bool(_THREAD_RE.match(raw.strip()))


def _degree(node_id: str, edges: list[dict[str, Any]]) -> int:
    return sum(1 for e in edges if node_id in (e.get("source"), e.get("target")))


def clean(
    payload: dict[str, Any], alias: dict[str, str], non_app: set[str]
) -> tuple[dict[str, Any], list[str]]:
    nodes: dict[str, dict[str, Any]] = {n["id"]: n for n in payload.get("nodes", [])}
    edges: list[dict[str, Any]] = list(payload.get("edges", []))
    log: list[str] = []

    # (0) drop thread-label / bare-shell nodes the census mistook for apps
    for nid in list(nodes):
        if nid.startswith("app:") and _is_non_app(nid[4:], non_app):
            del nodes[nid]
            log.append(f"drop   {nid}  (not an application)")
    edges = [e for e in edges if e.get("source") in nodes and e.get("target") in nodes]

    # (1) fold duplicate app:/webapp: nodes
    groups: dict[str, list[str]] = {}
    for nid in list(nodes):
        for prefix in ("app:", "webapp:"):
            if nid.startswith(prefix):
                canon = f"{prefix}{_resolve(nid[len(prefix) :], alias)}"
                groups.setdefault(canon, []).append(nid)
                break

    remap: dict[str, str] = {}
    for canon_id, members in groups.items():
        if len(members) == 1 and members[0] == canon_id:
            continue
        present = [m for m in members if m in nodes]
        if not present:
            continue
        if canon_id in present:
            survivor = canon_id
        else:
            old = max(present, key=lambda m: _degree(m, edges))
            bare = canon_id.split(":", 1)[1]
            nodes[canon_id] = {**nodes.pop(old), "id": canon_id, "label": bare}
            remap[old] = canon_id
            log.append(f"rename {old} -> {canon_id}")
            survivor = canon_id
            present = [canon_id if m == old else m for m in present]
        for victim in present:
            if victim == survivor:
                continue
            v = nodes.pop(victim)
            s = nodes[survivor]
            s["access_count"] = int(s.get("access_count", 0)) + int(v.get("access_count", 0))
            if float(v.get("ram_mb", 0)) > float(s.get("ram_mb", 0)):
                s["ram_mb"] = v.get("ram_mb")
                s["cpu_percent"] = v.get("cpu_percent")
            for key in ("first_seen_at",):
                if v.get(key) and (not s.get(key) or str(v[key]) < str(s[key])):
                    s[key] = v[key]
            remap[victim] = survivor
            log.append(f"merge  {victim} -> {survivor}")

    for e in edges:
        e["source"] = remap.get(e.get("source", ""), e.get("source"))
        e["target"] = remap.get(e.get("target", ""), e.get("target"))
    edges = [
        e
        for e in edges
        if e.get("source") != e.get("target")
        and e.get("source") in nodes
        and e.get("target") in nodes
    ]
    # de-dup edges on (source, target, relation)
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for e in edges:
        key = (e.get("source", ""), e.get("target", ""), e.get("relation", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    edges = deduped

    # (2) drop non-hub nodes with zero edges
    for nid in list(nodes):
        if nid not in HUB_IDS and _degree(nid, edges) == 0:
            del nodes[nid]
            log.append(f"drop   {nid}  (no edges)")

    payload["nodes"] = list(nodes.values())
    payload["edges"] = edges
    return payload, log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    ap.add_argument("--identity", type=Path, default=DEFAULT_IDENTITY)
    ap.add_argument("--apply", action="store_true", help="write the cleaned graph back")
    args = ap.parse_args(argv)

    try:
        payload = json.loads(args.graph.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read {args.graph}: {exc}", file=sys.stderr)
        return 1

    before_n, before_e = len(payload.get("nodes", [])), len(payload.get("edges", []))
    alias, non_app = _load_identity(args.identity)
    cleaned, log = clean(payload, alias, non_app)
    after_n, after_e = len(cleaned["nodes"]), len(cleaned["edges"])

    for line in log:
        print(line)
    print(f"\nnodes {before_n} -> {after_n}   edges {before_e} -> {after_e}")

    if not log:
        print("nothing to clean.")
        return 0
    if not args.apply:
        print("\n(dry run — pass --apply to write)")
        return 0

    tmp = args.graph.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cleaned), "utf-8")
    os.replace(tmp, args.graph)
    print(f"\nwrote {args.graph}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
