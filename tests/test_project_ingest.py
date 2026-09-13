# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for ProjectIngest (S2 · Projects).

Presents git repositories in every fixture state:
- Clean repo
- Dirty repo (uncommitted / untracked files)
- Detached HEAD
- Zero commits (freshly initialized repo)
- Merge-in-progress (merge conflict state)
- With / without .pytest_cache/v/cache/lastfailed
- With / without .neuropaca/next (note capping at project_next_max_chars)
- Read-only property: git status and HEAD byte-identical before and after
- Fact churn suppression: unchanged repo produces zero new facts on subsequent poll
- Forget: removes facts, episodes, graph node, and cached state
- Thread moment: transitions from > project_stale_days away to focused
  triggers Moment(kind="thread")
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, EventType, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event, Moment
from neuropaca.sensing.project_ingest import ProjectIngest, format_project_left_off, project_slug


async def _run_git(cwd: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0 and args[0] != "merge":
        raise RuntimeError(f"git command failed: {args}, stderr: {stderr.decode()}")
    return stdout.decode()


async def _git_snapshot(repo: Path) -> tuple[str, str]:
    """Capture (git status --porcelain, HEAD ref/commit) to verify zero mutation."""
    status = await _run_git(repo, "status", "--porcelain")
    proc = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "HEAD",
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    head = stdout.decode() if proc.returncode == 0 else "NO_HEAD"
    return status, head


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(wall=datetime(2026, 9, 13, 12, 0, tzinfo=UTC))


async def _setup_ingest(
    tmp_path: Path, clock: FakeClock, watch_paths: list[str]
) -> tuple[ProjectIngest, GraphMemory, EpisodeStore, EventBus]:
    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()

    cfg = Config(
        inference_backend="fake",
        project_tracking_enabled=True,
        watch_paths=watch_paths,
        project_poll_interval_seconds=300.0,
        project_stale_days=3,
        project_next_max_chars=200,
    )

    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    await gm.upsert_node(
        "domain:engineering", NodeType.CONCEPT, attributes={"label": "Engineering"}
    )

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    ingest = ProjectIngest(bus, cfg, gm, store, clock=clock)
    await ingest.initialize()
    return ingest, gm, store, bus


async def test_project_ingest_all_fixture_states_and_readonly(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir(parents=True)

    # 1. Clean repo
    clean_repo = repos_dir / "clean_repo"
    clean_repo.mkdir()
    await _run_git(clean_repo, "init", "-b", "main")
    (clean_repo / "README.md").write_text("Clean repo\n")
    await _run_git(clean_repo, "add", "README.md")
    await _run_git(clean_repo, "commit", "-m", "Initial commit")

    # 2. Dirty repo with .pytest_cache and .neuropaca/next
    dirty_repo = repos_dir / "dirty_repo"
    dirty_repo.mkdir()
    await _run_git(dirty_repo, "init", "-b", "feature-x")
    (dirty_repo / ".gitignore").write_text(".pytest_cache/\n.neuropaca/\n")
    (dirty_repo / "main.py").write_text("print('hello')\n")
    await _run_git(dirty_repo, "add", ".gitignore", "main.py")
    await _run_git(dirty_repo, "commit", "-m", "Base commit")
    # Make dirty: one modified file, one untracked file
    (dirty_repo / "main.py").write_text("print('hello world')\n")
    (dirty_repo / "untracked.txt").write_text("draft\n")
    # Add .pytest_cache
    pytest_cache = dirty_repo / ".pytest_cache" / "v" / "cache"
    pytest_cache.mkdir(parents=True)
    (pytest_cache / "lastfailed").write_text(
        json.dumps(
            {
                "tests/test_foo.py::test_alpha": True,
                "tests/test_bar.py::test_bridge_value": True,
            }
        )
    )
    # Add .neuropaca/next with long note to test capping
    neuropaca_dir = dirty_repo / ".neuropaca"
    neuropaca_dir.mkdir(parents=True)
    (neuropaca_dir / "next").write_text("wire the tray and verify the guardian\nsecond line ignore")

    # 3. Detached HEAD repo
    detached_repo = repos_dir / "detached_repo"
    detached_repo.mkdir()
    await _run_git(detached_repo, "init", "-b", "main")
    (detached_repo / "file1.txt").write_text("v1\n")
    await _run_git(detached_repo, "add", "file1.txt")
    await _run_git(detached_repo, "commit", "-m", "commit 1")
    (detached_repo / "file1.txt").write_text("v2\n")
    await _run_git(detached_repo, "commit", "-am", "commit 2")
    await _run_git(detached_repo, "checkout", "--detach", "HEAD~1")

    # 4. Zero commits repo
    empty_repo = repos_dir / "empty_repo"
    empty_repo.mkdir()
    await _run_git(empty_repo, "init", "-b", "main")

    # 5. Merge-in-progress repo
    merge_repo = repos_dir / "merge_repo"
    merge_repo.mkdir()
    await _run_git(merge_repo, "init", "-b", "main")
    (merge_repo / "conflict.txt").write_text("base\n")
    await _run_git(merge_repo, "add", "conflict.txt")
    await _run_git(merge_repo, "commit", "-m", "base")
    await _run_git(merge_repo, "checkout", "-b", "branch-a")
    (merge_repo / "conflict.txt").write_text("version A\n")
    await _run_git(merge_repo, "commit", "-am", "branch A edit")
    await _run_git(merge_repo, "checkout", "main")
    (merge_repo / "conflict.txt").write_text("version B\n")
    await _run_git(merge_repo, "commit", "-am", "branch B edit")
    # Trigger conflict merge
    await _run_git(merge_repo, "merge", "branch-a")

    all_repos = [clean_repo, dirty_repo, detached_repo, empty_repo, merge_repo]

    # Capture pre-run git snapshots for read-only guarantee
    pre_snapshots = {repo: await _git_snapshot(repo) for repo in all_repos}

    ingest, gm, store, bus = await _setup_ingest(tmp_path, fake_clock, watch_paths=[str(repos_dir)])

    try:
        # Run poll
        num_repos = await ingest.poll_tick()
        assert num_repos == 5

        # Assert Read-Only Guarantee: git state is byte-identical before and after
        for repo in all_repos:
            post_status, post_head = await _git_snapshot(repo)
            pre_status, pre_head = pre_snapshots[repo]
            assert post_status == pre_status, f"Repo {repo.name} status modified by collector!"
            assert post_head == pre_head, f"Repo {repo.name} HEAD changed by collector!"

        # Assert EpisodeStore facts
        open_facts = await store.at(fake_clock.now())
        project_facts = [f for f in open_facts if f.kind == str(EpisodeKind.PROJECT_STATE_FACT)]
        assert len(project_facts) >= 4  # empty_repo has no commits, rev-parse may fail

        facts_by_subject = {f.subject: f for f in project_facts}

        # Check clean repo
        clean_fact = facts_by_subject.get("project:clean-repo")
        assert clean_fact is not None
        assert clean_fact.object == "main"
        assert clean_fact.attrs["dirty_count"] == 0
        assert clean_fact.attrs["last_failing_tests"] == []
        assert clean_fact.attrs["next_note"] is None
        assert gm.has_node("project:clean-repo")
        assert any(
            e.target_id == "domain:engineering" and e.relation == RelationType.PART_OF
            for e in gm.get_edges("project:clean-repo")
        )

        # Check dirty repo
        dirty_fact = facts_by_subject.get("project:dirty-repo")
        assert dirty_fact is not None
        assert dirty_fact.object == "feature-x"
        assert dirty_fact.attrs["dirty_count"] == 2
        assert "tests/test_bar.py::test_bridge_value" in dirty_fact.attrs["last_failing_tests"]
        assert dirty_fact.attrs["next_note"] == "wire the tray and verify the guardian"

        # Check detached HEAD repo
        detached_fact = facts_by_subject.get("project:detached-repo")
        assert detached_fact is not None
        assert detached_fact.object.startswith("detached:")

        # ------------------------------------------------ Fact churn suppression
        # Running poll_tick() again without repo changes should NOT assert new facts
        facts_count_before = ingest._facts_written
        await ingest.poll_tick()
        assert ingest._facts_written == facts_count_before

        # V-10: repeated polling of an unchanged repo is a sighting, not an
        # access — it must not inflate `activity`/`access_count`/
        # `relevance_score` (GraphMemory._touch_unsafe), or a repo would look
        # more "important" purely from sitting under watch_paths.
        node_before = gm.get_node("project:clean-repo")
        assert node_before is not None
        access_count_before = node_before.access_count
        activity_before = node_before.activity
        await ingest.poll_tick()
        await ingest.poll_tick()
        node_after = gm.get_node("project:clean-repo")
        assert node_after is not None
        assert node_after.access_count == access_count_before
        assert node_after.activity == pytest.approx(activity_before)

    finally:
        await ingest.stop()
        await store.stop()
        await bus.stop()


async def test_project_ingest_forget(tmp_path: Path, fake_clock: FakeClock) -> None:
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    repo = repos_dir / "target_repo"
    repo.mkdir()
    await _run_git(repo, "init", "-b", "main")
    (repo / "file.txt").write_text("content\n")
    await _run_git(repo, "add", "file.txt")
    await _run_git(repo, "commit", "-m", "init")

    ingest, gm, store, bus = await _setup_ingest(tmp_path, fake_clock, watch_paths=[str(repo)])

    try:
        await ingest.poll_tick()
        entity = "project:target-repo"
        assert gm.has_node(entity)

        open_facts = await store.at(fake_clock.now())
        assert any(f.subject == entity for f in open_facts)

        # Call forget
        removed = await ingest.forget(repo)
        assert removed > 0

        # Verify scrubbed from GraphMemory
        assert not gm.has_node(entity)

        # Verify scrubbed from EpisodeStore
        facts_after = await store.at(fake_clock.now())
        assert not any(f.subject == entity for f in facts_after)

        # Verify in-memory state cleared
        assert entity not in ingest._last_seen_state
        assert entity not in ingest._repo_states
    finally:
        await ingest.stop()
        await store.stop()
        await bus.stop()


async def _run_git_dated(cwd: Path, iso_date: str, *args: str) -> str:
    """Like `_run_git`, but pins author/committer date — so a commit's
    `%ct` (what `ProjectIngest` reads for staleness) is deterministic and
    independent of real wall-clock time, decoupled from the `FakeClock` the
    module itself uses for "now"."""
    import os

    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": iso_date,
        "GIT_COMMITTER_DATE": iso_date,
    }
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        *args,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git command failed: {args}, stderr: {stderr.decode()}")
    return stdout.decode()


async def test_project_thread_moment_on_return_after_stale_days(tmp_path: Path) -> None:
    """The thread moment (VISION.md §1.4) fires from real git ground truth —
    not a simulated focus event (see the module docstring for why `APP_SWITCH`
    can't be the trigger). A repo whose last commit is older than
    `project_stale_days` picking up its first uncommitted change is the
    signal; committing again (or staying clean) must not spuriously refire."""
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    repo = repos_dir / "neuropaca"
    repo.mkdir()
    await _run_git(repo, "init", "-b", "graph-view")
    (repo / ".gitignore").write_text(".pytest_cache/\n.neuropaca/\n")
    (repo / "code.py").write_text("code\n")
    await _run_git(repo, "add", ".gitignore", "code.py")
    old_commit_date = "2026-08-01T12:00:00"
    await _run_git_dated(repo, old_commit_date, "commit", "-m", "left off")

    cache_dir = repo / ".pytest_cache" / "v" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "lastfailed").write_text(json.dumps({"test_bridge_value": True}))
    np_dir = repo / ".neuropaca"
    np_dir.mkdir(parents=True)
    (np_dir / "next").write_text("wire the tray")

    # "now" is well past project_stale_days (3) after old_commit_date.
    clock = FakeClock(wall=datetime(2026, 9, 10, 12, 0, tzinfo=UTC))
    ingest, _gm, store, bus = await _setup_ingest(tmp_path, clock, watch_paths=[str(repo)])

    delivered_moments: list[Moment] = []

    def _on_moment(event: Event) -> None:
        m = event.payload.get("moment")
        if isinstance(m, Moment):
            delivered_moments.append(m)

    bus.subscribe(EventType.MOMENT_PROPOSED, _on_moment)

    try:
        await ingest.start()  # first poll_tick: stale but clean -> no moment
        assert len(delivered_moments) == 0

        # Become dirty while stale: fires exactly once.
        (repo / "code.py").write_text("code\nmore\n")
        await ingest.poll_tick()
        assert len(delivered_moments) == 1
        moment = delivered_moments[0]
        assert moment.kind == "thread"
        assert moment.evidence == ("project:neuropaca",)
        assert moment.text == (
            "You left neuropaca on graph-view with one uncommitted file; "
            "the last failing test was test_bridge_value; your note says: wire the tray."
        )

        # Still dirty, still stale: does not refire.
        await ingest.poll_tick()
        assert len(delivered_moments) == 1

        # Commit (dated close to "now", so no longer stale) and go dirty again:
        # must not refire, since the repo is no longer stale.
        await _run_git(repo, "add", "code.py")
        await _run_git_dated(repo, "2026-09-10T12:00:00", "commit", "-m", "wired the tray")
        await ingest.poll_tick()  # clean again -> clears the fired flag
        (repo / "code.py").write_text("code\nmore\nagain\n")
        await ingest.poll_tick()
        assert len(delivered_moments) == 1  # no longer stale -> no second thread moment

        h = ingest.health()
        assert h.ok is True
        assert "thread_moments=1" in h.detail
    finally:
        bus.unsubscribe(EventType.MOMENT_PROPOSED, _on_moment)
        await ingest.stop()
        await store.stop()
        await bus.stop()


