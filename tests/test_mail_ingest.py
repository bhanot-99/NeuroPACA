# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for MailIngest spool tailing, episode conversion, and forget scrubbing (S1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory, graph_schema_version
from neuropaca.sensing.mail_ingest import MailIngest


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(wall=datetime(2026, 9, 12, 12, 0, tzinfo=UTC))


async def _setup(
    tmp_path: Path, clock: FakeClock
) -> tuple[MailIngest, GraphMemory, EpisodeStore, EventBus]:
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        inference_backend="fake",
        mail_enabled=True,
        mail_spool_dir=str(spool_dir),
        mail_resolved_after_days=21,
        mail_user_address="me@example.com",
    )
    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()

    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    ingest = MailIngest(bus, cfg, gm, store, clock=clock)
    await ingest.initialize()
    return ingest, gm, store, bus


async def test_schema_version_is_at_least_v9() -> None:
    assert graph_schema_version() >= 9


async def test_mail_ingest_spool_conversion_and_watermark(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    ingest, gm, store, bus = await _setup(tmp_path, fake_clock)
    try:
        spool_file = ingest.spool_dir / "messages.jsonl"

        row1 = {
            "message_id": "<m1@corp.com>",
            "in_reply_to": None,
            "references": [],
            "sender": "Maya Angelou <maya@corp.com>",
            "sender_address": "maya@corp.com",
            "to": ["Me <me@example.com>"],
            "date": "2026-09-10T10:00:00+00:00",
            "direction": "inbound",
            "subject": "Review draft",
        }
        row2 = {
            "message_id": "<m2@corp.com>",
            "in_reply_to": "<m1@corp.com>",
            "references": ["<m1@corp.com>"],
            "sender": "Me <me@example.com>",
            "sender_address": "me@example.com",
            "to": ["Maya Angelou <maya@corp.com>"],
            "date": "2026-09-10T11:00:00+00:00",
            "direction": "outbound",
            "subject": "Re: Review draft",
        }

        spool_file.write_text(json.dumps(row1) + "\n" + json.dumps(row2) + "\n", encoding="utf-8")

        # Ingest rows
        processed = await ingest.ingest_spool()
        assert processed == 2
        await store.flush()

        # Verify ledger rows in EpisodeStore
        thread_entity = "thread:m1@corp.com"
        person_entity = "person:maya-corp-com"

        thread_episodes = await store.for_entity(thread_entity)
        kinds = [e.kind for e in thread_episodes]
        assert EpisodeKind.MESSAGE_RECEIVED in kinds
        assert EpisodeKind.MESSAGE_SENT in kinds
        assert EpisodeKind.THREAD_STATE_FACT in kinds

        # Verify facts: state should be awaiting_them since row2 was sent by user
        open_facts = await store.at(fake_clock.now())
        thread_facts = [
            f
            for f in open_facts
            if f.subject == thread_entity and f.kind == EpisodeKind.THREAD_STATE_FACT
        ]
        assert len(thread_facts) == 1
        assert thread_facts[0].object == "awaiting_them"

        # Verify GraphMemory nodes and edges
        assert gm.has_node(thread_entity)
        assert gm.has_node(person_entity)
        node_thread = gm.get_node(thread_entity)
        assert node_thread is not None
        assert node_thread.node_type == NodeType.THREAD

        node_person = gm.get_node(person_entity)
        assert node_person is not None
        assert node_person.node_type == NodeType.PERSON

        # Second ingest should see nothing new (watermark at EOF)
        processed_second = await ingest.ingest_spool()
        assert processed_second == 0

        # Append row 3 (received from Maya)
        row3 = {
            "message_id": "<m3@corp.com>",
            "in_reply_to": "<m2@corp.com>",
            "references": ["<m1@corp.com>", "<m2@corp.com>"],
            "sender": "Maya Angelou <maya@corp.com>",
            "sender_address": "maya@corp.com",
            "to": ["Me <me@example.com>"],
            "date": "2026-09-11T09:00:00+00:00",
            "direction": "inbound",
            "subject": "Re: Review draft",
        }
        curr_text = spool_file.read_text(encoding="utf-8")
        spool_file.write_text(curr_text + json.dumps(row3) + "\n", encoding="utf-8")

        processed_third = await ingest.ingest_spool()
        assert processed_third == 1
        await store.flush()

        # Now state should be flipped to awaiting_you
        open_facts = await store.at(fake_clock.now())
        thread_facts = [
            f
            for f in open_facts
            if f.subject == thread_entity and f.kind == EpisodeKind.THREAD_STATE_FACT
        ]
        assert len(thread_facts) == 1
        assert thread_facts[0].object == "awaiting_you"
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()


async def test_mail_resolved_timeout(tmp_path: Path, fake_clock: FakeClock) -> None:
    ingest, _gm, store, bus = await _setup(tmp_path, fake_clock)
    try:
        spool_file = ingest.spool_dir / "messages.jsonl"

        sent_date = "2026-08-01T10:00:00+00:00"
        row = {
            "message_id": "<dead@corp.com>",
            "in_reply_to": None,
            "references": [],
            "sender": "Me <me@example.com>",
            "sender_address": "me@example.com",
            "to": ["Nobody <nobody@corp.com>"],
            "date": sent_date,
            "direction": "outbound",
            "subject": "Died long ago",
        }
        spool_file.write_text(json.dumps(row) + "\n", encoding="utf-8")
        await ingest.ingest_spool()
        await store.flush()

        # Since fake_clock is 2026-09-12 (> 21 days past 2026-08-01), the thread must be resolved
        open_facts = await store.at(fake_clock.now())
        facts = [
            f
            for f in open_facts
            if f.subject == "thread:dead@corp.com" and f.kind == EpisodeKind.THREAD_STATE_FACT
        ]
        assert len(facts) == 1
        assert facts[0].object == "resolved"
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()


async def test_mail_forget_scrub(tmp_path: Path, fake_clock: FakeClock) -> None:
    ingest, gm, store, bus = await _setup(tmp_path, fake_clock)
    try:
        spool_file = ingest.spool_dir / "messages.jsonl"

        row_maya = {
            "message_id": "<m1@corp.com>",
            "sender": "Maya Angelou <maya@corp.com>",
            "sender_address": "maya@corp.com",
            "to": ["Me <me@example.com>"],
            "date": "2026-09-10T10:00:00+00:00",
            "direction": "inbound",
        }
        row_arjun = {
            "message_id": "<a1@corp.com>",
            "sender": "Arjun Sharma <arjun@corp.com>",
            "sender_address": "arjun@corp.com",
            "to": ["Me <me@example.com>"],
            "date": "2026-09-10T10:30:00+00:00",
            "direction": "inbound",
        }
        spool_file.write_text(
            json.dumps(row_maya) + "\n" + json.dumps(row_arjun) + "\n", encoding="utf-8"
        )
        await ingest.ingest_spool()
        await store.flush()

        assert gm.has_node("person:maya-corp-com")
        assert gm.has_node("person:arjun-corp-com")

        # Forget Maya
        scrubbed = await ingest.forget("person:maya-corp-com")
        assert scrubbed >= 1
        await store.flush()

        # 1. Spool file should no longer contain Maya
        spool_text = spool_file.read_text("utf-8")
        assert "maya@corp.com" not in spool_text
        assert "arjun@corp.com" in spool_text

        # 2. GraphMemory no longer has Maya
        assert not gm.has_node("person:maya-corp-com")
        assert gm.has_node("person:arjun-corp-com")

        # 3. EpisodeStore no longer has Maya
        maya_episodes = await store.for_entity("person:maya-corp-com")
        assert len(maya_episodes) == 0

        # 4. forgotten.json exists and contains Maya
        forgotten_path = ingest.spool_dir / "forgotten.json"
        assert forgotten_path.exists()
        forgotten_data = json.loads(forgotten_path.read_text("utf-8"))
        assert "person:maya-corp-com" in forgotten_data.get("forgotten", [])

        # 5. Appending a new message from Maya to spool is ignored on next ingest
        row_maya_new = {
            "message_id": "<m99@corp.com>",
            "sender": "Maya Angelou <maya@corp.com>",
            "sender_address": "maya@corp.com",
            "to": ["Me <me@example.com>"],
            "date": "2026-09-12T11:00:00+00:00",
            "direction": "inbound",
        }
        curr_text = spool_file.read_text(encoding="utf-8")
        spool_file.write_text(curr_text + json.dumps(row_maya_new) + "\n", encoding="utf-8")

        ingested = await ingest.ingest_spool()
        assert ingested == 0
        assert not gm.has_node("person:maya-corp-com")
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()


async def test_mail_resolved_timeout_without_new_spool_activity(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """Issue 2 regression: threads must resolve on poll tick even when spool is completely idle."""
    ingest, _gm, store, bus = await _setup(tmp_path, fake_clock)
    try:
        spool_file = ingest.spool_dir / "messages.jsonl"
        t0 = fake_clock.now()

        # Ingest one message to open an awaiting_you fact
        row = {
            "message_id": "<idle@corp.com>",
            "in_reply_to": None,
            "references": [],
            "sender": "Colleague <colleague@corp.com>",
            "sender_address": "colleague@corp.com",
            "to": ["Me <me@example.com>"],
            "date": t0.isoformat(),
            "direction": "inbound",
            "subject": "Need your response",
        }
        spool_file.write_text(json.dumps(row) + "\n", encoding="utf-8")
        processed = await ingest.ingest_spool()
        assert processed == 1
        await store.flush()

        # Initially awaiting_you
        open_facts = await store.at(t0)
        facts = [
            f
            for f in open_facts
            if f.subject == "thread:idle@corp.com" and f.kind == EpisodeKind.THREAD_STATE_FACT
        ]
        assert len(facts) == 1
        assert facts[0].object == "awaiting_you"

        # Advance fake_clock past mail_resolved_after_days (21 days) with NO further spool activity
        await fake_clock.advance(22 * 86400)
        now_future = fake_clock.now()

        # Run poll_tick (which checks resolution regardless of whether new spool records arrived)
        processed_idle = await ingest.poll_tick()
        assert processed_idle == 0  # No new spool activity!

        # Fact must now be flipped to resolved
        open_facts_future = await store.at(now_future)
        facts_future = [
            f
            for f in open_facts_future
            if f.subject == "thread:idle@corp.com" and f.kind == EpisodeKind.THREAD_STATE_FACT
        ]
        assert len(facts_future) == 1
        assert facts_future[0].object == "resolved"
        assert facts_future[0].attrs.get("resolved_reason") == "timeout"
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()


async def test_subject_fallback_threading_survives_restart(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """Issue 3 regression: subject-fallback thread mapping survives daemon restart."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        inference_backend="fake",
        mail_enabled=True,
        mail_spool_dir=str(spool_dir),
        mail_resolved_after_days=21,
        mail_user_address="me@example.com",
    )
    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    spool_file = spool_dir / "messages.jsonl"
    t0 = fake_clock.now()

    # Message 1: No Message-ID, no in_reply_to, no references
    row1 = {
        "message_id": "",
        "in_reply_to": None,
        "references": [],
        "sender": "Maya Angelou <maya@corp.com>",
        "sender_address": "maya@corp.com",
        "to": ["Me <me@example.com>"],
        "date": t0.isoformat(),
        "direction": "inbound",
        "subject": "Quarterly Budget Planning",
    }
    spool_file.write_text(json.dumps(row1) + "\n", encoding="utf-8")

    ingest1 = MailIngest(bus, cfg, gm, store, clock=fake_clock)
    await ingest1.initialize()
    await ingest1.ingest_spool()
    await store.flush()

    # Find the thread node created for row1
    thread_nodes_1 = [n for n in gm.node_ids if n.startswith("thread:")]
    assert len(thread_nodes_1) == 1
    thread_id_1 = thread_nodes_1[0]

    # Teardown ingest1 (simulating daemon shutdown)
    await ingest1.stop()

    # Advance time 1 day and append message 2: also subject-only (no Message-ID)
    await fake_clock.advance(86400)
    t1 = fake_clock.now()
    row2 = {
        "message_id": "",
        "in_reply_to": None,
        "references": [],
        "sender": "Me <me@example.com>",
        "sender_address": "me@example.com",
        "to": ["Maya Angelou <maya@corp.com>"],
        "date": t1.isoformat(),
        "direction": "outbound",
        "subject": "Re: Quarterly Budget Planning",
    }
    curr_content = spool_file.read_text(encoding="utf-8")
    spool_file.write_text(curr_content + json.dumps(row2) + "\n", encoding="utf-8")

    # Reconstruct new MailIngest instance with same spool_dir and episode store
    ingest2 = MailIngest(bus, cfg, gm, store, clock=fake_clock)
    await ingest2.initialize()
    await ingest2.ingest_spool()
    await store.flush()

    # The second message MUST resolve to the exact same thread entity, not a new one!
    thread_nodes_2 = [n for n in gm.node_ids if n.startswith("thread:")]
    assert len(thread_nodes_2) == 1, f"Expected 1 thread node but found {thread_nodes_2}"
    assert thread_nodes_2[0] == thread_id_1

    # Verify both messages are in this thread's history
    thread_episodes = await store.for_entity(thread_id_1)
    msg_episodes = [
        e
        for e in thread_episodes
        if e.kind in (EpisodeKind.MESSAGE_RECEIVED, EpisodeKind.MESSAGE_SENT)
    ]
    assert len(msg_episodes) == 2

    await ingest2.stop()
    await bus.stop()
    await store.stop()


async def test_mail_two_people_same_name_separate_nodes(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    """Issue 4: people with the same display name must never fuse into one person node."""
    ingest, gm, store, bus = await _setup(tmp_path, fake_clock)
    try:
        spool_file = ingest.spool_dir / "messages.jsonl"
        row_alex_a = {
            "message_id": "<alex1@corp-a.com>",
            "sender": "Alex <alex@corp-a.com>",
            "sender_address": "alex@corp-a.com",
            "to": ["Me <me@example.com>"],
            "date": "2026-09-10T08:00:00+00:00",
            "direction": "inbound",
            "subject": "Proposal from Corp A",
        }
        row_alex_b = {
            "message_id": "<alex2@corp-b.com>",
            "sender": "Alex <alex@corp-b.com>",
            "sender_address": "alex@corp-b.com",
            "to": ["Me <me@example.com>"],
            "date": "2026-09-10T09:00:00+00:00",
            "direction": "inbound",
            "subject": "Update from Corp B",
        }
        spool_file.write_text(
            json.dumps(row_alex_a) + "\n" + json.dumps(row_alex_b) + "\n",
            encoding="utf-8",
        )
        processed = await ingest.ingest_spool()
        assert processed == 2
        await store.flush()

        # Both must exist as distinct nodes in GraphMemory
        assert gm.has_node("person:alex-corp-a-com")
        assert gm.has_node("person:alex-corp-b-com")
        assert not gm.has_node("person:alex")

        node_a = gm.get_node("person:alex-corp-a-com")
        node_b = gm.get_node("person:alex-corp-b-com")
        assert node_a is not None and node_a.label == "Alex"
        assert node_b is not None and node_b.label == "Alex"

        # Both have distinct episodes in EpisodeStore
        episodes_a = await store.for_entity("person:alex-corp-a-com")
        episodes_b = await store.for_entity("person:alex-corp-b-com")
        assert len(episodes_a) == 1
        assert len(episodes_b) == 1

        # Forget Alex from Corp A only
        scrubbed = await ingest.forget("alex@corp-a.com")
        assert scrubbed >= 1
        await store.flush()

        # Alex Corp A is scrubbed from graph, store, and spool
        assert not gm.has_node("person:alex-corp-a-com")
        assert len(await store.for_entity("person:alex-corp-a-com")) == 0
        spool_content = spool_file.read_text("utf-8")
        assert "alex@corp-a.com" not in spool_content

        # Alex Corp B remains intact in graph, store, and spool
        assert gm.has_node("person:alex-corp-b-com")
        assert len(await store.for_entity("person:alex-corp-b-com")) == 1
        assert "alex@corp-b.com" in spool_content
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()


# gen-ref: e838de78
