"""B9 · Hardening — the exit criteria, as tests (phases.md B9, BL-1..BL-10).

Grouped by blocker so a failure names the thing it protects:

- BL-1  the systemd unit can still bind the L9 socket under ProtectSystem=strict
- BL-2  boot recovery: an unreadable graph is quarantined, not fatal
- BL-3  schema versioning is *read*, not merely written
- BL-4  the file sink logrotate rotates actually exists
- BL-7  `doctor` / `export` / `panic` — the offline verbs
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from neuropaca.core.config import Config
from neuropaca.core.errors import ConfigError, GraphMemoryError
from neuropaca.core.graph_memory import GraphMemory, graph_schema_version
from neuropaca.interface import offline
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator

_HUB_COUNT = 11  # YOU + 10 domain hubs


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        inference_backend="fake",
        graph_db_path=str(tmp_path / "data" / "graph.json"),
        action_log_path=str(tmp_path / "data" / "actions.jsonl"),
        quarantine_path=str(tmp_path / "data" / "quarantine"),
        log_file_path=str(tmp_path / "data" / "neuropaca.log"),
        graph_save_interval_seconds=3600,
    )


def _write_graph(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_config(tmp_path: Path, graph: Path | None = None) -> Path:
    cfg = tmp_path / "neuropaca.toml"
    body = 'inference_backend = "fake"\n'
    if graph is not None:
        body += f'graph_db_path = "{graph}"\n'
    cfg.write_text(body, encoding="utf-8")
    return cfg


# ---------------------------------------------------------------- BL-3 · schema


async def test_a_graph_from_a_newer_build_is_refused_not_silently_loaded(
    tmp_path: Path,
) -> None:
    """A future schema must raise, not load with unknown fields dropped."""
    path = tmp_path / "graph.json"
    _write_graph(
        path,
        json.dumps({"schema_version": graph_schema_version() + 1, "nodes": [], "edges": []}),
    )
    graph = GraphMemory.get_instance(persistence_path=str(path))
    with pytest.raises(GraphMemoryError, match="newer NeuroPACA"):
        await graph.load()


async def test_a_v1_graph_still_loads(tmp_path: Path) -> None:
    """v1 differs from v2 only by the absent `surfaced_at` — it must keep working."""
    path = tmp_path / "graph.json"
    node = {
        "id": "app:webpack",
        "node_type": "concept",
        "label": "webpack",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_accessed": "2026-01-01T00:00:00+00:00",
        "access_count": 1,
        "relevance_score": 5.0,
        "priority": 1,
    }
    _write_graph(path, json.dumps({"schema_version": 1, "nodes": [node], "edges": []}))
    graph = GraphMemory.get_instance(persistence_path=str(path))
    await graph.load()
    assert graph.node_count == 1


async def test_a_graph_with_no_schema_version_is_treated_as_v1(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    _write_graph(path, json.dumps({"nodes": [], "edges": []}))
    graph = GraphMemory.get_instance(persistence_path=str(path))
    await graph.load()  # must not raise
    assert graph.node_count == _HUB_COUNT  # empty -> hubs seeded


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version": "two", "nodes": [], "edges": []}',
        '{"schema_version": true, "nodes": [], "edges": []}',
    ],
)
async def test_a_non_integer_schema_version_is_refused(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "graph.json"
    _write_graph(path, payload)
    graph = GraphMemory.get_instance(persistence_path=str(path))
    with pytest.raises(GraphMemoryError, match="must be an integer"):
        await graph.load()


async def test_a_malformed_node_record_raises_graph_memory_error_not_key_error(
    tmp_path: Path,
) -> None:
    """The boot-recovery path catches one exception type; a truncated record must
    arrive as that type rather than a bare KeyError from inside the node loop."""
    path = tmp_path / "graph.json"
    _write_graph(path, json.dumps({"schema_version": 2, "nodes": [{"id": "x"}], "edges": []}))
    graph = GraphMemory.get_instance(persistence_path=str(path))
    with pytest.raises(GraphMemoryError, match="malformed record"):
        await graph.load()


# ------------------------------------------------------------- BL-2 · recovery


@pytest.mark.parametrize(
    "corrupt",
    ["{ this is not json", "[]", '{"schema_version": 99, "nodes": [], "edges": []}'],
)
async def test_an_unreadable_graph_is_quarantined_and_the_daemon_still_boots(
    config: Config, corrupt: str
) -> None:
    """The BL-2 criterion. Before B9 this raised, and systemd looped until
    StartLimitBurst gave up permanently."""
    graph_path = Path(config.graph_db_path)
    _write_graph(graph_path, corrupt)

    orch = NeuroPACAOrchestrator(config)
    await orch.initialize()
    try:
        assert orch.graph_memory.node_count == _HUB_COUNT  # reseeded
        quarantined = sorted(graph_path.parent.glob("graph.json.corrupt.*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text("utf-8") == corrupt  # evidence preserved
        assert not graph_path.exists()  # moved, not copied
    finally:
        await orch.stop()


async def test_a_degraded_boot_is_visible_in_health(config: Config) -> None:
    """A daemon that came up on a reseeded graph must not report itself ok."""
    _write_graph(Path(config.graph_db_path), "{ broken")
    orch = NeuroPACAOrchestrator(config)
    await orch.initialize()
    await orch.start()
    try:
        health = orch.health_check()
        assert health.ok is False
        assert any("quarantined" in note for note in health.notes)
        assert health.graph_schema_version == graph_schema_version()
    finally:
        await orch.stop()


async def test_a_healthy_graph_boots_without_quarantining_anything(config: Config) -> None:
    """The recovery path must not fire on a good file."""
    graph_path = Path(config.graph_db_path)
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    seed = GraphMemory.get_instance(persistence_path=str(graph_path))
    await seed.load()
    await seed.save()
    GraphMemory._reset_for_tests()

    orch = NeuroPACAOrchestrator(config)
    await orch.initialize()
    await orch.start()
    try:
        assert orch.health_check().ok is True
        assert not list(graph_path.parent.glob("graph.json.corrupt.*"))
    finally:
        await orch.stop()


async def test_the_reseeded_graph_is_persisted_not_just_in_memory(config: Config) -> None:
    """`_dirty` must be left True, or a second crash loses the reseed too."""
    _write_graph(Path(config.graph_db_path), "{ broken")
    orch = NeuroPACAOrchestrator(config)
    await orch.initialize()
    assert orch.graph_memory.dirty is True
    await orch.stop()  # stop() saves exactly once
    assert Path(config.graph_db_path).exists()


# ------------------------------------------------------------------ BL-4 · logs


def test_the_file_sink_logrotate_rotates_actually_exists(tmp_path: Path) -> None:
    from neuropaca.core import logging as np_logging

    target = tmp_path / "logs" / "neuropaca.log"
    np_logging.configure("INFO", file_path=str(target))
    np_logging.get_logger("b9").info("hardening")
    for handler in logging.getLogger("neuropaca").handlers:
        handler.flush()

    assert target.exists()
    assert "hardening" in target.read_text("utf-8")
    assert oct(target.stat().st_mode)[-3:] == "600"


def test_an_unopenable_log_file_does_not_stop_the_daemon(tmp_path: Path) -> None:
    """A daemon must not fail to boot over its own logging."""
    from neuropaca.core import logging as np_logging

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    np_logging.configure("INFO", file_path=str(blocker / "neuropaca.log"))
    np_logging.get_logger("b9").info("still works")  # must not raise


def test_config_rejects_an_empty_log_file_path_when_the_sink_is_on() -> None:
    with pytest.raises(ConfigError):
        Config(inference_backend="fake", log_to_file=True, log_file_path="").validate()


# --------------------------------------------------------------- BL-7 · offline


def test_doctor_runs_with_no_daemon_and_no_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The BL-7 criterion: `doctor` works when nothing is running."""
    graph = tmp_path / "data" / "graph.json"
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path, graph)))
    monkeypatch.setenv("NEUROPACA_SOCKET", str(tmp_path / "absent.sock"))

    assert offline.doctor([]) == 0
    out = capsys.readouterr().out
    assert "not running" in out
    assert "seeds 11 hubs" in out


