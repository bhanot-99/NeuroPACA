"""B13 · `RawMetricsRecorder` — the append-only raw-data CSV (operator request).

Passive `METRIC_COLLECTED` subscriber; driven here directly with hand-built
snapshots. No cognitive-loop coupling, so no bus round-trip is needed.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event
from neuropaca.orchestration.modules import build_modules
from neuropaca.sensing.raw_recorder import RawMetricsRecorder
from neuropaca.sensing.snapshot import MetricSnapshot

_T = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


async def _recorder(tmp_path: Path) -> tuple[RawMetricsRecorder, EventBus, Path]:
    csv_path = tmp_path / "raw_metrics.csv"
    bus = EventBus.get_instance()
    await bus.start()
    cfg = Config(inference_backend="fake", raw_metrics_csv_path=str(csv_path))
    rec = RawMetricsRecorder(bus, cfg)
    await rec.initialize()
    await rec.start()
    return rec, bus, csv_path


def _ev(collector: str, data: dict) -> Event:
    return Event(
        EventType.METRIC_COLLECTED,
        source=f"sensing.{collector}",
        payload={"snapshot": MetricSnapshot(collector, _T, data)},
    )


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


async def test_header_is_written_on_init(tmp_path: Path) -> None:
    _rec, bus, path = await _recorder(tmp_path)
    try:
        assert path.exists()
        header = path.read_text(encoding="utf-8").splitlines()[0]
        assert header.startswith("timestamp,collector,cpu_percent")
        assert header.endswith(",detail")
    finally:
        await bus.stop()


async def test_system_snapshot_becomes_one_wide_row(tmp_path: Path) -> None:
    rec, bus, path = await _recorder(tmp_path)
    try:
        await rec.on_metric(
            _ev(
                "system",
                {
                    "cpu_percent": 12.5,
                    "mem_percent": 44.0,
                    "mem_available_mb": 8000.0,
                    "disk_percent": 60.0,
                    "disk_free_gb": 120.0,
                },
            )
        )
        rows = _rows(path)
        assert len(rows) == 1
        assert rows[0]["collector"] == "system"
        assert rows[0]["cpu_percent"] == "12.5"
        assert rows[0]["mem_percent"] == "44.0"
        assert rows[0]["app_name"] == ""
    finally:
        await bus.stop()


async def test_process_snapshot_becomes_one_row_per_app(tmp_path: Path) -> None:
    rec, bus, path = await _recorder(tmp_path)
    try:
        await rec.on_metric(
            _ev(
                "process",
                {
                    "processes": [
                        {
                            "name": "brave",
                            "rss_mb": 2048.0,
                            "cpu_percent": 3.0,
                            "proc_count": 12,
                            "running_seconds": 3600.0,
                        },
                        {
                            "name": "code",
                            "rss_mb": 1536.0,
                            "cpu_percent": 1.0,
                            "proc_count": 4,
                            "running_seconds": 1800.0,
                        },
                    ],
                    "group_count": 2,
                },
            )
        )
        rows = _rows(path)
        assert [r["app_name"] for r in rows] == ["brave", "code"]
        assert rows[0]["app_rss_mb"] == "2048.0"
        assert rows[0]["cpu_percent"] == ""  # system columns blank on a process row
        assert rows[0]["app_proc_count"] == "12"
    finally:
        await bus.stop()


async def test_other_collectors_keep_their_data_in_the_detail_column(tmp_path: Path) -> None:
    rec, bus, path = await _recorder(tmp_path)
    try:
        await rec.on_metric(
            _ev("activity", {"app_id": "dev.zed.Zed", "domain": "domain:engineering"})
        )
        row = _rows(path)[0]
        assert row["collector"] == "activity"
        assert "dev.zed.Zed" in row["detail"]
        assert "domain:engineering" in row["detail"]
    finally:
        await bus.stop()


async def test_a_write_failure_is_reported_once_and_never_raises(tmp_path: Path) -> None:
    rec, bus, _path = await _recorder(tmp_path)
    errs: list[Event] = []

    async def sink(e: Event) -> None:
        errs.append(e)

    bus.subscribe(EventType.SYSTEM_ERROR, sink)
    try:
        rec._append = _boom  # type: ignore[method-assign]
        await rec.on_metric(_ev("system", {"cpu_percent": 1.0}))
        await bus.join()
        assert len(errs) == 1
        assert rec.health().ok is False
        # a second failure does not spam more errors
        await rec.on_metric(_ev("system", {"cpu_percent": 2.0}))
        await bus.join()
        assert len(errs) == 1
    finally:
        await bus.stop()


def _boom(_rows: list[list[str]]) -> None:
    raise OSError("disk full")


def test_build_modules_only_wires_the_recorder_when_a_path_is_set(tmp_path: Path) -> None:
    from neuropaca.core.bitnet_runtime import BitNetRuntime
    from neuropaca.core.graph_memory import GraphMemory

    bus = EventBus.get_instance()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    runtime = BitNetRuntime.get_instance()

    off = build_modules(Config(inference_backend="fake"), bus, graph, runtime)
    assert "raw_recorder" not in [m.name for m in off]

    on = build_modules(
        Config(inference_backend="fake", raw_metrics_csv_path=str(tmp_path / "m.csv")),
        bus,
        graph,
        runtime,
    )
    assert [m.name for m in on][:2] == ["sensing", "raw_recorder"]
