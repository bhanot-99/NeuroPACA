# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""The soak probe (`scripts/soak_probe.py`).

The probe turns `neuropaca health` into one sample row. It parses human-facing
module detail strings, which means it can silently start returning zeros after a
cosmetic reword -- and a soak sampling zeros looks exactly like a soak of a dead
system. These tests pin the parse against the real shapes the daemon emits.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "soak_probe.py"
_spec = importlib.util.spec_from_file_location("soak_probe", _MODULE_PATH)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
sys.modules["soak_probe"] = probe
_spec.loader.exec_module(probe)


# Captured verbatim from the running daemon on the target box, 2026-09-03.
LIVE_HEALTH = {
    "ok": True,
    "uptime_seconds": 126.3,
    "rss_mb": 42.47265625,
    "graph_nodes": 59,
    "graph_edges": 43,
    "queue_depth": 0,
    "events_dropped": 0,
    "modules": [
        {"name": "sensing", "ok": True, "detail": "2/2 collectors up, buffer 2/720"},
        {
            "name": "activity",
            "ok": True,
            "detail": "idle✓ window✓ · 0 transitions · 3 switches · 1 reconnects · 0 pump-errors",
        },
        {
            "name": "diagnosis",
            "ok": True,
            "detail": "4 patterns · 44 app-rules · 0 signals · 0 errors",
        },
        {
            "name": "learning",
            "ok": True,
            "detail": "model lazy · 0 insights · 0 dropped · 0 errors",
        },
        {
            "name": "drive",
            "ok": True,
            "detail": "0 tracked · 2 contributions · 1 low · 0 high · 0 errors",
        },
        {
            "name": "action",
            "ok": True,
            "detail": "dry-run · tiers safe · 5 proposed · 0 executed · 0 errors",
        },
    ],
}


def test_counters_are_pulled_out_of_a_module_detail_string() -> None:
    parsed = probe.parse_counters("0 transitions · 3 switches")
    assert parsed == {"transitions": 0, "switches": 3}


def test_an_unrecognised_detail_string_yields_no_counters_rather_than_raising() -> None:
    """A reworded detail must cost one metric, never end a six-day soak."""
    assert probe.parse_counters("model lazy, nothing to report") == {}


def test_the_live_health_payload_maps_onto_the_sample_row() -> None:
    sample = probe.build_sample(LIVE_HEALTH, actions=7)

    assert sample["daemon_up"] is True
    assert sample["rss_mib"] == 42.5
    assert sample["uptime_seconds"] == 126.3
    assert sample["graph_nodes"] == 59
    assert sample["activity_edges"] == 0
    assert sample["app_switches"] == 3
    # B15 · Wayland sensor liveness pulled straight from the activity detail
    assert sample["window_ok"] is True
    assert sample["reconnects"] == 1
    assert sample["pump_errors"] == 0
    assert sample["pressure_events"] == 2
    assert sample["pressure_low"] == 1
    assert sample["insights"] == 0
    assert sample["proposed"] == 5
    assert sample["actions"] == 7


def test_errors_are_summed_across_every_module_not_a_hand_listed_few() -> None:
    """So a module added in a later phase is counted without anyone remembering
    to come back and edit the probe."""
    health = {
        "modules": [
            {"name": "a", "ok": True, "detail": "1 errors"},
            {"name": "b", "ok": True, "detail": "2 errors"},
            {"name": "c", "ok": True, "detail": "no counters here"},
        ]
    }
    assert probe.build_sample(health)["errors"] == 3


def test_a_degraded_module_is_named_in_the_sample() -> None:
    health = {
        "modules": [
            {"name": "activity", "ok": False, "detail": "no $WAYLAND_DISPLAY"},
            {"name": "sensing", "ok": True, "detail": "2/2 collectors up"},
        ]
    }
    assert probe.build_sample(health)["degraded"] == ["activity"]


def test_a_deaf_sensor_reads_window_not_ok_even_though_the_module_tolerates_it() -> None:
    """B15 §2a: a headless / never-started Wayland source keeps the module `ok`
    (tolerated), but the sample must still record `window_ok=false` so a week of
    it is visible in the summary."""
    health = {
        "modules": [
            {
                "name": "activity",
                "ok": True,
                "detail": "idle✗ window✗ · 0 transitions · 0 switches",
            },
        ]
    }
    sample = probe.build_sample(health)
    assert sample["window_ok"] is False
    assert sample["reconnects"] == 0


def test_an_unreachable_daemon_produces_a_row_saying_so_not_an_exception() -> None:
    """A daemon that died at 03:00 is the single most important thing a week-long
    soak can record. Aborting the sampler would lose it."""
    sample = probe.build_sample(None)

    assert sample["daemon_up"] is False
    assert "ts" in sample


def test_fetch_health_returns_none_when_there_is_no_dump_file(tmp_path: Path) -> None:
    assert probe.fetch_health(str(tmp_path / "absent.json")) is None


def test_fetch_health_returns_none_on_malformed_json(tmp_path: Path) -> None:
    bad = tmp_path / "health.json"
    bad.write_text("not json", encoding="utf-8")
    assert probe.fetch_health(str(bad)) is None


def test_fetch_health_returns_none_when_ok_is_false(tmp_path: Path) -> None:
    dump = tmp_path / "health.json"
    dump.write_text('{"ok": false}', encoding="utf-8")
    assert probe.fetch_health(str(dump)) is None


def test_fetch_health_reads_a_real_dump(tmp_path: Path) -> None:
    dump = tmp_path / "health.json"
    dump.write_text('{"ok": true, "uptime_seconds": 12.0}', encoding="utf-8")
    health = probe.fetch_health(str(dump))
    assert health is not None
    assert health["uptime_seconds"] == 12.0


def test_hebbian_weight_stats_reads_the_co_occurrence_distribution(tmp_path: Path) -> None:
    """T7: the soak needs `weight_nonzero_fraction` climbing off zero and a
    `max_cooccurrence_weight` above one step (`hebbian_delta`) as proof the wiring fires."""

    def edge(source: str, target: str, relation: str, weight: float) -> dict[str, object]:
        return {"source": source, "target": target, "relation": relation, "weight": weight}

    graph = tmp_path / "graph.json"
    graph.write_text(
        json.dumps(
            {
                "edges": [
                    edge("app:a", "app:b", "related_to", 0.18),
                    edge("app:a", "app:c", "related_to", 0.0),
                    edge("app:a", "domain:x", "part_of", 0.0),
                    edge("app:b", "app:c", "related_to", 0.06),
                ]
            }
        )
    )
    stats = probe.hebbian_weight_stats(str(graph))
    assert stats["graph_edges_total"] == 4
    assert stats["weight_nonzero_fraction"] == pytest.approx(0.5)
    assert stats["max_cooccurrence_weight"] == pytest.approx(0.18)  # related_to only
    assert stats["mean_nonzero_weight"] == pytest.approx(0.12)


def test_hebbian_weight_stats_is_best_effort(tmp_path: Path) -> None:
    assert probe.hebbian_weight_stats(None) == {}
    assert probe.hebbian_weight_stats(str(tmp_path / "gone.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert probe.hebbian_weight_stats(str(bad)) == {}


# gen-ref: f9a9663e
