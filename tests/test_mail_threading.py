# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Unit tests for email threading and participant normalization (S1)."""

from datetime import UTC, datetime

from neuropaca.sensing.mail_threading import (
    Threader,
    clean_subject,
    extract_message_ids,
    normalize_email_address,
    normalize_message_id,
    person_slug,
)


def test_normalize_message_id() -> None:
    assert normalize_message_id("<foo@bar.com>") == "foo@bar.com"
    assert normalize_message_id("  <foo@bar.com>  ") == "foo@bar.com"
    assert normalize_message_id("foo@bar.com") == "foo@bar.com"
    assert normalize_message_id("") == ""
    assert normalize_message_id(None) == ""


def test_extract_message_ids() -> None:
    raw = "<a@x> <b@x>\n<c@x>"
    assert extract_message_ids(raw) == ["a@x", "b@x", "c@x"]
    assert extract_message_ids(["<a@x>", "<b@x>"]) == ["a@x", "b@x"]
    assert extract_message_ids(None) == []


def test_normalize_email_address() -> None:
    name, addr = normalize_email_address("Maya Angelou <maya@example.com>")
    assert name == "Maya Angelou"
    assert addr == "maya@example.com"

    name2, addr2 = normalize_email_address("arjun.sharma@corp.com")
    assert name2 == "Arjun Sharma"
    assert addr2 == "arjun.sharma@corp.com"

    name3, addr3 = normalize_email_address("Maya Angelou")
    assert name3 == "Maya Angelou"
    assert addr3 == ""

    name4, addr4 = normalize_email_address("maya-corp-com")
    assert name4 == "maya-corp-com"
    assert addr4 == ""


def test_person_slug() -> None:
    assert person_slug("Maya Angelou", "maya@example.com") == "maya-example-com"
    assert person_slug("Arjun", "arjun@example.com") == "arjun-example-com"
    assert person_slug("", "bob.smith@corp.com") == "bob-smith-corp-com"
    assert person_slug("alice@corp.com", "alice@corp.com") == "alice-corp-com"


def test_person_slug_prevents_same_name_collision() -> None:
    """Two different people sharing a display name must have distinct slugs."""
    slug_a = person_slug("Maya", "maya@corp-a.com")
    slug_b = person_slug("Maya", "maya@corp-b.com")
    assert slug_a != slug_b
    assert slug_a == "maya-corp-a-com"
    assert slug_b == "maya-corp-b-com"

    slug_sup1 = person_slug("Support", "support@github.com")
    slug_sup2 = person_slug("Support", "support@google.com")
    assert slug_sup1 != slug_sup2


def test_person_slug_same_address_name_variations() -> None:
    """Different display names for the same address produce the same slug."""
    slug_full = person_slug("Bob Smith", "bob@example.com")
    slug_first = person_slug("Bob", "bob@example.com")
    slug_empty = person_slug("", "bob@example.com")
    assert slug_full == slug_first == slug_empty == "bob-example-com"


def test_clean_subject() -> None:
    assert clean_subject("Re: Project update") == "Project update"
    assert clean_subject("RE:  FWD: Urgent request") == "Urgent request"
    assert clean_subject("Hello world") == "Hello world"


def test_threading_linear_chain() -> None:
    threader = Threader()
    # Message 1: Root
    t1 = threader.get_thread_id(
        message_id="<msg-root@example.com>",
        in_reply_to=None,
        references=[],
        subject="Project Kickoff",
    )
    assert t1 == "msg-root@example.com"

    # Message 2: Reply to root
    t2 = threader.get_thread_id(
        message_id="<msg-reply1@example.com>",
        in_reply_to="<msg-root@example.com>",
        references=["<msg-root@example.com>"],
        subject="Re: Project Kickoff",
    )
    assert t2 == "msg-root@example.com"

    # Message 3: Reply to reply1
    t3 = threader.get_thread_id(
        message_id="<msg-reply2@example.com>",
        in_reply_to="<msg-reply1@example.com>",
        references=["<msg-root@example.com>", "<msg-reply1@example.com>"],
        subject="Re: Project Kickoff",
    )
    assert t3 == "msg-root@example.com"


def test_threading_out_of_order_arrival() -> None:
    threader = Threader()
    # Reply arrives first, referencing the root
    t_reply = threader.get_thread_id(
        message_id="<reply@example.com>",
        in_reply_to="<root@example.com>",
        references=["<root@example.com>"],
        subject="Re: Out of order test",
    )
    assert t_reply == "root@example.com"

    # Root arrives second
    t_root = threader.get_thread_id(
        message_id="<root@example.com>",
        in_reply_to=None,
        references=[],
        subject="Out of order test",
    )
    assert t_root == "root@example.com"


def test_threading_branching_tree() -> None:
    threader = Threader()
    t_root = threader.get_thread_id("<root@example.com>")
    # Two separate replies to the root
    t_branch_a = threader.get_thread_id(
        "<branch-a@example.com>",
        in_reply_to="<root@example.com>",
        references=["<root@example.com>"],
    )
    t_branch_b = threader.get_thread_id(
        "<branch-b@example.com>",
        in_reply_to="<root@example.com>",
        references=["<root@example.com>"],
    )
    assert t_branch_a == t_root
    assert t_branch_b == t_root


def test_threading_subject_fallback() -> None:
    threader = Threader()
    now = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
    t1 = threader.get_thread_id(
        "<orphan1@example.com>",
        subject="No Headers Discussion",
        date=now,
    )
    t2 = threader.get_thread_id(
        "<orphan2@example.com>",
        subject="Re: No Headers Discussion",
        date=now,
    )
    assert t1 == t2


# gen-ref: bd314dfb
