"""B9 · one soak measurement, taken over the L9 socket (BL-5).

WHY NOT journalctl.

The first draft of the sampler grepped `journalctl --user -u neuropacad` for
ACTIVITY_DETECTED and pressure, the way scripts/b9_soak_gate.sh does. Measured
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

`neuropaca health` over the unix socket has none of that dependency. It is the
daemon's own account of itself, it is structured rather than grepped, and its
counters are the ones the modules actually maintain.

Emits one JSON object on stdout, or `{}` when the daemon is unreachable -- an
unreachable daemon is a fact worth a row, not a reason to abort the soak.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from datetime import UTC, datetime
from typing import Any

_COUNTER = re.compile(r"(\d+)\s+([a-z][a-z-]*)")


def default_socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return f"{runtime}/neuropaca.sock"


def fetch_health(path: str, timeout: float = 10.0) -> dict[str, Any] | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall((json.dumps({"op": "health"}) + "\n").encode())
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if chunks[-1].endswith(b"\n"):
                    break
        reply = json.loads(b"".join(chunks).decode())
    except (OSError, json.JSONDecodeError):
        return None
    if not reply.get("ok"):
        return None
    health: dict[str, Any] | None = reply.get("health")
    return health


def parse_counters(detail: str) -> dict[str, int]:
    """`"0 transitions - 3 switches"` -> `{"transitions": 0, "switches": 3}`.

    Deliberately permissive: the module detail strings are human-facing and get
    reworded, and a sampler that raised on a rewording would end a soak on day
    four over a cosmetic change. Anything it fails to recognise simply does not
    appear, and the missing key reads as zero.
    """
    return {word: int(value) for value, word in _COUNTER.findall(detail)}


def module_counters(health: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {
        module["name"]: parse_counters(module.get("detail", ""))
        for module in health.get("modules", [])
    }


def build_sample(health: dict[str, Any] | None, actions: int = 0) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if health is None:
        return {"ts": now, "daemon_up": False}

    counters = module_counters(health)
    activity = counters.get("activity", {})
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
        "errors": errors,
        "actions": actions,
        "degraded": [m["name"] for m in health.get("modules", []) if not m.get("ok")],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One B9 soak sample as JSON.")
    parser.add_argument("--socket", default=None)
    parser.add_argument("--actions-log", default=None)
    args = parser.parse_args(argv)

    actions = 0
    if args.actions_log:
        try:
            with open(args.actions_log) as handle:
                actions = sum(1 for _ in handle)
        except OSError:
            actions = 0

    health = fetch_health(args.socket or default_socket_path())
    json.dump(build_sample(health, actions), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
