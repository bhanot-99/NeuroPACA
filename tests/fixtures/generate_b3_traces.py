"""Recorded ``MetricSnapshot`` traces for the B3 exit criterion (phases.md B3).

The B3 exit line requires each pattern to "fire against a recorded fixture and
stay silent against a negative fixture". This script serialises three telemetry
traces into ``tests/fixtures/`` as JSON; ``tests/test_b3_recorded_fixtures.py``
loads them back into :class:`~neuropaca.sensing.snapshot.MetricSnapshot` objects
and replays them one at a time through a real
:class:`~neuropaca.diagnosis.correlator.SignalCorrelator`.

Everything here is a fixed table — no RNG — so the files and every value the
replay harness asserts against are byte-for-byte reproducible.

Traces (all snapshots ``POLL_SECONDS`` apart from ``_BASE``):

- ``trace_highload.json`` — 6 ``system`` snapshots, the first 5 at
  ``cpu_percent > 90``; 2 ``filesystem`` snapshots interleaved in the load
  window naming the files being written. Replay fires ``HIGH_LOAD`` exactly
  once, with those files as the signal's related nodes.
- ``trace_idle.json`` — 6 ``system`` snapshots, the first 5 at
  ``cpu_percent < 5``, then back to active. Replay fires ``IDLE`` exactly once.
- ``trace_noise.json`` — a fluctuating ``system`` trace that crosses both
  thresholds but never sustains either for the 5-sample run. Replay fires
  nothing.

    python -m tests.fixtures.generate_b3_traces        # writes the three files
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent

# At the default 60 s ``system`` poll this gives HighLoadPattern's
# ``ceil(300 / poll) = 5``-sample run and IdlePattern's
# ``ceil(idle_threshold_seconds=300 / poll) = 5``.
POLL_SECONDS = 60
_BASE = datetime(2026, 1, 1, tzinfo=UTC)

TRACE_HIGHLOAD = FIXTURE_DIR / "trace_highload.json"
TRACE_IDLE = FIXTURE_DIR / "trace_idle.json"
TRACE_NOISE = FIXTURE_DIR / "trace_noise.json"

# The files "being written" during the high-load episode. The replay harness
# asserts the HIGH_LOAD signal's ``related_node_ids`` are exactly
# ``{f"file:{p}" for p in HIGHLOAD_FILES}``.
HIGHLOAD_FILES: tuple[str, str, str] = (
    "/home/u/proj/train.py",
    "/home/u/proj/data_loader.py",
    "/home/u/proj/model.py",
)


def _snapshot(collector: str, seconds: int, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "collector_name": collector,
        "timestamp": (_BASE + timedelta(seconds=seconds)).isoformat(),
        "data": data,
        "anomaly_score": 0.0,
    }


def _system(seconds: int, cpu_percent: float) -> dict[str, Any]:
    return _snapshot("system", seconds, {"cpu_percent": cpu_percent})


def _filesystem(seconds: int, paths: list[str]) -> dict[str, Any]:
    return _snapshot(
        "filesystem",
        seconds,
        {"changed_paths": list(paths), "change_count": len(paths)},
    )


def _trace(name: str, description: str, snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "poll_seconds": POLL_SECONDS,
        "snapshots": snapshots,
    }


def build_highload_trace() -> dict[str, Any]:
    """6 ``system`` polls (1-5 > 90 %) with 2 ``filesystem`` polls interleaved."""
    train, loader, model = HIGHLOAD_FILES
    snapshots = [
        _system(0, 96.4),
        _system(60, 93.8),
        _filesystem(90, [train, loader, train]),
        _system(120, 98.1),
        _system(180, 91.5),
        _filesystem(210, [train, model]),
        _system(240, 97.7),  # 5th sustained system reading > 90 -> HIGH_LOAD fires
        _system(300, 28.3),  # load ends -> pattern re-arms, no second signal
    ]
    return _trace(
        "highload",
        "CPU pinned > 90% for 5 polls while train.py / data_loader.py / model.py "
        "are written; HIGH_LOAD fires once, related nodes are those files.",
        snapshots,
    )


def build_idle_trace() -> dict[str, Any]:
    """6 ``system`` polls, the first 5 < 5 %, then back to active."""
    snapshots = [
        _system(0, 2.4),
        _system(60, 3.9),
        _system(120, 0.8),
        _system(180, 4.1),
        _system(240, 1.5),  # 5th sustained system reading < 5 -> IDLE fires
        _system(300, 21.7),  # active again -> pattern re-arms
    ]
    return _trace(
        "idle",
        "CPU < 5% for 5 polls, then back to active; IDLE fires exactly once.",
        snapshots,
    )


def build_noise_trace() -> dict[str, Any]:
    """Alternates across both thresholds; neither predicate ever holds for the
    5-sample run, so nothing fires."""
    cpus = [95.2, 18.6, 92.4, 4.1, 96.8, 33.0, 91.1, 2.7]
    snapshots = [_system(i * POLL_SECONDS, cpu) for i, cpu in enumerate(cpus)]
    return _trace(
        "noise",
        "Fluctuating CPU that crosses both thresholds but never sustains either "
        "for 5 consecutive polls; no pattern fires.",
        snapshots,
    )


_BUILDERS: tuple[tuple[Path, Callable[[], dict[str, Any]]], ...] = (
    (TRACE_HIGHLOAD, build_highload_trace),
    (TRACE_IDLE, build_idle_trace),
    (TRACE_NOISE, build_noise_trace),
)


def write_all() -> list[Path]:
    """(Re)write all three trace files. Returns the paths written."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for path, builder in _BUILDERS:
        path.write_text(json.dumps(builder(), indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    for path in write_all():
        payload = json.loads(path.read_text(encoding="utf-8"))
        print(f"wrote {path} — {len(payload['snapshots'])} snapshots")


if __name__ == "__main__":
    main()
