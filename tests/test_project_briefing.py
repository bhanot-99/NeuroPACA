# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Project briefing candidate and formatting tests (S2 · Projects).

Tests:
1. Clause composition & number-word formatting (_format_project_left_off)
2. Grounding guards & candidate generation (_project_left_off_candidates)
3. build_candidates & compose_briefing integration
4. End-to-end integration with ProjectIngest sensor
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.interface.briefing import (
    _format_project_left_off,
    _project_left_off_candidates,
    build_candidates,
    compose_briefing,
)
from neuropaca.sensing.project_ingest import ProjectIngest

_NOW = datetime(2026, 9, 13, 10, 0, tzinfo=UTC)


async def _run_git(cwd: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "user.name=Test Dev",
        "-c",
        "user.email=dev@example.com",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git command failed: {args}, stderr: {stderr.decode()}")
    return stdout.decode()


def test_format_project_left_off_all_fields() -> None:
    text = _format_project_left_off(
        name="NeuroPACA",
        branch="graph-view",
        dirty_count=2,
        last_failing_tests=["tests/interface/test_briefing.py::test_bridge_value"],
        next_note="wire the tray",
    )
    assert text == (
        "You left NeuroPACA on graph-view with two uncommitted files; "
        "the last failing test was test_bridge_value; "
        "your note says: wire the tray."
    )


def test_format_project_left_off_clean_no_tests_no_note() -> None:
    text = _format_project_left_off(
        name="NeuroPACA",
        branch="main",
        dirty_count=0,
        last_failing_tests=[],
        next_note=None,
    )
    assert text == "You left NeuroPACA on main."


def test_format_project_left_off_singular_file() -> None:
    text = _format_project_left_off(
        name="NeuroPACA",
        branch="feature",
        dirty_count=1,
        last_failing_tests=[],
        next_note=None,
    )
    assert text == "You left NeuroPACA on feature with one uncommitted file."


def test_format_project_left_off_large_file_count() -> None:
    text = _format_project_left_off(
        name="NeuroPACA",
        branch="refactor",
        dirty_count=15,
        last_failing_tests=[],
        next_note=None,
    )
    assert text == "You left NeuroPACA on refactor with 15 uncommitted files."


def test_format_project_left_off_failing_test_only() -> None:
    text = _format_project_left_off(
        name="NeuroPACA",
        branch="main",
        dirty_count=3,
        last_failing_tests=["tests/test_foo.py::test_bar"],
        next_note=None,
    )
    assert text == (
        "You left NeuroPACA on main with three uncommitted files; "
        "the last failing test was test_bar."
    )


def test_format_project_left_off_note_only() -> None:
    text = _format_project_left_off(
        name="NeuroPACA",
        branch="main",
        dirty_count=2,
        last_failing_tests=[],
        next_note="wire the tray",
    )
    assert text == (
        "You left NeuroPACA on main with two uncommitted files; your note says: wire the tray."
    )


def test_format_project_left_off_clean_with_note() -> None:
    text = _format_project_left_off(
        name="NeuroPACA",
        branch="main",
        dirty_count=0,
        last_failing_tests=[],
        next_note="prepare release v0.2",
    )
    assert text == "You left NeuroPACA on main; your note says: prepare release v0.2."


def test_format_project_left_off_multiple_failing_tests_picks_last() -> None:
    text = _format_project_left_off(
        name="NeuroPACA",
        branch="dev",
        dirty_count=1,
        last_failing_tests=[
            "tests/test_a.py::test_one",
            "tests/test_b.py::test_two",
        ],
        next_note=None,
    )
    assert text == (
        "You left NeuroPACA on dev with one uncommitted file; the last failing test was test_two."
    )


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(wall=_NOW)


async def _graph(tmp_path: Path) -> GraphMemory:
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    return gm


async def _store(tmp_path: Path) -> EpisodeStore:
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    return store


async def test_project_candidates_grounding_guard(tmp_path: Path) -> None:
    """If project node does not exist in GraphMemory, candidate is excluded."""
    gm = await _graph(tmp_path)
    store = await _store(tmp_path)
    try:
        # Record fact without adding node to GraphMemory
        store.assert_fact(
            EpisodeKind.PROJECT_STATE_FACT,
            "project:neuropaca",
            "s2-projects",
            valid_from=_NOW,
            attrs={
                "branch": "s2-projects",
                "repo_name": "NeuroPACA",
                "dirty_count": 2,
            },
        )
        await store.flush()

        candidates = await _project_left_off_candidates(gm, store, now=_NOW)
        assert len(candidates) == 0

        # Now add node to GraphMemory
        await gm.upsert_node(
            "project:neuropaca",
            NodeType.PROJECT,
            attributes={"label": "NeuroPACA", "path": "/path/to/repo"},
        )

        candidates = await _project_left_off_candidates(gm, store, now=_NOW)
        assert len(candidates) == 1
        item = candidates[0]
        assert item.anchor == "project:neuropaca"
        assert item.evidence == ("project:neuropaca",)
        assert item.text == "You left NeuroPACA on s2-projects with two uncommitted files."
    finally:
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_project_candidates_superseded_and_forgotten(tmp_path: Path) -> None:
    """Superseded facts update the candidate; forgotten projects are excluded."""
    gm = await _graph(tmp_path)
    store = await _store(tmp_path)
    try:
        await gm.upsert_node(
            "project:neuropaca",
            NodeType.PROJECT,
            attributes={"label": "NeuroPACA"},
        )
        # Assert initial fact
        store.assert_fact(
            EpisodeKind.PROJECT_STATE_FACT,
            "project:neuropaca",
            "old-branch",
            valid_from=_NOW - timedelta(hours=2),
            attrs={"branch": "old-branch", "repo_name": "NeuroPACA", "dirty_count": 0},
        )
        # Assert newer fact (closes old-branch)
        store.assert_fact(
            EpisodeKind.PROJECT_STATE_FACT,
            "project:neuropaca",
            "new-branch",
            valid_from=_NOW - timedelta(hours=1),
            attrs={"branch": "new-branch", "repo_name": "NeuroPACA", "dirty_count": 1},
        )
        await store.flush()

        candidates = await _project_left_off_candidates(gm, store, now=_NOW)
        assert len(candidates) == 1
        assert candidates[0].text == "You left NeuroPACA on new-branch with one uncommitted file."

        # Forget the project
        await store.forget("project:neuropaca")
        await store.flush()
        candidates_after_forget = await _project_left_off_candidates(gm, store, now=_NOW)
        assert len(candidates_after_forget) == 0
    finally:
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_project_candidates_in_build_candidates(tmp_path: Path) -> None:
    """build_candidates incorporates project left-off candidates."""
    gm = await _graph(tmp_path)
    store = await _store(tmp_path)
    try:
        await gm.upsert_node(
            "project:bot",
            NodeType.PROJECT,
            attributes={"label": "MyBotTrader"},
        )
        store.assert_fact(
            EpisodeKind.PROJECT_STATE_FACT,
            "project:bot",
            "master",
            valid_from=_NOW,
            attrs={
                "branch": "master",
                "repo_name": "MyBotTrader",
                "dirty_count": 0,
            },
        )
        await store.flush()

        candidates = await build_candidates(gm, store, now=_NOW, last_briefing_seq=0)
        project_items = [c for c in candidates if c.anchor == "project:bot"]
        assert len(project_items) == 1
        assert project_items[0].text == "You left MyBotTrader on master."
    finally:
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_compose_briefing_with_project_candidate(tmp_path: Path) -> None:
    """compose_briefing selects project candidate when grounded and relevant."""
    gm = await _graph(tmp_path)
    store = await _store(tmp_path)
    try:
        await gm.upsert_node(
            "project:neuropaca",
            NodeType.PROJECT,
            attributes={"label": "NeuroPACA", "relevance_score": 8.0},
        )
        await gm.upsert_node(
            "domain:engineering",
            NodeType.CONCEPT,
            attributes={"label": "Engineering"},
        )
        await gm.add_edge(
            "project:neuropaca",
            "domain:engineering",
            RelationType.PART_OF,
        )

        store.assert_fact(
            EpisodeKind.PROJECT_STATE_FACT,
            "project:neuropaca",
            "graph-view",
            valid_from=_NOW,
            attrs={
                "branch": "graph-view",
                "repo_name": "NeuroPACA",
                "dirty_count": 2,
                "last_failing_tests": ["test_bridge_value"],
                "next_note": "wire the tray",
            },
        )
        await store.flush()

        moment = await compose_briefing(
            gm,
            store,
            focus_history=[("project:neuropaca", _NOW)],
            now=_NOW,
            last_briefing_seq=0,
            config=Config(inference_backend="fake"),
        )

        assert moment is not None
        assert moment.kind == "briefing"
        assert "project:neuropaca" in moment.evidence
        assert (
            "You left NeuroPACA on graph-view with two uncommitted files; "
            "the last failing test was test_bridge_value; "
            "your note says: wire the tray."
        ) in moment.text
    finally:
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_project_briefing_end_to_end_with_sensor(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """End-to-end integration: ProjectIngest scans git repo, briefing renders it."""
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()
    await _run_git(repo_dir, "init", "-b", "main")
    # .gitignore
    (repo_dir / ".gitignore").write_text(".pytest_cache/\n.neuropaca/\n", encoding="utf-8")
    (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    await _run_git(repo_dir, "add", ".")
    await _run_git(repo_dir, "commit", "-m", "initial commit")

    # Make dirty: 2 uncommitted files
    (repo_dir / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (repo_dir / "utils.py").write_text("pass\n", encoding="utf-8")

    # Add failing test in .pytest_cache
    cache_dir = repo_dir / ".pytest_cache" / "v" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "lastfailed").write_text(
        json.dumps({"tests/test_core.py::test_integration": True}),
        encoding="utf-8",
    )

    # Add .neuropaca/next note
    np_dir = repo_dir / ".neuropaca"
    np_dir.mkdir(parents=True)
    (np_dir / "next").write_text("fix integration test assertion", encoding="utf-8")

    # Setup NeuroPACA components
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "gm.json"))
    await gm.load()
    store = EpisodeStore(tmp_path / "ep.sqlite")
    await store.start()
    bus = EventBus()
    await bus.start()

    cfg = Config(
        project_tracking_enabled=True,
        watch_paths=[str(repo_dir)],
        inference_backend="fake",
    )
    ingest = ProjectIngest(bus, cfg, gm, store, clock=fake_clock)

    try:
        await ingest.initialize()
        await ingest.poll_tick()
        await store.flush()

        candidates = await build_candidates(
            gm, store, now=fake_clock.now(), last_briefing_seq=0, config=cfg
        )
        assert len(candidates) >= 1
        proj_candidates = [c for c in candidates if c.anchor.startswith("project:")]
        assert len(proj_candidates) == 1
        item = proj_candidates[0]

        expected_text = (
            f"You left {repo_dir.name} on main with two uncommitted files; "
            "the last failing test was test_integration; "
            "your note says: fix integration test assertion."
        )
        assert item.text == expected_text

        # Compose briefing
        moment = await compose_briefing(
            gm,
            store,
            focus_history=[(item.anchor, fake_clock.now())],
            now=fake_clock.now(),
            last_briefing_seq=0,
            config=cfg,
        )
        assert moment is not None
        assert expected_text in moment.text
        assert item.anchor in moment.evidence
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()
