# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""One soak measurement, read from the daemon's own health-dump file (BL-5).

WHY NOT journalctl.

The first draft of the sampler grepped `journalctl --user -u neuropacad` for
ACTIVITY_DETECTED and pressure, the way scripts/soak_gate.sh does. Measured
on the target box 2026-09-03 that returns "No journal files were found":
journald ships `Storage=auto` and /var/log/journal does not exist, so the
journal is volatile and split into a system journal only -- there are no
per-user journal files to read at all.

That matters more than it looks. A gate check reading zero activity edges from
an empty journal is indistinguishable from a collector that is genuinely dead,
and it would refuse the soak -- or, worse, a soak sampling that way would record
a week of zeros for a system that was working the whole time. That is the B7
failure exactly: a measurement apparatus lying, and the lie being read as a
finding about the system.

WHY NOT THE L9 SOCKET ANY MORE.

This originally read `neuropaca health` over the L9 unix socket. That whole
interface (the socket, the CLI, the tray) was removed by user decision -- no
terminal/text control surface, superseded eventually by voice. The daemon's
own account of itself did not go away with it: `orchestration/orchestrator.py`
now periodically writes its own `health_check()` as JSON to
`config.health_dump_path` (atomically -- temp file + rename), and this script
reads that file instead of opening a socket. Same structured, authoritative
data (`SystemHealth`, straight from `asdict()`); the daemon's own account of
itself, same as before, no daemon-side control surface required to get it.

Emits one JSON object on stdout, or `{}` when the daemon is unreachable -- an
unreachable daemon is a fact worth a row, not a reason to abort the soak.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_COUNTER = re.compile(r"(?<![:\d.=])(\d+)\s+([a-z][a-z-]*)")


def default_health_dump_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "data" / "health.json")


def fetch_health(path: str) -> dict[str, Any] | None:
    try:
        health = json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(health, dict) or not health.get("ok"):
        return None
    return health


def parse_counters(detail: str) -> dict[str, int]:
    """`"0 transitions - 3 switches"` -> `{"transitions": 0, "switches": 3}`.

    Deliberately permissive: the module detail strings are human-facing and get
    reworded, and a sampler that raised on a rewording would end a soak on day
    four over a cosmetic change. Anything it fails to recognise simply does not
    appear, and the missing key reads as zero.

    The lookbehind `(?<![:\\d.=])` stops digits embedded in timestamps (the
    `30` in a `+05:30` UTC offset), decimals, or `key=value` pairs from being
    read as counter values — a real bug that reported 30 phantom errors for the
    entire first soak session.
    """
    return {word: int(value) for value, word in _COUNTER.findall(detail)}


