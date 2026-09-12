# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · `EpisodeStore` (VISION_PHASES.md §3.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from neuropaca.core.enums import EpisodeKind
from neuropaca.core.episodes import EpisodeStore, episodes_schema_version

_T0 = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)


async def _store(tmp_path) -> EpisodeStore:
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()
    return store


async def test_span_round_trips_and_at_finds_it(tmp_path) -> None:
    store = await _store(tmp_path)
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", _T0, _T0 + timedelta(minutes=30))
    await store.flush()

    hits = await store.at(_T0 + timedelta(minutes=10))
    assert len(hits) == 1
    assert hits[0].subject == "app:code"
    assert hits[0].kind == "focus_span"
    assert hits[0].t_start == _T0
    assert hits[0].episode_seq == 1

    assert await store.at(_T0 - timedelta(minutes=1)) == []
    await store.stop()


async def test_assert_fact_closes_the_contradicting_open_interval(tmp_path) -> None:
    store = await _store(tmp_path)
    store.assert_fact(EpisodeKind.TOPIC_FACT, "app:code", "engineering", valid_from=_T0)
    await store.flush()
    store.assert_fact(
        EpisodeKind.TOPIC_FACT, "app:code", "research", valid_from=_T0 + timedelta(days=1)
    )
    await store.flush()

    # what was true a minute after the first fact — the old one, not deleted
    before = await store.at(_T0 + timedelta(minutes=1))
    assert [r.object for r in before] == ["engineering"]

    after = await store.at(_T0 + timedelta(days=1, minutes=1))
    assert [r.object for r in after] == ["research"]

    # the old fact still exists in the log, just closed
    everything = await store.since(0)
    assert len(everything) == 2
    old = next(r for r in everything if r.object == "engineering")
    assert old.t_invalid == _T0 + timedelta(days=1)
    await store.stop()


async def test_since_watermark_replay(tmp_path) -> None:
    store = await _store(tmp_path)
    for i in range(5):
        store.record_span(
            EpisodeKind.FOCUS_SPAN,
            "app:code",
            _T0 + timedelta(minutes=i),
            _T0 + timedelta(minutes=i + 1),
        )
    await store.flush()

    all_rows = await store.since(0)
    assert [r.episode_seq for r in all_rows] == [1, 2, 3, 4, 5]

    tail = await store.since(3)
    assert [r.episode_seq for r in tail] == [4, 5]
    await store.stop()


async def test_between_overlap(tmp_path) -> None:
    store = await _store(tmp_path)
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:a", _T0, _T0 + timedelta(minutes=5))
    store.record_span(
        EpisodeKind.FOCUS_SPAN,
        "app:b",
        _T0 + timedelta(hours=1),
        _T0 + timedelta(hours=1, minutes=5),
    )
    await store.flush()

    rows = await store.between(_T0 - timedelta(minutes=1), _T0 + timedelta(minutes=1))
    assert [r.subject for r in rows] == ["app:a"]
    await store.stop()


async def test_forget_removes_subject_and_object_rows(tmp_path) -> None:
    store = await _store(tmp_path)
    store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", _T0, _T0 + timedelta(minutes=5))
    store.assert_fact(EpisodeKind.TOPIC_FACT, "app:other", "app:code", valid_from=_T0)
    await store.flush()

    removed = await store.forget("app:code")
    assert removed == 2
    assert await store.since(0) == []
    await store.stop()


async def test_20k_switch_storm_batches_without_dropping(tmp_path) -> None:
    store = await _store(tmp_path)
    n = 20_000
    for i in range(n):
        t = _T0 + timedelta(seconds=i)
        store.record_span(EpisodeKind.FOCUS_SPAN, "app:code", t, t + timedelta(seconds=1))
    ok = await store.flush()
    assert ok
    assert store.dropped_count == 0
    assert store.written_count == n
    rows = await store.since(n - 1)
    assert len(rows) == 1
    await store.stop()


def test_schema_version_is_stable() -> None:
    assert episodes_schema_version() == 1


# gen-ref: 7d1c9e44
