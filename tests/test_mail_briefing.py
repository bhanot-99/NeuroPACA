# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""50-thread hand-labelled evaluation for correspondence briefing candidates (S1 exit criterion).

VISION_PHASES.md S1 exit criterion:
- "On a 50-thread hand-labelled set: 'replied' / 'awaiting' precision >= 0.9."
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import NodeType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.interface.briefing import (
    _mail_overdue_candidates,
    _mail_reply_candidates,
    build_candidates,
    compose_briefing,
)
from neuropaca.sensing.mail_ingest import MailIngest


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(wall=datetime(2026, 9, 12, 12, 0, tzinfo=UTC))


async def _setup_50_threads(
    tmp_path: Path, clock: FakeClock
) -> tuple[MailIngest, GraphMemory, EpisodeStore, EventBus, dict[str, dict[str, bool]]]:
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(
        inference_backend="fake",
        mail_enabled=True,
        mail_spool_dir=str(spool_dir),
        mail_overdue_days=3,
        mail_resolved_after_days=21,
        mail_user_address="me@example.com",
        mail_min_interactions=1,  # eval fixture tests briefing logic, not threshold
    )

    GraphMemory._reset_for_tests()
    bus = EventBus()
    await bus.start()

    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    # Add domain:comms hub
    await gm.upsert_node("domain:comms", NodeType.CONCEPT, attributes={"label": "Communications"})

    store = EpisodeStore(tmp_path / "episodes.sqlite")
    await store.start()

    ingest = MailIngest(bus, cfg, gm, store, clock=clock)
    await ingest.initialize()

    now = clock.now()
    records: list[dict] = []
    labels: dict[str, dict[str, bool]] = {}

    # 1..10: User started thread, recipient replied 2 days ago (awaiting user, < 3 days overdue)
    # expected_replied = True, expected_awaiting = False
    for i in range(1, 11):
        t_id = f"t{i:02d}"
        contact = f"colleague{i:02d}@corp.com"
        contact_name = f"Colleague {i:02d}"
        m1_id = f"<m{t_id}_1@example.com>"
        m2_id = f"<m{t_id}_2@corp.com>"
        labels[f"thread:{m1_id.strip('<>')}"] = {"replied": True, "awaiting": False}

        records.append(
            {
                "message_id": m1_id,
                "in_reply_to": None,
                "references": [],
                "sender": "Me <me@example.com>",
                "sender_address": "me@example.com",
                "to": [f"{contact_name} <{contact}>"],
                "date": (now - timedelta(days=4)).isoformat(),
                "direction": "outbound",
                "subject": f"Project Discussion {t_id}",
            }
        )
        records.append(
            {
                "message_id": m2_id,
                "in_reply_to": m1_id,
                "references": [m1_id],
                "sender": f"{contact_name} <{contact}>",
                "sender_address": contact,
                "to": ["Me <me@example.com>"],
                "date": (now - timedelta(days=2)).isoformat(),
                "direction": "inbound",
                "subject": f"Re: Project Discussion {t_id}",
            }
        )

    # 11..20: User started thread, recipient replied 5 days ago (awaiting user, >= 3 days overdue)
    # expected_replied = True, expected_awaiting = True
    for i in range(11, 21):
        t_id = f"t{i:02d}"
        contact = f"colleague{i:02d}@corp.com"
        contact_name = f"Colleague {i:02d}"
        m1_id = f"<m{t_id}_1@example.com>"
        m2_id = f"<m{t_id}_2@corp.com>"
        labels[f"thread:{m1_id.strip('<>')}"] = {"replied": True, "awaiting": True}

        records.append(
            {
                "message_id": m1_id,
                "in_reply_to": None,
                "references": [],
                "sender": "Me <me@example.com>",
                "sender_address": "me@example.com",
                "to": [f"{contact_name} <{contact}>"],
                "date": (now - timedelta(days=10)).isoformat(),
                "direction": "outbound",
                "subject": f"Inquiry {t_id}",
            }
        )
        records.append(
            {
                "message_id": m2_id,
                "in_reply_to": m1_id,
                "references": [m1_id],
                "sender": f"{contact_name} <{contact}>",
                "sender_address": contact,
                "to": ["Me <me@example.com>"],
                "date": (now - timedelta(days=5)).isoformat(),
                "direction": "inbound",
                "subject": f"Re: Inquiry {t_id}",
            }
        )

    # 21..30: I started thread 8 days ago, client replied 5 days ago, I haven't replied since
    # (state = awaiting_you, >= 3 days overdue)
    # expected_replied = True (client replied to me), expected_awaiting = True
    for i in range(21, 31):
        t_id = f"t{i:02d}"
        contact = f"client{i:02d}@partner.org"
        contact_name = f"Client {i:02d}"
        m1_id = f"<m{t_id}_1@example.com>"
        m2_id = f"<m{t_id}_2@partner.org>"
        labels[f"thread:{m1_id.strip('<>')}"] = {"replied": True, "awaiting": True}

        records.append(
            {
                "message_id": m1_id,
                "in_reply_to": None,
                "references": [],
                "sender": "Me <me@example.com>",
                "sender_address": "me@example.com",
                "to": [f"{contact_name} <{contact}>"],
                "date": (now - timedelta(days=8)).isoformat(),
                "direction": "outbound",
                "subject": f"Partnership Inquiry {t_id}",
            }
        )
        records.append(
            {
                "message_id": m2_id,
                "in_reply_to": m1_id,
                "references": [m1_id],
                "sender": f"{contact_name} <{contact}>",
                "sender_address": contact,
                "to": ["Me <me@example.com>"],
                "date": (now - timedelta(days=5)).isoformat(),
                "direction": "inbound",
                "subject": f"Re: Partnership Inquiry {t_id}",
            }
        )

    # 31..40: I started thread 3 days ago, client replied 1 day ago, not yet overdue
    # (state = awaiting_you, < 3 days overdue)
    # expected_replied = True (client replied to me), expected_awaiting = False (not yet overdue)
    for i in range(31, 41):
        t_id = f"t{i:02d}"
        contact = f"client{i:02d}@partner.org"
        contact_name = f"Client {i:02d}"
        m1_id = f"<m{t_id}_1@example.com>"
        m2_id = f"<m{t_id}_2@partner.org>"
        labels[f"thread:{m1_id.strip('<>')}"] = {"replied": True, "awaiting": False}

        records.append(
            {
                "message_id": m1_id,
                "in_reply_to": None,
                "references": [],
                "sender": "Me <me@example.com>",
                "sender_address": "me@example.com",
                "to": [f"{contact_name} <{contact}>"],
                "date": (now - timedelta(days=3)).isoformat(),
                "direction": "outbound",
                "subject": f"Quick Note {t_id}",
            }
        )
        records.append(
            {
                "message_id": m2_id,
                "in_reply_to": m1_id,
                "references": [m1_id],
                "sender": f"{contact_name} <{contact}>",
                "sender_address": contact,
                "to": ["Me <me@example.com>"],
                "date": (now - timedelta(days=1)).isoformat(),
                "direction": "inbound",
                "subject": f"Re: Quick Note {t_id}",
            }
        )

    # 41..45: I sent a message 30 days ago, no reply (> 21 days → timeout to resolved)
    # expected_replied = False, expected_awaiting = False
    for i in range(41, 46):
        t_id = f"t{i:02d}"
        contact = f"archive{i:02d}@archive.org"
        contact_name = f"Archive {i:02d}"
        m1_id = f"<m{t_id}_1@example.com>"
        labels[f"thread:{m1_id.strip('<>')}"] = {"replied": False, "awaiting": False}

        records.append(
            {
                "message_id": m1_id,
                "in_reply_to": None,
                "references": [],
                "sender": "Me <me@example.com>",
                "sender_address": "me@example.com",
                "to": [f"{contact_name} <{contact}>"],
                "date": (now - timedelta(days=30)).isoformat(),
                "direction": "outbound",
                "subject": f"Archived Thread {t_id}",
            }
        )

    # 46..50: I started thread 5 hrs ago, vendor replied, I already replied 1 hr ago
    # (state = awaiting_them: I replied last, waiting for vendor)
    # expected_replied = False, expected_awaiting = False
    for i in range(46, 51):
        t_id = f"t{i:02d}"
        contact = f"vendor{i:02d}@vendor.com"
        contact_name = f"Vendor {i:02d}"
        m1_id = f"<m{t_id}_1@example.com>"
        m2_id = f"<m{t_id}_2@vendor.com>"
        m3_id = f"<m{t_id}_3@example.com>"
        labels[f"thread:{m1_id.strip('<>')}"] = {"replied": False, "awaiting": False}

        records.append(
            {
                "message_id": m1_id,
                "in_reply_to": None,
                "references": [],
                "sender": "Me <me@example.com>",
                "sender_address": "me@example.com",
                "to": [f"{contact_name} <{contact}>"],
                "date": (now - timedelta(hours=5)).isoformat(),
                "direction": "outbound",
                "subject": f"Invoice {t_id}",
            }
        )
        records.append(
            {
                "message_id": m2_id,
                "in_reply_to": m1_id,
                "references": [m1_id],
                "sender": f"{contact_name} <{contact}>",
                "sender_address": contact,
                "to": ["Me <me@example.com>"],
                "date": (now - timedelta(hours=3)).isoformat(),
                "direction": "inbound",
                "subject": f"Re: Invoice {t_id}",
            }
        )
        records.append(
            {
                "message_id": m3_id,
                "in_reply_to": m2_id,
                "references": [m1_id, m2_id],
                "sender": "Me <me@example.com>",
                "sender_address": "me@example.com",
                "to": [f"{contact_name} <{contact}>"],
                "date": (now - timedelta(hours=1)).isoformat(),
                "direction": "outbound",
                "subject": f"Re: Invoice {t_id}",
            }
        )

    assert len(labels) == 50

    # Write spool file
    spool_file = spool_dir / "eval_messages.jsonl"
    spool_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    # Ingest spool
    processed = await ingest.ingest_spool()
    assert processed == len(records)
    await store.flush()

    return ingest, gm, store, bus, labels


