"""Recorded traces for the B2.5b exit criterion (phases.md B2.5b, D-10).

    "the two patterns fire on fixtures and stay silent on negatives"

Unlike the B3 traces (system/filesystem `MetricSnapshot`s only), a B2.5b trace is
an ordered list of **mixed events** — `metric` readings and `app_switch` events —
each with an integer `offset` (seconds from `base`). `test_b2_5_recorded_fixtures`
loads them, rebuilds `METRIC_COLLECTED` / `APP_SWITCH` events at those timestamps,
and replays them through a real `SignalCorrelator` (real `EventBus` + `GraphMemory`,
the shipped `data/app_map.default.toml`, no sleeping).

Everything is a fixed table — no RNG — so the files and every asserted value are
byte-for-byte reproducible.

    python -m tests.fixtures.generate_b2_5_traces        # writes the three files
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent

POLL_SECONDS = 60
_BASE_ISO = "2026-01-01T00:00:00+00:00"

TRACE_FOCUS = FIXTURE_DIR / "trace_focus.json"
TRACE_DISTRACTION = FIXTURE_DIR / "trace_distraction.json"
TRACE_CALM = FIXTURE_DIR / "trace_calm.json"

# Anchored in data/app_map.default.toml (asserted in test_app_map.py).
FOCUS_APP = "dev.zed.Zed"  # -> domain:engineering
NEUTRAL_APP = "org.gnome.Nautilus"  # -> domain:tools (not a focus domain)


def _metric(offset: int, collector: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "metric", "offset": offset, "collector_name": collector, "data": data}


def _system(offset: int, cpu: float) -> dict[str, Any]:
    return _metric(offset, "system", {"cpu_percent": cpu})


def _switch(offset: int, app_id: str, title: str = "") -> dict[str, Any]:
    return {"kind": "app_switch", "offset": offset, "app_id": app_id, "title": title}


def _trace(name: str, description: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "base": _BASE_ISO,
        "poll_seconds": POLL_SECONDS,
        "events": events,
    }


def build_focus_trace() -> dict[str, Any]:
    """Switch to Zed at t=0, then 22 min of active CPU on the same app.
    FOCUS_SESSION fires exactly once, at the first system poll past 20 min."""
    events: list[dict[str, Any]] = [_switch(0, FOCUS_APP, "neuropaca — correlator.py")]
    for i in range(23):  # t = 0 .. 1320 s
        events.append(_system(i * POLL_SECONDS, 41.0 + (i % 3)))
    return _trace(
        "focus",
        f"{FOCUS_APP} focused with CPU ~42% for 22 min; FOCUS_SESSION fires once "
        f"and writes app:{FOCUS_APP} -> domain:engineering.",
        events,
    )


def build_distraction_trace() -> dict[str, Any]:
    """Six app switches inside 90 s. DISTRACTION fires exactly once."""
    apps = ["a.Alpha", "b.Bravo", "c.Charlie", "d.Delta", "e.Echo", "f.Foxtrot"]
    events = [_switch(i * 15, app_id, "") for i, app_id in enumerate(apps)]  # 0..75 s
    events.append(_system(0, 33.0))
    events.append(_system(90, 34.0))
    return _trace(
        "distraction",
        "6 app switches in 90 s across 6 unclassified apps; DISTRACTION fires once, "
        "FOCUS_SESSION stays silent (no focus-domain app).",
        events,
    )


def build_calm_trace() -> dict[str, Any]:
    """Zed briefly, then a non-focus app for the rest of the session, steady CPU.
    Neither activity pattern fires (nor HIGH_LOAD / IDLE)."""
    events: list[dict[str, Any]] = [
        _switch(0, FOCUS_APP, "scratch"),
        _switch(300, NEUTRAL_APP, "Files"),
    ]
    for i in range(23):  # t = 0 .. 1320 s
        events.append(_system(i * POLL_SECONDS, 38.0 + (i % 4)))
    return _trace(
        "calm",
        f"{FOCUS_APP} for 5 min then {NEUTRAL_APP} (domain:tools) for the rest, "
        "CPU ~40%; no pattern fires.",
        events,
    )


_BUILDERS: tuple[tuple[Path, Callable[[], dict[str, Any]]], ...] = (
    (TRACE_FOCUS, build_focus_trace),
    (TRACE_DISTRACTION, build_distraction_trace),
    (TRACE_CALM, build_calm_trace),
)


def write_all() -> list[Path]:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for path, builder in _BUILDERS:
        path.write_text(json.dumps(builder(), indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    for path in write_all():
        payload = json.loads(path.read_text(encoding="utf-8"))
        print(f"wrote {path} — {len(payload['events'])} events")


if __name__ == "__main__":
    main()
