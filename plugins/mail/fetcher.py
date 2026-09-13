#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""External mail fetcher process (S1 · VISION_PHASES.md).

Runs completely outside the NeuroPACA daemon (`scripts/systemd/neuropaca-mail.service`),
fetching headers only (never bodies by default) from IMAP or local Maildir, and writing
append-only JSONL records to `data/plugins/mail/spool/messages.jsonl` with 0600 permissions.

Maintains the daemon's zero-egress guarantee (rules.md §6).
"""

from __future__ import annotations

import argparse
import email
import email.header
import email.utils
import imaplib
import json
import logging
import mailbox
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger("neuropaca-mail-fetcher")

_MSGID_RE = re.compile(r"<[^>]+>")
_IMAP_TIMEOUT_SECONDS = 30.0


def _decode_header_str(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
        decoded: list[str] = []
        for piece, charset in parts:
            if isinstance(piece, bytes):
                enc = charset or "utf-8"
                try:
                    decoded.append(piece.decode(enc, errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    decoded.append(piece.decode("utf-8", errors="replace"))
            else:
                decoded.append(str(piece))
        return " ".join("".join(decoded).split())
    except Exception:
        return str(raw).strip()


def _parse_message_date(date_str: str | None) -> str:
    if not date_str:
        return datetime.now(UTC).isoformat()
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except Exception:
        return datetime.now(UTC).isoformat()


def _extract_ids(header_val: str | None) -> list[str]:
    if not header_val:
        return []
    return _MSGID_RE.findall(header_val)


def _load_forgotten(spool_dir: Path) -> set[str]:
    path = spool_dir / "forgotten.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x).strip().lower() for x in data if x}
        return set()
    except Exception as e:
        _log.warning("Failed to load %s: %s", path, e)
        return set()


def _is_forgotten(
    sender_addr: str,
    to_addrs: list[str],
    forgotten: set[str],
) -> bool:
    if not forgotten:
        return False
    if sender_addr and sender_addr.lower() in forgotten:
        return True
    for to_a in to_addrs:
        if to_a and to_a.lower() in forgotten:
            return True
    return False


def _safe_append_records(spool_dir: Path, records: list[dict[str, Any]]) -> int:
    if not records:
        return 0
    spool_dir.mkdir(parents=True, exist_ok=True)
    target = spool_dir / "messages.jsonl"

    fd = os.open(str(target), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    written = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        _log.exception("Failed writing records to %s", target)
        raise
    return written


class StateManager:
    """Persists fetch progress per folder across runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.state: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if self.path.is_file():
            try:
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                _log.warning("Failed loading fetch state from %s: %s", self.path, e)
                self.state = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def get_last_uid(self, folder: str) -> int:
        folders = self.state.get("folders", {})
        folder_state = folders.get(folder, {})
        return int(folder_state.get("last_uid", 0))

    def set_last_uid(self, folder: str, uid: int) -> None:
        folders = self.state.setdefault("folders", {})
        folder_state = folders.setdefault(folder, {})
        folder_state["last_uid"] = max(folder_state.get("last_uid", 0), uid)
        folder_state["last_synced"] = datetime.now(UTC).isoformat()

    def get_seen_keys(self, folder: str) -> set[str]:
        folders = self.state.setdefault("folders", {})
        folder_state = folders.setdefault(folder, {})
        return set(folder_state.get("seen_keys", []))

    def add_seen_keys(self, folder: str, keys: set[str]) -> None:
        folders = self.state.setdefault("folders", {})
        folder_state = folders.setdefault(folder, {})
        current = set(folder_state.get("seen_keys", []))
        current.update(keys)
        # Bounded retention of seen keys: keep latest 10,000 keys
        if len(current) > 10000:
            current = set(list(current)[-10000:])
        folder_state["seen_keys"] = sorted(current)
        folder_state["last_synced"] = datetime.now(UTC).isoformat()


def _get_password(password_arg: str | None, password_cmd: str | None) -> str:
    if password_arg:
        return password_arg
    env_pwd = os.environ.get("NEUROPACA_MAIL_PASSWORD")
    if env_pwd:
        return env_pwd
    if password_cmd:
        try:
            cmd = shlex.split(password_cmd)
            out = subprocess.check_output(cmd, text=True, timeout=15)
            return out.strip()
        except Exception as e:
            _log.error("Failed to execute password command '%s': %s", password_cmd, e)
            raise RuntimeError(f"Password command failed: {e}") from e
    raise ValueError(
        "No password provided. Set --password, --password-cmd, or NEUROPACA_MAIL_PASSWORD."
    )