def test_doctor_reports_a_corrupt_graph_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = tmp_path / "data" / "graph.json"
    _write_graph(graph, "{ not json")
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path, graph)))
    monkeypatch.setenv("NEUROPACA_SOCKET", str(tmp_path / "absent.sock"))

    assert offline.doctor([]) == 1
    assert "CORRUPT" in capsys.readouterr().out


def test_doctor_surfaces_a_previous_boot_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A quarantined file is the only evidence a reseed happened — doctor is how
    the user finds out, since the daemon came up looking fine."""
    graph = tmp_path / "data" / "graph.json"
    _write_graph(graph, json.dumps({"schema_version": 2, "nodes": [], "edges": []}))
    (graph.parent / "graph.json.corrupt.20260903T120000Z").write_text("{ broken")
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path, graph)))
    monkeypatch.setenv("NEUROPACA_SOCKET", str(tmp_path / "absent.sock"))

    assert offline.doctor([]) == 1
    assert "quarantined graph" in capsys.readouterr().out


def test_doctor_never_opens_the_socket_when_the_daemon_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the architectural exception itself: if `doctor` ever grew a socket
    round-trip it would stop working in the case it exists for."""
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path)))
    monkeypatch.setenv("NEUROPACA_SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.chdir(tmp_path)

    def _explode(*_a: object, **_k: object) -> None:
        raise AssertionError("doctor must not open a connection")

    monkeypatch.setattr(offline.socket.socket, "connect", _explode)
    offline.doctor([])  # must not raise


def test_export_writes_the_graph_and_warns_that_data_left_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = tmp_path / "data" / "graph.json"
    payload = {
        "schema_version": 2,
        "nodes": [{"id": "app:webpack", "label": "webpack"}],
        "edges": [],
    }
    _write_graph(graph, json.dumps(payload))
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path, graph)))

    dest = tmp_path / "out" / "mygraph.json"
    assert offline.export([str(dest)]) == 0

    written = json.loads(dest.read_text("utf-8"))
    assert written["graph"] == payload
    assert written["schema_version"] == 2
    assert "exported_at" in written
    assert oct(dest.stat().st_mode)[-3:] == "600"

    out = capsys.readouterr().out
    assert "left data/" in out
    assert "1 nodes" in out


