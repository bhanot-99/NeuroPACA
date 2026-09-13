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
        mail_min_interactions=1,  # existing tests verify mechanism, not threshold
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
            "sender": "Me <me@example.com>",
            "sender_address": "me@example.com",
            "to": ["Maya Angelou <maya@corp.com>"],
            "date": "2026-09-10T10:00:00+00:00",
            "direction": "outbound",
            "subject": "Review draft",
        }
        row2 = {
            "message_id": "<m2@corp.com>",
            "in_reply_to": "<m1@corp.com>",
            "references": ["<m1@corp.com>"],
            "sender": "Maya Angelou <maya@corp.com>",
            "sender_address": "maya@corp.com",
            "to": ["Me <me@example.com>"],
            "date": "2026-09-10T11:00:00+00:00",
            "direction": "inbound",
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
        assert EpisodeKind.MESSAGE_SENT in kinds  # row1 I sent
        assert EpisodeKind.MESSAGE_RECEIVED in kinds  # row2 Maya replied
        assert EpisodeKind.THREAD_STATE_FACT in kinds

        # Verify facts: state should be awaiting_you since row2 was received
        open_facts = await store.at(fake_clock.now())
        thread_facts = [
            f
            for f in open_facts
            if f.subject == thread_entity and f.kind == EpisodeKind.THREAD_STATE_FACT
        ]
        assert len(thread_facts) == 1
        assert thread_facts[0].object == "awaiting_you"

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

        # Append row 3 (sent by me as follow-up)
        row3 = {
            "message_id": "<m3@corp.com>",
            "in_reply_to": "<m2@corp.com>",
            "references": ["<m1@corp.com>", "<m2@corp.com>"],
            "sender": "Me <me@example.com>",
            "sender_address": "me@example.com",
            "to": ["Maya Angelou <maya@corp.com>"],
            "date": "2026-09-11T09:00:00+00:00",
            "direction": "outbound",
            "subject": "Re: Review draft",
        }
        curr_text = spool_file.read_text(encoding="utf-8")
        spool_file.write_text(curr_text + json.dumps(row3) + "\n", encoding="utf-8")

        processed_third = await ingest.ingest_spool()
        assert processed_third == 1
        await store.flush()

        # Now state should be flipped to awaiting_them
        open_facts = await store.at(fake_clock.now())
        thread_facts = [
            f
            for f in open_facts
            if f.subject == thread_entity and f.kind == EpisodeKind.THREAD_STATE_FACT
        ]
        assert len(thread_facts) == 1
        assert thread_facts[0].object == "awaiting_them"
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
            "sender": "Me <me@example.com>",
            "sender_address": "me@example.com",
            "to": ["Maya Angelou <maya@corp.com>"],
            "date": "2026-09-10T10:00:00+00:00",
            "direction": "outbound",
        }
        row_arjun = {
            "message_id": "<a1@corp.com>",
            "sender": "Me <me@example.com>",
            "sender_address": "me@example.com",
            "to": ["Arjun Sharma <arjun@corp.com>"],
            "date": "2026-09-10T10:30:00+00:00",
            "direction": "outbound",
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

        # Ingest one outbound message to open an awaiting_them fact
        row = {
            "message_id": "<idle@corp.com>",
            "in_reply_to": None,
            "references": [],
            "sender": "Me <me@example.com>",
            "sender_address": "me@example.com",
            "to": ["Colleague <colleague@corp.com>"],
            "date": t0.isoformat(),
            "direction": "outbound",
            "subject": "Need your response",
        }
        spool_file.write_text(json.dumps(row) + "\n", encoding="utf-8")
        processed = await ingest.ingest_spool()
        assert processed == 1
        await store.flush()

        # Initially awaiting_them (I sent, waiting for reply)
        open_facts = await store.at(t0)
        facts = [
            f
            for f in open_facts
            if f.subject == "thread:idle@corp.com" and f.kind == EpisodeKind.THREAD_STATE_FACT
        ]
        assert len(facts) == 1
        assert facts[0].object == "awaiting_them"

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
        mail_min_interactions=1,  # test verifies threading, not threshold
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

    # Message 1: No Message-ID — I initiate (outbound), subject-fallback threading
    row1 = {
        "message_id": "",
        "in_reply_to": None,
        "references": [],
        "sender": "Me <me@example.com>",
        "sender_address": "me@example.com",
        "to": ["Maya Angelou <maya@corp.com>"],
        "date": t0.isoformat(),
        "direction": "outbound",
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
            "sender": "Me <me@example.com>",
            "sender_address": "me@example.com",
            "to": ["Alex <alex@corp-a.com>"],
            "date": "2026-09-10T08:00:00+00:00",
            "direction": "outbound",
            "subject": "Proposal for Corp A",
        }
        row_alex_b = {
            "message_id": "<alex2@corp-b.com>",
            "sender": "Me <me@example.com>",
            "sender_address": "me@example.com",
            "to": ["Alex <alex@corp-b.com>"],
            "date": "2026-09-10T09:00:00+00:00",
            "direction": "outbound",
            "subject": "Update for Corp B",
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


# ── Two-way filter tests (s1-mail-two-way-filter) ──────────────────────────


async def _setup_filtered(
    tmp_path: Path,
    clock: FakeClock,
    *,
    min_interactions: int = 3,
    max_threads_per_person: int = 5,
) -> tuple[MailIngest, GraphMemory, EpisodeStore, EventBus]:
    """Like _setup but with configurable two-way-filter knobs."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        inference_backend="fake",
        mail_enabled=True,
        mail_spool_dir=str(spool_dir),
        mail_resolved_after_days=21,
        mail_user_address="me@example.com",
        mail_min_interactions=min_interactions,
        mail_max_threads_per_person=max_threads_per_person,
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


def _inbound(msg_id: str, from_addr: str = "spam@example.com") -> dict:
    return {
        "message_id": f"<{msg_id}>",
        "in_reply_to": None,
        "references": [],
        "sender": f"Sender <{from_addr}>",
        "sender_address": from_addr,
        "to": ["Me <me@example.com>"],
        "date": "2026-09-01T10:00:00+00:00",
        "direction": "inbound",
    }


def _outbound(msg_id: str, to_addr: str = "friend@example.com") -> dict:
    return {
        "message_id": f"<{msg_id}>",
        "in_reply_to": None,
        "references": [],
        "sender": "Me <me@example.com>",
        "sender_address": "me@example.com",
        "to": [f"Friend <{to_addr}>"],
        "date": "2026-09-01T11:00:00+00:00",
        "direction": "outbound",
    }


def _reply_to(msg_id: str, parent_id: str, from_addr: str = "friend@example.com") -> dict:
    return {
        "message_id": f"<{msg_id}>",
        "in_reply_to": f"<{parent_id}>",
        "references": [f"<{parent_id}>"],
        "sender": f"Friend <{from_addr}>",
        "sender_address": from_addr,
        "to": ["Me <me@example.com>"],
        "date": "2026-09-01T12:00:00+00:00",
        "direction": "inbound",
    }


async def test_pure_inbound_not_promoted_to_graph(tmp_path: Path, fake_clock: FakeClock) -> None:
    """Rule 1+2: a pure inbound email that is NOT a reply to any sent message
    must not create any graph nodes or episodes."""
    ingest, gm, store, bus = await _setup_filtered(tmp_path, fake_clock)
    try:
        spool_file = ingest.spool_dir / "messages.jsonl"
        spool_file.write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    _inbound("noise-1@newsletter.com", "noreply@newsletter.com"),
                    _inbound("noise-2@newsletter.com", "alerts@bank.com"),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        processed = await ingest.ingest_spool()
        assert processed == 2

        # No thread or person nodes in graph
        all_ids = list(gm.node_ids)
        thread_nodes = [n for n in all_ids if n.startswith("thread:")]
        person_nodes = [n for n in all_ids if n.startswith("person:")]
        assert thread_nodes == [], f"Expected no thread nodes, got {thread_nodes}"
        assert person_nodes == [], f"Expected no person nodes, got {person_nodes}"
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()


async def test_outbound_always_qualifies(tmp_path: Path, fake_clock: FakeClock) -> None:
    """Rule 1: an email I send always counts as a qualifying interaction."""
    ingest, gm, store, bus = await _setup_filtered(tmp_path, fake_clock, min_interactions=1)
    try:
        spool_file = ingest.spool_dir / "messages.jsonl"
        spool_file.write_text(
            json.dumps(_outbound("sent-1@me.com", "colleague@corp.com")) + "\n",
            encoding="utf-8",
        )
        await ingest.ingest_spool()
        await store.flush()

        # The sent message-ID should be tracked
        assert "sent-1@me.com" in ingest._plugin._sent_message_ids
        # With min_interactions=1, should promote to graph immediately
        person_nodes = [n for n in gm.node_ids if n.startswith("person:")]
        assert len(person_nodes) >= 1
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()


async def test_reply_to_sent_qualifies(tmp_path: Path, fake_clock: FakeClock) -> None:
    """Rule 2: an inbound message that replies to my sent message-ID qualifies."""
    ingest, gm, store, bus = await _setup_filtered(tmp_path, fake_clock, min_interactions=1)
    try:
        spool_file = ingest.spool_dir / "messages.jsonl"
        records = [
            _outbound("my-msg-001", "alice@work.com"),  # I send → tracked
            _reply_to("reply-001", "my-msg-001", "alice@work.com"),  # reply → qualifies
        ]
        spool_file.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n",
            encoding="utf-8",
        )
        await ingest.ingest_spool()
        await store.flush()

        # Both messages produced graph nodes (min_interactions=1)
        assert any(n.startswith("person:") for n in gm.node_ids)
        assert any(n.startswith("thread:") for n in gm.node_ids)
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()


async def test_below_threshold_no_graph_node(tmp_path: Path, fake_clock: FakeClock) -> None:
    """Rule 3a: below mail_min_interactions (=3), qualifying messages reach the
    episode store but produce no graph nodes."""
    ingest, gm, store, bus = await _setup_filtered(tmp_path, fake_clock, min_interactions=3)
    try:
        spool_file = ingest.spool_dir / "messages.jsonl"
        records = [
            _outbound("send-001", "bob@work.com"),  # 1 interaction
            _reply_to("reply-001", "send-001", "bob@work.com"),  # 2 interactions
        ]
        spool_file.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n",
            encoding="utf-8",
        )
        await ingest.ingest_spool()
        await store.flush()

        # Below threshold: no graph nodes
        person_nodes = [n for n in gm.node_ids if n.startswith("person:")]
        thread_nodes = [n for n in gm.node_ids if n.startswith("thread:")]
        assert person_nodes == [], f"Expected no person nodes below threshold, got {person_nodes}"
        assert thread_nodes == [], f"Expected no thread nodes below threshold, got {thread_nodes}"

        # But ledger is tracking the interactions
        from neuropaca.sensing.mail_threading import normalize_email_address, person_slug

        _, addr = normalize_email_address("bob@work.com")
        slug = person_slug("bob@work.com", addr or "bob@work.com")
        assert ingest._plugin._interaction_ledger.get(slug, 0) == 2
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()


async def test_at_threshold_emits_graph_node(tmp_path: Path, fake_clock: FakeClock) -> None:
    """Rule 3a: reaching mail_min_interactions (=3) on the 3rd tick promotes
    person and thread nodes into GraphMemory."""
    ingest, gm, store, bus = await _setup_filtered(tmp_path, fake_clock, min_interactions=3)
    try:
        spool_file = ingest.spool_dir / "messages.jsonl"
        records = [
            _outbound("send-001", "carol@work.com"),  # 1
            _reply_to("reply-001", "send-001", "carol@work.com"),  # 2
            _outbound("send-002", "carol@work.com"),  # 3 → threshold met
        ]
        spool_file.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n",
            encoding="utf-8",
        )
        await ingest.ingest_spool()
        await store.flush()

        person_nodes = [n for n in gm.node_ids if n.startswith("person:")]
        thread_nodes = [n for n in gm.node_ids if n.startswith("thread:")]
        assert len(person_nodes) >= 1, "Expected person node after threshold reached"
        assert len(thread_nodes) >= 1, "Expected thread node after threshold reached"
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()


async def test_max_threads_per_person_capped(tmp_path: Path, fake_clock: FakeClock) -> None:
    """Rule 3b: thread nodes per person are capped at mail_max_threads_per_person."""
    cap = 3
    ingest, gm, store, bus = await _setup_filtered(
        tmp_path, fake_clock, min_interactions=1, max_threads_per_person=cap
    )
    try:
        spool_file = ingest.spool_dir / "messages.jsonl"
        # Send cap+2 separate outbound messages to same recipient
        records = [_outbound(f"send-{i:03d}@me.com", "dave@work.com") for i in range(cap + 2)]
        spool_file.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n",
            encoding="utf-8",
        )
        await ingest.ingest_spool()
        await store.flush()

        thread_nodes = [n for n in gm.node_ids if n.startswith("thread:")]
        assert len(thread_nodes) <= cap, (
            f"Expected at most {cap} thread nodes, got {len(thread_nodes)}: {thread_nodes}"
        )
    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()


# gen-ref: e838de78