def _parse_email_message(
    msg: email.message.Message,
    folder: str,
    user_address: str,
    retain_subject: bool = False,
    snippet_chars: int = 0,
) -> dict[str, Any]:
    """Parse raw email message into standard JSONL record dict."""
    msg_id = msg.get("Message-ID", "")
    in_reply_to = msg.get("In-Reply-To", None)
    if in_reply_to:
        in_reply_to = in_reply_to.strip()
    references = _extract_ids(msg.get("References"))

    raw_from = _decode_header_str(msg.get("From", ""))
    _, sender_addr = email.utils.parseaddr(raw_from)
    sender_addr = sender_addr.strip().lower()

    to_headers = msg.get_all("To", [])
    raw_tos: list[str] = []
    to_addrs: list[str] = []
    for val in to_headers:
        decoded = _decode_header_str(val)
        raw_tos.append(decoded)
        for _, addr in email.utils.getaddresses([decoded]):
            clean = addr.strip().lower()
            if clean:
                to_addrs.append(clean)

    date_iso = _parse_message_date(msg.get("Date"))

    # Determine direction
    is_sent_folder = any(s in folder.lower() for s in ("sent", "outbox"))
    is_sent_user = bool(user_address and sender_addr == user_address.strip().lower())
    direction = "outbound" if (is_sent_folder or is_sent_user) else "inbound"

    subject = _decode_header_str(msg.get("Subject")) if retain_subject else None

    snippet = None
    if snippet_chars > 0:
        try:
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if isinstance(payload, bytes):
                            body = payload.decode("utf-8", errors="replace")
                            break
            else:
                payload = msg.get_payload(decode=True)
                if isinstance(payload, bytes):
                    body = payload.decode("utf-8", errors="replace")
            if body:
                snippet = " ".join(body.split())[:snippet_chars]
        except Exception:
            snippet = None

    return {
        "message_id": msg_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "sender": raw_from,
        "sender_address": sender_addr,
        "to": raw_tos,
        "to_addresses": to_addrs,
        "date": date_iso,
        "direction": direction,
        "folder": folder,
        "subject": subject,
        "snippet": snippet,
    }


def fetch_imap(
    host: str,
    port: int,
    user: str,
    password: str,
    folders: list[str],
    spool_dir: Path,
    state: StateManager,
    user_address: str,
    retain_subject: bool = False,
    snippet_chars: int = 0,
) -> int:
    """Fetch new email headers from IMAP server into spool."""
    forgotten = _load_forgotten(spool_dir)
    total_written = 0

    _log.info("Connecting to IMAP %s:%d as %s", host, port, user)
    # Found in real use: no timeout means a stalled connection (server accepts
    # the TCP handshake but stops answering mid-fetch) hangs the blocking
    # read() forever — nothing to retry, nothing for systemd's Restart= to
    # act on. The per-loop `except Exception` above already logs and moves on
    # to the next poll cycle, so a bounded timeout here is enough to recover.
    client = imaplib.IMAP4_SSL(host, port, timeout=_IMAP_TIMEOUT_SECONDS)
    try:
        client.login(user, password)
        for folder in folders:
            folder = folder.strip()
            if not folder:
                continue
            typ, _ = client.select(f'"{folder}"', readonly=True)
            if typ != "OK":
                _log.warning("Could not select folder %s (status: %s)", folder, typ)
                continue

            last_uid = state.get_last_uid(folder)
            if last_uid > 0:
                search_criterion = f"UID {last_uid + 1}:*"
            else:
                search_criterion = "ALL"

            # `None` is the documented charset placeholder for UID SEARCH — CPython's
            # imaplib._command() explicitly skips None args when building the wire
            # command, so this is correct at runtime; typeshed's `*args: str` stub
            # just doesn't reflect that.
            typ, data = client.uid("SEARCH", None, search_criterion)  # type: ignore[arg-type]
            if typ != "OK" or not data or not data[0]:
                continue

            uids = [int(u) for u in data[0].split() if u.isdigit()]
            new_uids = [u for u in uids if u > last_uid]
            if not new_uids:
                continue

            _log.info("Folder %s: %d new messages to fetch", folder, len(new_uids))
            batch_records: list[dict[str, Any]] = []
            max_fetched_uid = last_uid

            for uid in new_uids:
                fetch_parts = "(BODY.PEEK[HEADER])"
                if snippet_chars > 0:
                    fetch_parts = f"(BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.{snippet_chars * 2}>)"

                typ, fetch_data = client.uid("FETCH", str(uid), fetch_parts)
                if typ != "OK" or not fetch_data:
                    continue

                raw_header: bytes | None = None
                raw_text: bytes | None = None

                for item in fetch_data:
                    if isinstance(item, tuple) and len(item) == 2:
                        header_tag = item[0]
                        if b"HEADER" in header_tag:
                            raw_header = item[1]
                        elif b"TEXT" in header_tag:
                            raw_text = item[1]

                if not raw_header:
                    continue

                msg = email.message_from_bytes(raw_header)
                rec = _parse_email_message(
                    msg,
                    folder=folder,
                    user_address=user_address,
                    retain_subject=retain_subject,
                    snippet_chars=snippet_chars,
                )

                if snippet_chars > 0 and raw_text and not rec.get("snippet"):
                    try:
                        text_str = raw_text.decode("utf-8", errors="replace")
                        rec["snippet"] = " ".join(text_str.split())[:snippet_chars]
                    except Exception:
                        pass

                if not _is_forgotten(
                    rec.get("sender_address", ""),
                    rec.get("to_addresses", []),
                    forgotten,
                ):
                    batch_records.append(rec)

                max_fetched_uid = max(max_fetched_uid, uid)

            written = _safe_append_records(spool_dir, batch_records)
            total_written += written
            state.set_last_uid(folder, max_fetched_uid)
            state.save()
    finally:
        try:
            client.logout()
        except Exception:
            pass

    return total_written


