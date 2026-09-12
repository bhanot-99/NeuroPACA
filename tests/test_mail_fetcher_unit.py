# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for mail fetcher systemd unit hardening and fetcher execution (S1)."""

from __future__ import annotations

import email.message
import json
import mailbox
import stat
from pathlib import Path

import pytest
from plugins.mail.fetcher import (
    StateManager,
    _get_password,
    fetch_maildir,
)


def test_neuropacad_daemon_egress_guarantees_remain_intact() -> None:
    """The core daemon MUST have zero egress: PrivateNetwork=true and AF_UNIX only."""
    repo = Path(__file__).resolve().parents[1]
    daemon_unit = repo / "scripts" / "systemd" / "neuropacad.service"
    text = daemon_unit.read_text("utf-8")

    assert "PrivateNetwork=true" in text
    assert "RestrictAddressFamilies=AF_UNIX" in text
    assert "AF_INET" not in text
    assert "AF_INET6" not in text
    assert "ReadWritePaths=__REPO__/data" in text


def test_neuropaca_mail_service_hardening_and_egress_scoping() -> None:
    """The mail fetcher runs outside the daemon with scoped host-only egress."""
    repo = Path(__file__).resolve().parents[1]
    mail_unit = repo / "scripts" / "systemd" / "neuropaca-mail.service"
    assert mail_unit.is_file()
    text = mail_unit.read_text("utf-8")

    assert "PrivateNetwork=false" in text
    # systemd's IPAddressAllow requires literal IP addresses or CIDRs, never hostnames.
    assert "IPAddressAllow=__IMAP_HOST_IP__ 127.0.0.53/32" in text
    assert "IPAddressAllow=__IMAP_HOST__" not in text
    assert "IPAddressDeny=any" in text
    assert "__IMAP_HOST_IP__" in text
    assert "ahostsv4" in text  # install instructions must resolve host to IP
    assert "ReadWritePaths=__REPO__/data/plugins/mail/spool" in text
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=read-only" in text
    assert "NoNewPrivileges=true" in text
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in text
    assert "plugins/mail/fetcher.py" in text

    # ExecStart must connect by hostname, not the bare IP: imaplib ties the
    # connect-target and the TLS SNI/certificate-verification name together,
    # so connecting by IP would either break cert verification or require
    # hand-rolling SNI override — the IP is for IPAddressAllow's sandboxing
    # only, never for the actual IMAP connection.
    execstart_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert "--host=__IMAP_HOST__" in execstart_line
    assert "__IMAP_HOST_IP__" not in execstart_line

    # The fetcher's own DNS lookup at connect time (for --host=__IMAP_HOST__)
    # must not be blocked by the tightened IPAddressDeny=any — the stub
    # resolver needs its own allow-list entry (man systemd.resource-control:
    # "the loopback interface is not treated in any special way").
    assert "127.0.0.53/32" in text


def test_get_password_resolution() -> None:
    assert _get_password("direct_secret", None) == "direct_secret"
    # Shell command execution
    pwd = _get_password(None, "echo 'secret_from_pass'")
    assert pwd == "secret_from_pass"

    with pytest.raises(ValueError, match="No password provided"):
        _get_password(None, None)


