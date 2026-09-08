#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B15 · live integration check — the Wayland activity sensor, end to end.

The B15 bug was live-only: unit tests were green while the running daemon's focus
sensor was deaf. This script proves the fix on the real compositor, without a
human switching windows — it pops throwaway `zenity` dialogs to force real focus
changes and asserts the events flow through a real `ActivityCollector` (shared
`WaylandConnection`, real `ext-foreign-toplevel` + `ext-idle-notify`).

  scripts/b15_live_check.py            # run the checks, print a report
  scripts/b15_live_check.py --json     # machine-readable

Exit 0 = all checks passed. Exit 1 = a check failed. Exit 2 = could not run
(no compositor / no zenity / no pywayland).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.sensing.activity.collector import ActivityCollector

_ZENITY = shutil.which("zenity")


class _Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append({"check": name, "ok": ok, "detail": detail})

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks)

    def render(self) -> str:
        lines = ["", "=" * 68, "B15 LIVE INTEGRATION CHECK", "=" * 68]
        for c in self.checks:
            mark = "PASS" if c["ok"] else "FAIL"
            lines.append(f"  [{mark}] {c['check']}")
            lines.append(f"         {c['detail']}")
        lines.append("-" * 68)
        lines.append(f"  RESULT: {'ALL PASSED' if self.ok else 'FAILURES ABOVE'}")
        lines.append("=" * 68)
        return "\n".join(lines)


def _pop_zenity(text: str, seconds: int) -> None:
    """Raise a dialog that grabs focus, then auto-dismisses."""
    subprocess.Popen(
        ["setsid", _ZENITY, "--info", "--text", text, f"--timeout={seconds}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _run(report: _Report) -> None:
    bus = EventBus.get_instance()
    await bus.start()

    switches: list[dict[str, Any]] = []
    idle_events: list[str] = []

    async def on_switch(ev: Any) -> None:
        switches.append(dict(ev.payload))

    async def on_idle(ev: Any) -> None:
        idle_events.append("idle")

    async def on_active(ev: Any) -> None:
        idle_events.append("active")

    bus.subscribe(EventType.APP_SWITCH, on_switch)
    bus.subscribe(EventType.IDLE_DETECTED, on_idle)
    bus.subscribe(EventType.ACTIVITY_DETECTED, on_active)

    cfg = Config.from_file(os.path.join(REPO, "neuropaca.b13.toml"))
    collector = ActivityCollector(bus, cfg)
    await collector.initialize()
    await collector.start()
    await asyncio.sleep(1.0)  # let the shared connection connect + prime

    # --- check 1: the collector came up alive ------------------------------
    h = collector.health()
    report.add(
        "collector starts and both Wayland halves are alive",
        "idle✓" in h.detail and "window✓" in h.detail and h.ok,
        f"health.ok={h.ok} detail={h.detail!r}",
    )

    # --- check 2: exactly ONE shared Wayland connection ------------------
    # The bug was two Display connections (the second went deaf + segfaulted).
    conn = collector._wl_conn
    report.add(
        "the collector uses exactly ONE shared Wayland connection for both protocols",
        conn is not None and len(conn._handlers) == 2 and conn._display is not None,
        f"handlers on the connection: {len(conn._handlers) if conn else 0} "
        f"(idle-notify + toplevel-info), one Display",
    )

    # --- check 3: forced focus changes produce APP_SWITCH events ----------
    baseline = len(switches)
    n_pops = 5
    for i in range(n_pops):
        _pop_zenity(f"b15 focus check {i + 1}/{n_pops}", 2)
        await asyncio.sleep(3.0)
    await asyncio.sleep(2.0)
    gained = len(switches) - baseline
    report.add(
        f"{n_pops} forced window focus changes register as APP_SWITCH events",
        gained >= n_pops,  # each dialog: focus in (+ maybe focus back out)
        f"APP_SWITCH events gained: {gained} (expected >= {n_pops}); "
        f"app_ids seen: {sorted({s.get('app_id', '?') for s in switches[baseline:]})}",
    )

    # --- check 4: the payload shape is the B14 contract ------------------
    sample = switches[-1] if switches else {}
    keys_ok = {"app_id", "webapp", "webapp_domain", "previous_app_id", "previous_webapp"} <= set(
        sample
    )
    report.add(
        "APP_SWITCH payload carries the B14 fields and NO raw title",
        keys_ok and "title" not in sample,
        f"sample payload keys: {sorted(sample)}",
    )

    # --- check 5: health still healthy after the run --------------------
    h2 = collector.health()
    report.add(
        "collector still healthy after the focus storm",
        h2.ok and "window✓" in h2.detail,
        f"health.ok={h2.ok} detail={h2.detail!r} · idle transitions seen: {idle_events}",
    )

    await collector.stop()
    await asyncio.sleep(0.3)  # let the pump task unwind cleanly
    report.add(
        "collector.stop() tears the shared connection down without error",
        collector._wl_conn is not None and collector._wl_conn._display is None,
        "shared WaylandConnection disconnected",
    )
    await bus.stop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.environ.get("WAYLAND_DISPLAY"):
        print("SKIP: no $WAYLAND_DISPLAY", file=sys.stderr)
        return 2
    if _ZENITY is None:
        print("SKIP: zenity not installed (needed to force focus changes)", file=sys.stderr)
        return 2
    try:
        import pywayland  # noqa: F401
    except ImportError:
        print("SKIP: pywayland not installed", file=sys.stderr)
        return 2

    report = _Report()
    started = time.time()
    try:
        asyncio.run(_run(report))
    except Exception as exc:
        report.add("script ran to completion", False, f"unhandled exception: {exc!r}")
    report.checks.append(
        {"check": "_elapsed_seconds", "ok": True, "detail": f"{time.time() - started:.1f}"}
    )

    if args.json:
        print(json.dumps({"ok": report.ok, "checks": report.checks}, indent=2))
    else:
        print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

# gen-ref: 7bf3f9a3