def fetch_maildir(
    maildir_path: Path,
    folders: list[str],
    spool_dir: Path,
    state: StateManager,
    user_address: str,
    retain_subject: bool = False,
    snippet_chars: int = 0,
) -> int:
    """Fetch new messages from local Maildir into spool."""
    forgotten = _load_forgotten(spool_dir)
    total_written = 0

    if not folders:
        folders = ["."]

    for folder_rel in folders:
        folder_rel = folder_rel.strip()
        folder_dir = maildir_path if folder_rel in (".", "") else maildir_path / folder_rel
        if not folder_dir.exists():
            _log.warning("Maildir folder does not exist: %s", folder_dir)
            continue

        mdir = mailbox.Maildir(str(folder_dir), factory=None)
        seen_keys = state.get_seen_keys(folder_rel)
        new_keys: set[str] = set()
        records: list[dict[str, Any]] = []

        for key in mdir.keys():
            if key in seen_keys:
                continue
            try:
                msg = mdir.get_message(key)
                rec = _parse_email_message(
                    msg,
                    folder=folder_rel,
                    user_address=user_address,
                    retain_subject=retain_subject,
                    snippet_chars=snippet_chars,
                )
                new_keys.add(key)
                if not _is_forgotten(
                    rec.get("sender_address", ""),
                    rec.get("to_addresses", []),
                    forgotten,
                ):
                    records.append(rec)
            except Exception as e:
                _log.warning("Error reading maildir key %s: %s", key, e)

        written = _safe_append_records(spool_dir, records)
        total_written += written
        state.add_seen_keys(folder_rel, new_keys)
        state.save()

    return total_written


def main() -> int:
    parser = argparse.ArgumentParser(description="NeuroPACA external mail fetcher (S1)")
    parser.add_argument("--host", type=str, default="", help="IMAP server hostname")
    parser.add_argument("--port", type=int, default=993, help="IMAP port (default: 993)")
    parser.add_argument("--user", type=str, default="", help="IMAP username / login")
    parser.add_argument("--password", type=str, default="", help="Explicit password")
    parser.add_argument(
        "--password-cmd",
        type=str,
        default="",
        help="Command to retrieve password from secret store",
    )
    parser.add_argument(
        "--folders",
        type=str,
        default="INBOX",
        help="Comma-separated mail folders to fetch (default: INBOX)",
    )
    parser.add_argument(
        "--spool-dir",
        type=str,
        default="data/plugins/mail/spool",
        help="Directory to write JSONL spool files (default: data/plugins/mail/spool)",
    )
    parser.add_argument(
        "--maildir",
        type=str,
        default="",
        help="Local maildir path (alternative to IMAP)",
    )
    parser.add_argument(
        "--user-address",
        type=str,
        default="",
        help="Primary user email address for direction inference",
    )
    parser.add_argument(
        "--retain-subject",
        action="store_true",
        default=False,
        help="Retain email subject line (default: False)",
    )
    parser.add_argument(
        "--snippet-chars",
        type=int,
        default=0,
        help="Max characters of snippet to retain (default: 0 = disabled)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Polling interval in seconds (default: 300)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Run once and exit",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default="",
        help="State file tracking UID progress (default: <spool-dir>/.fetch_state.json)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    spool_dir = Path(args.spool_dir)
    state_file = Path(args.state_file) if args.state_file else spool_dir / ".fetch_state.json"
    state = StateManager(state_file)

    user_addr = args.user_address or args.user
    folders = [f.strip() for f in args.folders.split(",") if f.strip()]

    stop_requested = False

    def _sig_handler(signum: int, frame: Any) -> None:
        nonlocal stop_requested
        _log.info("Received signal %d, shutting down...", signum)
        stop_requested = True

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    while not stop_requested:
        try:
            if args.maildir:
                fetch_maildir(
                    maildir_path=Path(args.maildir),
                    folders=folders,
                    spool_dir=spool_dir,
                    state=state,
                    user_address=user_addr,
                    retain_subject=args.retain_subject,
                    snippet_chars=args.snippet_chars,
                )
            elif args.host:
                password = _get_password(args.password, args.password_cmd)
                fetch_imap(
                    host=args.host,
                    port=args.port,
                    user=args.user,
                    password=password,
                    folders=folders,
                    spool_dir=spool_dir,
                    state=state,
                    user_address=user_addr,
                    retain_subject=args.retain_subject,
                    snippet_chars=args.snippet_chars,
                )
            else:
                _log.error("Neither --host nor --maildir was provided.")
                return 1
        except Exception as e:
            _log.exception("Fetch error: %s", e)

        if args.once or stop_requested:
            break

        # Sleep interval in small chunks to respond quickly to signals
        end_time = time.time() + args.interval
        while time.time() < end_time and not stop_requested:
            time.sleep(1)

    return 0


if __name__ == "__main__":
    sys.exit(main())


# gen-ref: 28b4c20e