def test_fetch_maildir_ingestion_and_privacy_defaults(tmp_path: Path) -> None:
    """Tests maildir fetch: 0600 mode, header-only privacy, no duplicates on next run."""
    maildir_dir = tmp_path / "maildir"
    spool_dir = tmp_path / "spool"
    state_file = spool_dir / ".fetch_state.json"

    # Set up a mailbox.Maildir
    mdir = mailbox.Maildir(str(maildir_dir), create=True)
    msg1 = email.message.EmailMessage()
    msg1["Message-ID"] = "<msg1@corp.com>"
    msg1["From"] = "Alice Walker <alice@corp.com>"
    msg1["To"] = "User <user@example.com>"
    msg1["Subject"] = "Confidential project update"
    msg1["Date"] = "Wed, 10 Sep 2026 12:00:00 +0000"
    msg1.set_content("This body must never be saved in spool.")
    mdir.add(msg1)

    state = StateManager(state_file)

    # 1. Default fetch: retain_subject=False, snippet_chars=0
    written = fetch_maildir(
        maildir_path=maildir_dir,
        folders=["."],
        spool_dir=spool_dir,
        state=state,
        user_address="user@example.com",
        retain_subject=False,
        snippet_chars=0,
    )
    assert written == 1

    spool_file = spool_dir / "messages.jsonl"
    assert spool_file.is_file()

    # Check file permissions are 0600
    file_stat = spool_file.stat()
    assert stat.S_IMODE(file_stat.st_mode) == 0o600

    lines = spool_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec1 = json.loads(lines[0])
    assert rec1["message_id"] == "<msg1@corp.com>"
    assert rec1["sender_address"] == "alice@corp.com"
    assert rec1["direction"] == "inbound"
    assert rec1["subject"] is None  # Privacy default!
    assert rec1.get("snippet") is None
    assert "This body must never be saved" not in spool_file.read_text("utf-8")

    # 2. Second fetch with same state: should see 0 new messages
    written_again = fetch_maildir(
        maildir_path=maildir_dir,
        folders=["."],
        spool_dir=spool_dir,
        state=state,
        user_address="user@example.com",
    )
    assert written_again == 0

    # 3. Add second message with retain_subject=True
    msg2 = email.message.EmailMessage()
    msg2["Message-ID"] = "<msg2@corp.com>"
    msg2["In-Reply-To"] = "<msg1@corp.com>"
    msg2["From"] = "User <user@example.com>"
    msg2["To"] = "Alice Walker <alice@corp.com>"
    msg2["Subject"] = "Re: Confidential project update"
    msg2["Date"] = "Wed, 10 Sep 2026 13:00:00 +0000"
    msg2.set_content("Reply body")
    mdir.add(msg2)

    written_2 = fetch_maildir(
        maildir_path=maildir_dir,
        folders=["."],
        spool_dir=spool_dir,
        state=state,
        user_address="user@example.com",
        retain_subject=True,
    )
    assert written_2 == 1

    lines = spool_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec2 = json.loads(lines[1])
    assert rec2["message_id"] == "<msg2@corp.com>"
    assert rec2["in_reply_to"] == "<msg1@corp.com>"
    assert rec2["direction"] == "outbound"
    assert rec2["subject"] == "Re: Confidential project update"


def test_fetch_skips_forgotten_addresses(tmp_path: Path) -> None:
    maildir_dir = tmp_path / "maildir"
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    state_file = spool_dir / ".fetch_state.json"

    # Write forgotten.json with blocked address
    (spool_dir / "forgotten.json").write_text(
        json.dumps(["blocked@corp.com", "person:bob-smith"]), encoding="utf-8"
    )

    mdir = mailbox.Maildir(str(maildir_dir), create=True)

    msg_blocked = email.message.EmailMessage()
    msg_blocked["Message-ID"] = "<blocked@corp.com>"
    msg_blocked["From"] = "Blocked <blocked@corp.com>"
    msg_blocked["To"] = "User <user@example.com>"
    msg_blocked["Date"] = "Wed, 10 Sep 2026 12:00:00 +0000"
    mdir.add(msg_blocked)

    msg_allowed = email.message.EmailMessage()
    msg_allowed["Message-ID"] = "<allowed@corp.com>"
    msg_allowed["From"] = "Allowed <allowed@corp.com>"
    msg_allowed["To"] = "User <user@example.com>"
    msg_allowed["Date"] = "Wed, 10 Sep 2026 12:05:00 +0000"
    mdir.add(msg_allowed)

    state = StateManager(state_file)
    written = fetch_maildir(
        maildir_path=maildir_dir,
        folders=["."],
        spool_dir=spool_dir,
        state=state,
        user_address="user@example.com",
    )
    assert written == 1

    spool_file = spool_dir / "messages.jsonl"
    lines = spool_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["message_id"] == "<allowed@corp.com>"
    assert rec["sender_address"] == "allowed@corp.com"


# gen-ref: e5c10fba