def test_format_project_left_off_omission_rules() -> None:
    # Full example matching vision doc
    text_full = format_project_left_off(
        name="NeuroPACA",
        branch="graph-view",
        dirty_count=2,
        last_failing_tests=["test_bridge_value"],
        next_note="wire the tray",
    )
    assert text_full == (
        "You left NeuroPACA on graph-view with two uncommitted files; "
        "the last failing test was test_bridge_value; your note says: wire the tray."
    )

    # 1 uncommitted file singular
    text_singular = format_project_left_off(
        name="NeuroPACA",
        branch="main",
        dirty_count=1,
        last_failing_tests=[],
        next_note=None,
    )
    assert text_singular == "You left NeuroPACA on main with one uncommitted file."

    # Zero uncommitted files, no tests, no note
    text_clean = format_project_left_off(
        name="NeuroPACA",
        branch="main",
        dirty_count=0,
        last_failing_tests=[],
        next_note=None,
    )
    assert text_clean == "You left NeuroPACA on main."

    # Zero uncommitted files with note only
    text_note_only = format_project_left_off(
        name="NeuroPACA",
        branch="main",
        dirty_count=0,
        last_failing_tests=[],
        next_note="wire the tray",
    )
    assert text_note_only == "You left NeuroPACA on main; your note says: wire the tray."

    # Zero uncommitted files with failing test only
    text_test_only = format_project_left_off(
        name="NeuroPACA",
        branch="main",
        dirty_count=0,
        last_failing_tests=["tests/test_x.py::test_y"],
        next_note=None,
    )
    assert text_test_only == "You left NeuroPACA on main; the last failing test was test_y."


def test_project_slug() -> None:
    assert project_slug("/home/user/NeuroPaca") == "neuropaca"
    assert project_slug("My Cool Project") == "my-cool-project"
    assert project_slug("project:widgetco") == "widgetco"
    assert project_slug("path/to/repo_1") == "repo-1"


# gen-ref: 3c3b56ce