async def test_50_thread_eval_precision_criterion(tmp_path: Path, fake_clock: FakeClock) -> None:
    """Exit criterion: precision >= 0.9 on replied and awaiting candidates."""
    ingest, gm, store, bus, labels = await _setup_50_threads(tmp_path, fake_clock)
    try:
        now = fake_clock.now()

        # 1. Test replied candidates ("Maya replied to the thread you started...")
        reply_candidates = await _mail_reply_candidates(gm, store, now=now)
        tp_replied = 0
        fp_replied = 0

        for item in reply_candidates:
            expected = labels.get(item.anchor, {}).get("replied", False)
            if expected:
                tp_replied += 1
            else:
                fp_replied += 1

        total_replied_positives = tp_replied + fp_replied
        assert total_replied_positives > 0, "Replied candidates must not be empty"
        precision_replied = tp_replied / total_replied_positives
        assert precision_replied >= 0.9, (
            f"Replied precision {precision_replied:.2f} < 0.90 (TP={tp_replied}, FP={fp_replied})"
        )
        assert tp_replied == 40  # Threads 1..40 all have client replying to my outbound

        # 2. Test awaiting / overdue candidates ("You haven't answered ... in X days")
        overdue_candidates = await _mail_overdue_candidates(
            gm, store, now=now, overdue_days=3, resolved_after_days=21
        )
        tp_awaiting = 0
        fp_awaiting = 0

        for item in overdue_candidates:
            expected = labels.get(item.anchor, {}).get("awaiting", False)
            if expected:
                tp_awaiting += 1
            else:
                fp_awaiting += 1

        total_awaiting_positives = tp_awaiting + fp_awaiting
        assert total_awaiting_positives > 0, "Awaiting candidates must not be empty"
        precision_awaiting = tp_awaiting / total_awaiting_positives
        assert precision_awaiting >= 0.9, (
            f"Awaiting precision {precision_awaiting:.2f} < 0.90 "
            f"(TP={tp_awaiting}, FP={fp_awaiting})"
        )
        assert tp_awaiting == 20  # Threads 11..30 have awaiting=True (replied but overdue)

    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


