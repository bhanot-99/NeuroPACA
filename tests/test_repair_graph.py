# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · `neuropaca repair-graph` — the offline full-rebuild verb
(VISION_PHASES.md, "graph-store consistency"; `interface/offline.py`)."""

from __future__ import annotations

import json
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


# gen-ref: b7d3e0a4