def module_counters(health: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {
        module["name"]: parse_counters(module.get("detail", ""))
        for module in health.get("modules", [])
    }


def module_detail(health: dict[str, Any], name: str) -> str:
    for module in health.get("modules", []):
        if module.get("name") == name:
            return str(module.get("detail", ""))
    return ""


def hebbian_weight_stats(graph_path: str | None) -> dict[str, Any]:
    """T7 · best-effort read of the on-disk graph for the co-occurrence weight
    distribution. A daemon that is wiring co-activations shows a rising
    `weight_nonzero_fraction` and a `max` well above one step (`hebbian_delta`); a week of
    zeros is the T7 signature returning. Any read/parse failure yields `{}` --
    same philosophy as an unreachable daemon (a fact, not an abort)."""
    if not graph_path:
        return {}
    try:
        with open(graph_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        edges = payload.get("edges", payload.get("links", []))
        weights = [float(e.get("weight", 0.0)) for e in edges]
        rel = [
            float(e.get("weight", 0.0)) for e in edges if str(e.get("relation", "")) == "related_to"
        ]
    except (OSError, ValueError, AttributeError, TypeError):
        return {}
    if not weights:
        return {}
    nonzero = [w for w in weights if w > 0.0]
    return {
        "graph_edges_total": len(weights),
        "weight_nonzero_fraction": round(len(nonzero) / len(weights), 4),
        "max_cooccurrence_weight": round(max(rel), 4) if rel else 0.0,
        "mean_nonzero_weight": round(sum(nonzero) / len(nonzero), 4) if nonzero else 0.0,
    }


def build_sample(
    health: dict[str, Any] | None, actions: int = 0, graph_path: str | None = None
) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if health is None:
        return {"ts": now, "daemon_up": False, **hebbian_weight_stats(graph_path)}

    counters = module_counters(health)
    activity = counters.get("activity", {})
    activity_detail = module_detail(health, "activity")
    drive = counters.get("drive", {})

    # Every module reports its own error count in the same shape, so the total is
    # the sum rather than a hand-listed subset -- a module added in a later phase
    # is then counted without anyone remembering to come back here.
    errors = sum(c.get("errors", 0) for c in counters.values())

    return {
        "ts": now,
        "daemon_up": True,
        # uptime is what makes a daemon restart detectable: it goes DOWN. Every
        # cumulative counter below resets at the same instant, and the summary
        # needs to know that to avoid reading a reset as a drop to zero.
        "uptime_seconds": round(float(health.get("uptime_seconds", 0.0)), 1),
        "rss_mib": round(float(health.get("rss_mb", 0.0)), 1),
        "graph_nodes": int(health.get("graph_nodes", 0)),
        "graph_edges": int(health.get("graph_edges", 0)),
        "queue_depth": int(health.get("queue_depth", 0)),
        "events_dropped": int(health.get("events_dropped", 0)),
        # The gate's question, asked once a minute for a week: is the sensing
        # path producing anything at all?
        "activity_edges": activity.get("transitions", 0),
        "app_switches": activity.get("switches", 0),
        # B15 · the Wayland focus sensor. `window_ok` is the daemon's own live
        # `is_alive` verdict (not inferred); `reconnects` / `pump_errors` are the
        # shared connection's watchdog activity. A week of `window_ok=false`, or
        # reconnects climbing steadily, is the B15 §2a deafness / §7 flaky-start
        # signature the old soak could not see.
        "window_ok": "window✓" in activity_detail,
        "reconnects": activity.get("reconnects", 0),
        "pump_errors": activity.get("pump-errors", 0),
        # B13-B2 · the latest per-app census group count, from the sensing
        # module's health detail ("... census N groups"). 0 until the process
        # collector has produced a snapshot.
        "census_groups": counters.get("sensing", {}).get("groups", 0),
        # L3's correlated-signal count. A signal means L2 collected a snapshot
        # AND L3 correlated it, so it proves the sensing pipeline is producing
        # without depending on the user having switched apps or walked away --
        # the narrowness that failed the 2026-09-04 gate on a healthy box.
        "signals": counters.get("diagnosis", {}).get("signals", 0),
        "pressure_events": drive.get("contributions", 0),
        "pressure_low": drive.get("low", 0),
        "pressure_high": drive.get("high", 0),
        "insights": counters.get("learning", {}).get("insights", 0),
        "proposed": counters.get("action", {}).get("proposed", 0),
        # T7 · cumulative Hebbian co-occurrence edges created or strengthened by
        # the correlator's co-activation window. Flat at 0 for a week == the
        # driver is not firing (the original T7 signature).
        "hebbian_wired": counters.get("diagnosis", {}).get("hebbian", 0),
        "errors": errors,
        "actions": actions,
        "degraded": [m["name"] for m in health.get("modules", []) if not m.get("ok")],
        **hebbian_weight_stats(graph_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One soak sample as JSON.")
    parser.add_argument("--health-dump", default=None, help="path to the daemon's health.json")
    parser.add_argument("--actions-log", default=None)
    parser.add_argument(
        "--graph", default=None, help="path to data/graph.json for T7 weight stats (optional)"
    )
    args = parser.parse_args(argv)

    actions = 0
    if args.actions_log:
        try:
            with open(args.actions_log) as handle:
                actions = sum(1 for _ in handle)
        except OSError:
            actions = 0

    health = fetch_health(args.health_dump or default_health_dump_path())
    json.dump(build_sample(health, actions, args.graph), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# gen-ref: 7d7ef8f5
