# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · `neuropaca repair-graph` — the offline full-rebuild verb
(VISION_PHASES.md, "graph-store consistency"; `interface/offline.py`)."""

from __future__ import annotations

import json
import socket
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neuropaca.core.enums import EpisodeKind
from neuropaca.core.episodes import EpisodeStore
from neuropaca.interface import offline

_T0 = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _write_config(tmp_path: Path, *, episodes_enabled: bool = True) -> Path:
    cfg = tmp_path / "neuropaca.toml"
    cfg.write_text(
        'inference_backend = "fake"\n'
        f'graph_db_path = "{tmp_path / "data" / "graph.json"}"\n'
        f'quarantine_path = "{tmp_path / "data" / "quarantine"}"\n'
        f"episodes_enabled = {'true' if episodes_enabled else 'false'}\n"
        f'episodes_db_path = "{tmp_path / "data" / "episodes.sqlite"}"\n',
        encoding="utf-8",
    )
    return cfg


async def _seed_episodes(path: Path) -> None:
    store = EpisodeStore(path)
    await store.start()
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", _T0, _T0 + timedelta(minutes=5))
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "app:terminal",
        _T0 + timedelta(minutes=5),
        _T0 + timedelta(minutes=10),
    )
    await store.flush()
    await store.stop()


def test_refuses_when_episodes_are_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path, episodes_enabled=False)))
    monkeypatch.chdir(tmp_path)
    assert offline.repair_graph(["--yes"]) == 1


def test_refuses_when_no_episode_store_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path)))
    monkeypatch.chdir(tmp_path)
    assert offline.repair_graph(["--yes"]) == 1


def test_refuses_without_the_typed_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path)))
    monkeypatch.chdir(tmp_path)
    import asyncio

    asyncio.run(_seed_episodes(tmp_path / "data" / "episodes.sqlite"))
    monkeypatch.setattr("builtins.input", lambda _: "not the word")

    assert offline.repair_graph([]) == 1
    assert not (tmp_path / "data" / "graph.json").exists()


def test_rebuild_replaces_the_graph_and_quarantines_the_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path)))
    monkeypatch.chdir(tmp_path)
    import asyncio

    asyncio.run(_seed_episodes(tmp_path / "data" / "episodes.sqlite"))

    graph_path = tmp_path / "data" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text('{"schema_version": 1, "nodes": [], "edges": []}', encoding="utf-8")

    assert offline.repair_graph(["--yes"]) == 0

    rebuilt = json.loads(graph_path.read_text("utf-8"))
    node_ids = {n["id"] for n in rebuilt["nodes"]}
    assert "app:code" in node_ids and "app:terminal" in node_ids

    quarantine = tmp_path / "data" / "quarantine"
    backups = list(quarantine.glob("graph.json.pre-repair.*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text("utf-8"))["nodes"] == []  # the pre-rebuild graph


class _FakeDaemon:
    """A tiny stand-in for `InterfaceLayer`'s socket, real enough that
    `_daemon_pid`'s `SO_PEERCRED` probe and `_reload_live_daemon`'s JSONL
    round trip both work against it unmodified."""

    def __init__(self, socket_path: Path, response: dict) -> None:
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(socket_path))
        self._srv.listen(4)
        self._srv.settimeout(0.2)
        self._response = response
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(2.0)
                try:
                    data = conn.recv(65536)
                except TimeoutError:
                    data = b""
                if data:  # a real request, not just _daemon_pid's SO_PEERCRED probe
                    conn.sendall((json.dumps(self._response) + "\n").encode("utf-8"))

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._srv.close()


def test_rebuild_hot_reloads_a_running_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path)))
    monkeypatch.chdir(tmp_path)
    import asyncio

    asyncio.run(_seed_episodes(tmp_path / "data" / "episodes.sqlite"))

    sock_path = tmp_path / "daemon.sock"
    monkeypatch.setenv("NEUROPACA_SOCKET", str(sock_path))
    daemon = _FakeDaemon(sock_path, {"ok": True, "nodes": 12, "edges": 1})
    try:
        assert offline.repair_graph(["--yes"]) == 0
    finally:
        daemon.stop()

    out = capsys.readouterr().out
    assert "reloaded the rebuilt graph" in out
    assert "restart it" not in out


def test_rebuild_reports_a_failed_hot_reload_without_failing_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NEUROPACA_CONFIG", str(_write_config(tmp_path)))
    monkeypatch.chdir(tmp_path)
    import asyncio

    asyncio.run(_seed_episodes(tmp_path / "data" / "episodes.sqlite"))

    sock_path = tmp_path / "daemon.sock"
    monkeypatch.setenv("NEUROPACA_SOCKET", str(sock_path))
    daemon = _FakeDaemon(sock_path, {"ok": False, "error": "reload failed: boom"})
    try:
        assert offline.repair_graph(["--yes"]) == 0  # the rebuild itself still succeeded
    finally:
        daemon.stop()

    out = capsys.readouterr().out
    assert "could not hot-reload" in out
    assert "restart it" in out


# gen-ref: b7d3e0a4