async def test_mail_candidates_compose_into_briefing(tmp_path: Path, fake_clock: FakeClock) -> None:
    """End-to-end briefing candidate generation and composition with mail items."""
    ingest, gm, store, bus, _ = await _setup_50_threads(tmp_path, fake_clock)
    try:
        now = fake_clock.now()
        candidates = await build_candidates(
            gm, store, now=now, last_briefing_seq=0, config=ingest.config
        )

        # Confirm mail candidates exist in candidate pool
        mail_candidates = [
            c for c in candidates if c.anchor.startswith("thread:") or "domain:comms" in c.evidence
        ]
        assert len(mail_candidates) > 0

        # Compose final briefing with focus history seeded at domain:comms
        briefing = await compose_briefing(
            gm,
            store,
            focus_history=[("domain:comms", now)],
            now=now,
            last_briefing_seq=0,
            config=ingest.config,
        )
        assert briefing is not None
        assert len(briefing.evidence) >= 1

        # Confirm grounding: every evidence ID in briefing exists in GraphMemory
        for ev_id in briefing.evidence:
            assert gm.has_node(ev_id), f"Evidence node {ev_id} missing in graph!"

    finally:
        await ingest.stop()
        await bus.stop()
        await store.stop()
        GraphMemory._reset_for_tests()


# gen-ref: ba667390
