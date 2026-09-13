#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""s1-mail-two-way-filter · one-shot graph cleanup.

Removes all `thread:` and `person:` nodes (and every edge touching them)
from `data/graph.json`. These were created unconditionally before the
two-way filter was introduced and carry no meaningful signal.

Run this ONCE while the NeuroPACA daemon is stopped, on the
`s1-mail-two-way-filter` branch, before restarting.

Usage:
    python scripts/cleanup_mail_nodes.py [--graph data/graph.json] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_MAIL_PREFIXES = ("thread:", "person:")


def _is_mail_node(node_id: str) -> bool:
    return node_id.startswith(_MAIL_PREFIXES)


def cleanup(graph_path: Path, *, dry_run: bool = False) -> None:
    if not graph_path.exists():
        print(f"ERROR: graph file not found: {graph_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(graph_path.read_text(encoding="utf-8"))

    nodes_before: list[dict] = data.get("nodes", [])
    edges_before: list[dict] = data.get("edges", [])

    mail_ids: set[str] = {n["id"] for n in nodes_before if _is_mail_node(n["id"])}

    nodes_after = [n for n in nodes_before if n["id"] not in mail_ids]
    edges_after = [
        e
        for e in edges_before
        if e.get("source") not in mail_ids and e.get("target") not in mail_ids
    ]

    removed_nodes = len(nodes_before) - len(nodes_after)
    removed_edges = len(edges_before) - len(edges_after)

    print(f"Nodes before : {len(nodes_before):6d}")
    print(f"Nodes after  : {len(nodes_after):6d}  (removing {removed_nodes})")
    print(f"Edges before : {len(edges_before):6d}")
    print(f"Edges after  : {len(edges_after):6d}  (removing {removed_edges})")

    if dry_run:
        print("\n[dry-run] No files written.")
        return

    # Write backup
    backup_path = graph_path.with_suffix(".json.pre-mail-filter-backup")
    shutil.copy2(graph_path, backup_path)
    print(f"\nBackup written → {backup_path}")

    # Build cleaned payload
    data["nodes"] = nodes_after
    data["edges"] = edges_after
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    # Atomic write (temp → fsync → replace)
    dir_ = graph_path.parent
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, graph_path)
    except Exception:
        os.unlink(tmp)
        raise

    print(f"Graph saved   → {graph_path}")
    print(f"\nDone. Removed {removed_nodes} mail nodes and {removed_edges} edges.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove mail nodes from graph.json")
    parser.add_argument(
        "--graph",
        default="data/graph.json",
        help="Path to graph.json (default: data/graph.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be removed without writing anything",
    )
    args = parser.parse_args()
    cleanup(Path(args.graph), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