def test_export_refuses_to_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = tmp_path / "data" / "graph.json"
    _write_graph(graph, json.dumps({"schema_version": 2, "nodes": [], "edges": []}))
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path, graph)))

    dest = tmp_path / "existing.json"
    dest.write_text("mine", encoding="utf-8")
    assert offline.export([str(dest)]) == 1
    assert dest.read_text("utf-8") == "mine"
    assert offline.export([str(dest), "--force"]) == 0


def test_panic_wipes_the_data_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The BL-7 criterion: panic leaves nothing behind."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "graph.json").write_text("{}")
    (data / "actions.jsonl").write_text("{}")
    (data / "idle_cache.db").write_text("x")
    (data / "quarantine").mkdir()
    (data / "quarantine" / "held.bak").write_text("x")

    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path, data / "graph.json")))
    monkeypatch.setenv("NEUROPACA_SOCKET", str(tmp_path / "absent.sock"))

    assert offline.panic(["--yes"]) == 0
    assert data.exists()  # the directory survives; its contents do not
    assert list(data.iterdir()) == []


def test_panic_without_the_typed_word_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "graph.json").write_text("precious")
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path, data / "graph.json")))
    monkeypatch.setenv("NEUROPACA_SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")

    assert offline.panic([]) == 1
    assert (data / "graph.json").read_text("utf-8") == "precious"


def test_panic_refuses_when_the_config_will_not_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a config there is no way to know which directory to destroy."""
    monkeypatch.setenv("NEUROPACA_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.chdir(tmp_path)
    assert offline.panic(["--yes"]) == 1


def test_the_offline_verbs_are_dispatched_before_the_socket_client() -> None:
    for verb in ("doctor", "export", "panic"):
        assert verb in offline.OFFLINE_VERBS
    assert offline.dispatch([]) is None
    assert offline.dispatch(["health"]) is None


def test_the_cli_routes_offline_verbs_without_a_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through `main()`: no daemon, no socket, still a report."""
    from neuropaca.interface import cli

    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path)))
    monkeypatch.setenv("NEUROPACA_SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.chdir(tmp_path)

    # Exit code is 0 or 1 depending on what doctor finds; the point is that it
    # ran offline instead of failing with "cannot reach the daemon".
    assert cli.main(["doctor"]) in (0, 1)


# --------------------------------------------------------- BL-1/BL-4 · packaging


def test_the_systemd_unit_grants_write_access_to_the_runtime_directory() -> None:
    """BL-1, as a regression: without `ReadWritePaths=%t` the L9 socket cannot be
    bound under `ProtectSystem=strict` and every CLI verb dies — including
    `confirm`, the only approval path for a dangerous action (D-14c)."""
    unit = Path(__file__).resolve().parents[1] / "scripts" / "systemd" / "neuropacad.service"
    text = unit.read_text("utf-8")
    assert "ReadWritePaths=%t" in text
    assert "ProtectSystem=strict" in text
    assert "ReadWritePaths=__REPO__/data" in text


def test_logrotate_targets_the_configured_log_paths() -> None:
    """BL-4, as a regression: the stanza must name files the daemon really writes."""
    rotate = Path(__file__).resolve().parents[1] / "scripts" / "logrotate" / "neuropaca"
    text = rotate.read_text("utf-8")
    defaults = Config(inference_backend="fake")
    assert Path(defaults.log_file_path).name in text
    assert Path(defaults.action_log_path).name in text
    assert "copytruncate" in text  # the daemon holds the handle open
