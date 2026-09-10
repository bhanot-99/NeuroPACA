# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B13 · resource-aware sensing + non-inert Idle/Distraction (B13_PLAN.md, D-19).

Pure/synchronous units: the patterns are driven directly with hand-built
`MetricSnapshot` windows (no bus, no clock, no graph — rules.md §8); the
`ProcessCollector` is driven with a fake `psutil.process_iter`; the schema
round-trip goes through a real `GraphMemory`. Whole-pipeline claims are in
`test_b13_pipeline.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from neuropaca.core.enums import NodeType, SignalType
from neuropaca.core.graph_memory import GraphMemory, graph_schema_version
from neuropaca.diagnosis.patterns import (
    DistractionPattern,
    FocusSessionPattern,
    HeavyAppStartedPattern,
    IdlePattern,
    MemoryPressurePattern,
    build_patterns,
)
from neuropaca.sensing.snapshot import MetricSnapshot

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


class _NoBaseline:
    def zscore(self, collector: str, metric: str, value: float) -> float:
        return 0.0


class _HotMem:
    """A baseline that reports every `mem_percent` reading at z = 3.0."""

    def zscore(self, collector: str, metric: str, value: float) -> float:
        return 3.0 if metric == "mem_percent" else 0.0


def _sys(
    *, t: float, cpu: float = 20.0, mem_pct: float = 40.0, avail_mb: float = 8000.0
) -> MetricSnapshot:
    return MetricSnapshot(
        collector_name="system",
        timestamp=_BASE + timedelta(seconds=t),
        data={"cpu_percent": cpu, "mem_percent": mem_pct, "mem_available_mb": avail_mb},
    )


def _proc(*, t: float, rows: list[dict[str, Any]]) -> MetricSnapshot:
    return MetricSnapshot(
        collector_name="process",
        timestamp=_BASE + timedelta(seconds=t),
        data={"processes": rows, "group_count": len(rows)},
    )


def _row(name: str, rss_mb: float, *, cpu: float = 0.0, procs: int = 1, running: float = 100.0):
    return {
        "name": name,
        "rss_mb": rss_mb,
        "cpu_percent": cpu,
        "proc_count": procs,
        "running_seconds": running,
    }


def _act(app_id: str, *, t: float, domain: str = "") -> MetricSnapshot:
    return MetricSnapshot(
        collector_name="activity",
        timestamp=_BASE + timedelta(seconds=t),
        data={"app_id": app_id, "previous_app_id": None, "title": "", "domain": domain},
    )


def _win(**kw: Sequence[MetricSnapshot]) -> Mapping[str, Sequence[MetricSnapshot]]:
    return dict(kw)


# ------------------------------------------------------------------- B13-A · Idle


def test_idle_attaches_the_last_active_app() -> None:
    p = IdlePattern(idle_threshold_seconds=300, poll_seconds=60.0)
    system = tuple(_sys(t=i * 60, cpu=2.0) for i in range(5))
    activity = (_act("brave-browser", t=0), _act("com.system76.CosmicTerm", t=120))
    draft = p.evaluate(_win(system=system, activity=activity), _NoBaseline())
    assert draft is not None
    assert draft.signal_type is SignalType.IDLE
    assert [s.node_id for s in draft.node_specs] == ["app:com.system76.CosmicTerm"]
    assert "last active in com.system76.CosmicTerm" in draft.reason


def test_idle_with_no_activity_data_stays_nodeless() -> None:
    p = IdlePattern(idle_threshold_seconds=300, poll_seconds=60.0)
    system = tuple(_sys(t=i * 60, cpu=1.0) for i in range(5))
    draft = p.evaluate(_win(system=system, activity=()), _NoBaseline())
    assert draft is not None
    assert draft.node_specs == ()


# ----------------------------------------------------------- B13-A · Distraction


def test_distraction_attaches_the_distinct_apps_order_stable() -> None:
    p = DistractionPattern()
    apps = ["brave", "slack", "term", "slack", "brave", "term"]
    activity = [_act(a, t=float(i * 15)) for i, a in enumerate(apps)]
    draft = p.evaluate(_win(activity=activity), _NoBaseline())
    assert draft is not None
    assert [s.node_id for s in draft.node_specs] == ["app:brave", "app:slack", "app:term"]
    assert all(s.node_type is NodeType.APP and s.edges == () for s in draft.node_specs)


def test_distraction_specs_carry_no_domain_edges() -> None:
    # the app -> domain edge is owned by the APP_SWITCH classification path;
    # re-emitting it here would reset Hebbian weight (B13-A rationale).
    p = DistractionPattern()
    activity = [_act(f"app{i}", t=float(i * 12)) for i in range(6)]
    draft = p.evaluate(_win(activity=activity), _NoBaseline())
    assert draft is not None
    assert all(s.edges == () for s in draft.node_specs)


# ---------------------------------------------------- B13-B1 · MemoryPressure


def _mem_pattern() -> MemoryPressurePattern:
    return MemoryPressurePattern(poll_seconds=60.0, sustain_seconds=180.0, floor_mb=1024.0)


def test_memory_pressure_fires_on_sustained_z_and_is_silent_on_flat() -> None:
    p = _mem_pattern()
    hot = tuple(_sys(t=i * 60, mem_pct=88.0) for i in range(4))
    draft = p.evaluate(_win(system=hot, process=()), _HotMem())
    assert draft is not None
    assert draft.signal_type is SignalType.HIGH_LOAD
    assert "mem 88% (z 3.0)" in draft.reason

    calm = MemoryPressurePattern(poll_seconds=60.0, sustain_seconds=180.0)
    flat = tuple(_sys(t=i * 60, mem_pct=40.0) for i in range(6))
    assert calm.evaluate(_win(system=flat, process=()), _NoBaseline()) is None


def test_memory_pressure_fires_on_low_available_even_without_a_baseline() -> None:
    p = _mem_pattern()
    low = tuple(_sys(t=i * 60, mem_pct=95.0, avail_mb=400.0) for i in range(4))
    draft = p.evaluate(_win(system=low, process=()), _NoBaseline())
    assert draft is not None
    assert "400 MB free" in draft.reason


def test_memory_pressure_rearms_after_clearing() -> None:
    p = _mem_pattern()
    run = [_sys(t=i * 60, mem_pct=88.0) for i in range(4)]
    assert p.evaluate(_win(system=tuple(run), process=()), _HotMem()) is not None
    # still hot -> suppressed
    run.append(_sys(t=240, mem_pct=88.0))
    assert p.evaluate(_win(system=tuple(run), process=()), _HotMem()) is None
    # cools (z back to ~0, available fine) -> re-arm
    run.append(_sys(t=300, mem_pct=40.0))
    assert p.evaluate(_win(system=tuple(run), process=()), _NoBaseline()) is None
    # a fresh sustained episode fires again
    run.extend(_sys(t=360 + i * 60, mem_pct=88.0) for i in range(4))
    assert p.evaluate(_win(system=tuple(run), process=()), _HotMem()) is not None


def test_memory_pressure_attaches_the_heavy_apps_with_resource_attrs() -> None:
    p = _mem_pattern()
    hot = tuple(_sys(t=i * 60, mem_pct=90.0) for i in range(4))
    census = (_proc(t=150, rows=[_row("brave", 2048.0, running=3600.0), _row("code", 1536.0)]),)
    draft = p.evaluate(_win(system=hot, process=census), _HotMem())
    assert draft is not None
    ids = [s.node_id for s in draft.node_specs]
    assert ids == ["app:brave", "app:code"]
    attrs = dict(draft.node_specs[0].attributes)
    assert attrs["ram_mb"] == 2048.0
    assert "first_seen_at" in attrs and "last_seen_at" in attrs


# ---------------------------------------------------- B13-B4 · HeavyAppStarted


def test_heavy_app_started_edge_triggers_on_appearance_and_rearms() -> None:
    p = HeavyAppStartedPattern()
    baseline = (_proc(t=0, rows=[_row("brave", 1200.0)]),)
    assert p.evaluate(_win(process=baseline), _NoBaseline()) is None  # first census = arm only

    appeared = (*baseline, _proc(t=60, rows=[_row("brave", 1200.0), _row("qemu", 4096.0)]))
    draft = p.evaluate(_win(process=appeared), _NoBaseline())
    assert draft is not None
    assert draft.signal_type is SignalType.WORKING_SET_CHANGE
    assert [s.node_id for s in draft.node_specs] == ["app:qemu"]

    # still present -> silent
    still = (*appeared, _proc(t=120, rows=[_row("brave", 1200.0), _row("qemu", 4096.0)]))
    assert p.evaluate(_win(process=still), _NoBaseline()) is None

    # qemu drops out, then returns -> fires again
    gone = (*still, _proc(t=180, rows=[_row("brave", 1200.0)]))
    assert p.evaluate(_win(process=gone), _NoBaseline()) is None
    back = (*gone, _proc(t=240, rows=[_row("brave", 1200.0), _row("qemu", 4096.0)]))
    assert p.evaluate(_win(process=back), _NoBaseline()) is not None


def test_heavy_app_started_silent_when_census_unchanged() -> None:
    p = HeavyAppStartedPattern()
    a = _proc(t=0, rows=[_row("brave", 1200.0), _row("code", 900.0)])
    b = _proc(t=60, rows=[_row("code", 950.0), _row("brave", 1250.0)])  # same names, new order
    assert p.evaluate(_win(process=(a,)), _NoBaseline()) is None
    assert p.evaluate(_win(process=(a, b)), _NoBaseline()) is None


# ------------------------------------------------ B13-B4 · Focus corroboration


def _focus_windows(*, span: float, cpu: float, process: Sequence[MetricSnapshot] = ()):
    activity = [_act("dev.zed.Zed", t=0.0, domain="domain:engineering")]
    system = [_sys(t=s, cpu=cpu) for s in range(0, int(span) + 1, 60)]
    return _win(activity=activity, system=system, process=process)


def test_focus_session_ram_corroboration_crosses_the_l4_gate() -> None:
    plain = FocusSessionPattern().evaluate(_focus_windows(span=1260, cpu=40.0), _NoBaseline())
    assert plain is not None and plain.confidence == pytest.approx(0.6, abs=0.05)

    census = (_proc(t=600, rows=[_row("zed", 3200.0), _row("brave", 5000.0)]),)
    corroborated = FocusSessionPattern().evaluate(
        _focus_windows(span=1260, cpu=40.0, process=census), _NoBaseline()
    )
    assert corroborated is not None
    assert corroborated.confidence >= 0.7
    assert "RAM-corroborated" in corroborated.reason


def test_focus_session_no_corroboration_when_browser_is_the_heaviest() -> None:
    # browser excluded from the "largest non-browser RSS" test -> zed still wins,
    # so this actually corroborates; flip it: editor is small, another tool wins.
    census = (_proc(t=600, rows=[_row("dockerd", 4000.0), _row("zed", 200.0)]),)
    draft = FocusSessionPattern().evaluate(
        _focus_windows(span=1260, cpu=40.0, process=census), _NoBaseline()
    )
    assert draft is not None
    assert draft.confidence == pytest.approx(0.6, abs=0.05)  # dockerd != zed -> no bump


# --------------------------------------------------------- registry / B3 invariant


def test_build_patterns_registers_the_two_new_patterns_last() -> None:
    from neuropaca.core.config import Config

    names = [type(p).__name__ for p in build_patterns(Config(inference_backend="fake"))]
    assert names[-2:] == ["MemoryPressurePattern", "HeavyAppStartedPattern"]


def test_a_synthetic_seventh_pattern_needs_no_correlator_change() -> None:
    """B3 exit invariant survives the B13 additions — a new pattern is a class
    plus a `build_patterns` line, nothing in `SignalCorrelator`."""
    import inspect

    from neuropaca.diagnosis import correlator as corr_mod

    src = inspect.getsource(corr_mod)
    # the correlator still drives patterns generically via `.collectors` /
    # `.evaluate(...)` and never names a concrete pattern class.
    for cls in ("MemoryPressurePattern", "HeavyAppStartedPattern", "IdlePattern"):
        assert cls not in src


# ----------------------------------------------------------- schema v3 round-trip


async def test_resource_attributes_survive_a_save_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    graph = GraphMemory.get_instance(persistence_path=str(path))
    await graph.load()
    first = _BASE
    await graph.upsert_node(
        "app:brave",
        NodeType.APP,
        {
            "label": "brave",
            "ram_mb": 812.0,
            "cpu_percent": 4.5,
            "first_seen_at": first.isoformat(),
            "last_seen_at": first.isoformat(),
        },
    )
    await graph.save()

    GraphMemory._reset_for_tests()
    reloaded = GraphMemory.get_instance(persistence_path=str(path))
    await reloaded.load()
    node = reloaded.get_node("app:brave")
    assert node is not None
    assert node.ram_mb == 812.0
    assert node.cpu_percent == 4.5
    assert node.first_seen_at == first
    assert node.last_seen_at == first


async def test_first_seen_at_is_write_once_but_ram_updates(tmp_path: Path) -> None:
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await graph.load()
    t0, t1 = _BASE, _BASE + timedelta(hours=2)
    await graph.upsert_node(
        "app:code",
        NodeType.APP,
        {
            "label": "code",
            "ram_mb": 500.0,
            "first_seen_at": t0.isoformat(),
            "last_seen_at": t0.isoformat(),
        },
    )
    await graph.upsert_node(
        "app:code",
        NodeType.APP,
        {
            "label": "code",
            "ram_mb": 910.0,
            "first_seen_at": t1.isoformat(),
            "last_seen_at": t1.isoformat(),
        },
    )
    node = graph.get_node("app:code")
    assert node is not None
    assert node.first_seen_at == t0  # write-once
    assert node.ram_mb == 910.0  # refreshed
    assert node.last_seen_at == t1  # refreshed


async def test_a_v2_graph_loads_under_v3(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    path.write_text(
        '{"schema_version": 2, "nodes": [{"id": "app:x", "node_type": "app", '
        '"label": "x", "created_at": "2026-01-01T00:00:00+00:00", '
        '"last_accessed": "2026-01-01T00:00:00+00:00", "access_count": 1, '
        '"relevance_score": 2.0, "priority": 0, "surfaced_at": null}], "edges": []}',
        encoding="utf-8",
    )
    graph = GraphMemory.get_instance(persistence_path=str(path))
    await graph.load()  # must not raise
    node = graph.get_node("app:x")
    assert node is not None
    assert node.ram_mb == 0.0 and node.first_seen_at is None


def test_schema_version_is_v5() -> None:
    # v3: B13 resource attrs. v4: B14 NodeType.WEBAPP. v5: B18 LabelSpec.
    assert graph_schema_version() == 5


# gen-ref: a5cd4c80
