# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S1 · `MailIngest` — correspondence ledger and thread state (VISION_PHASES.md).

Tails the mail spool (`data/plugins/mail/spool/*.jsonl`) using byte-offset watermarks,
writing:
- `EpisodeKind.MESSAGE_RECEIVED` / `MESSAGE_SENT` — the in/out correspondence ledger.
- `EpisodeKind.THREAD_STATE_FACT` — superseding facts: awaiting_you, awaiting_them, resolved.
- Graph nodes: `NodeType.PERSON` and `NodeType.THREAD` linked to each other and `domain:comms`.

Forget: scrubs episodes, facts, spool records, and maintains `forgotten.json` to prevent
the external fetcher from repopulating forgotten correspondence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.sensing.mail_threading import (
    Threader,
    extract_message_ids,
    normalize_email_address,
    person_slug,
)

_log = logging.getLogger(__name__)


def _parse_iso_utc(ts: str | None) -> datetime:
    if not ts:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return datetime.now(UTC)


class MailIngest(BaseModule):
    """Daemon-side correspondence sensor reading from the external spool."""

    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        episode_store: EpisodeStore | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        super().__init__("mail_ingest", event_bus, config)
        self._gm = graph_memory
        self._store = episode_store
        self._clock: Clock = clock or SystemClock()
        self._spool_dir = Path(config.mail_spool_dir)
        self._watermark_path = self._spool_dir / ".watermark.json"
        self._forgotten_path = self._spool_dir / "forgotten.json"
        self._threader_path = self._spool_dir / ".threader_state.json"
        self._threader = Threader()
        self._watermarks: dict[str, int] = {}
        self._forgotten: set[str] = set()
        self._ingested_count = 0
        self._poll_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def spool_dir(self) -> Path:
        return self._spool_dir

    @property
    def ingested_count(self) -> int:
        return self._ingested_count

    # ---------------------------------------------------------------- lifecycle
    async def initialize(self) -> None:
        self._spool_dir.mkdir(parents=True, exist_ok=True)
        await self._load_watermarks()
        await self._load_forgotten()
        await self._load_threader_state()

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        # Ingest whatever is waiting in the spool
        await self.ingest_spool()
        # Start background polling task
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self._save_watermarks()
        await self._save_threader_state()

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=True,
            detail=(
                f"messages_ingested={self._ingested_count}, "
                f"tracked_files={len(self._watermarks)}, "
                f"forgotten_count={len(self._forgotten)}"
            ),
        )

    # ----------------------------------------------------------- spool ingestion
    async def _poll_loop(self) -> None:
        while self.is_running:
            try:
                await self.poll_tick()
            except asyncio.CancelledError:
                break
            except Exception:
                _log.exception("MailIngest error in poll loop")
            await asyncio.sleep(self.config.mail_poll_interval_seconds)

    async def poll_tick(self) -> int:
        """Run a single polling cycle: ingest pending records and resolve timed-out threads."""
        processed = await self.ingest_spool()
        await self.check_resolved_threads()
        return processed

    async def ingest_spool(self) -> int:
        """Tail all *.jsonl files in spool_dir from their saved watermark offsets."""
        async with self._lock:
            await self._load_forgotten()
            if not self._spool_dir.exists():
                return 0

            spool_files = sorted(self._spool_dir.glob("*.jsonl"))
            total_processed = 0

            for spool_file in spool_files:
                if spool_file.name.startswith("."):
                    continue
                records, new_offset = await asyncio.to_thread(
                    self._read_new_records_blocking, spool_file
                )
                for record in records:
                    await self._process_record(record)
                    total_processed += 1
                self._watermarks[spool_file.name] = new_offset

            if total_processed > 0:
                if self._store is not None:
                    await self._store.flush()
                await self._check_resolved_threads()
                await self._save_watermarks()
                await self._save_threader_state()

            self._ingested_count += total_processed
            return total_processed

    def _read_new_records_blocking(self, path: Path) -> tuple[list[dict[str, Any]], int]:
        offset = self._watermarks.get(path.name, 0)
        file_size = path.stat().st_size
        if offset >= file_size:
            return [], offset

        records: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as fh:
            fh.seek(offset)
            while True:
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if self._is_record_forgotten(record):
                    continue
                records.append(record)
            new_offset = fh.tell()
        return records, new_offset

    def _is_record_forgotten(self, record: dict[str, Any]) -> bool:
        if not self._forgotten:
            return False
        sender = record.get("sender", "")
        sender_addr = record.get("sender_address", "")
        name, addr = normalize_email_address(sender)
        slug = person_slug(name or sender, addr or sender_addr)
        if (
            f"person:{slug}" in self._forgotten
            or slug in self._forgotten
            or addr in self._forgotten
            or sender_addr in self._forgotten
        ):
            return True

        for to_val in record.get("to", []):
            t_name, t_addr = normalize_email_address(to_val)
            t_slug = person_slug(t_name or to_val, t_addr)
            if (
                f"person:{t_slug}" in self._forgotten
                or t_slug in self._forgotten
                or t_addr in self._forgotten
            ):
                return True
        return False

    async def _process_record(self, record: dict[str, Any]) -> None:
        """Dispatch a single parsed record to EpisodeStore and GraphMemory."""
        msg_id = record.get("message_id", "")
        in_reply_to = record.get("in_reply_to")
        references = record.get("references", [])
        if isinstance(references, str):
            references = extract_message_ids(references)
        subject_raw = record.get("subject") or ""
        date = _parse_iso_utc(record.get("date"))

        thread_id = self._threader.get_thread_id(
            message_id=msg_id,
            in_reply_to=in_reply_to,
            references=references,
            subject=subject_raw,
            date=date,
        )

        direction = record.get("direction", "inbound").lower()
        sender_raw = record.get("sender", "")
        sender_addr = record.get("sender_address", "")
        sender_name, sender_clean_addr = normalize_email_address(sender_raw)
        if not sender_clean_addr and sender_addr:
            sender_clean_addr = sender_addr.strip().lower()

        to_list = record.get("to", [])
        to_addrs = record.get("to_addresses", [])
        recip_name, recip_addr = "", ""
        if to_list:
            recip_name, recip_addr = normalize_email_address(to_list[0])
        elif to_addrs:
            recip_name, recip_addr = normalize_email_address(to_addrs[0])

        is_sent = direction in ("outbound", "sent") or (
            self.config.mail_user_address
            and sender_clean_addr == self.config.mail_user_address.strip().lower()
        )

        if is_sent:
            kind = EpisodeKind.MESSAGE_SENT
            p_name = recip_name or recip_addr or "recipient"
            p_addr = recip_addr
            thread_state = "awaiting_them"
        else:
            kind = EpisodeKind.MESSAGE_RECEIVED
            p_name = sender_name or sender_clean_addr or "sender"
            p_addr = sender_clean_addr
            thread_state = "awaiting_you"

        p_slug = person_slug(p_name, p_addr)
        thread_entity = f"thread:{thread_id}"
        person_entity = f"person:{p_slug}"

        attrs: dict[str, Any] = {
            "message_id": msg_id,
            "direction": "sent" if is_sent else "received",
            "folder": record.get("folder", "INBOX"),
            "sender": sender_raw,
            "to": to_list,
        }
        if self.config.mail_retain_subject and subject_raw:
            attrs["subject"] = subject_raw
        snippet = record.get("snippet")
        if self.config.mail_snippet_chars > 0 and snippet:
            attrs["snippet"] = str(snippet)[: self.config.mail_snippet_chars]

        # 1. Record episode ledger row
        if self._store is not None:
            self._store.record_span(
                kind,
                thread_entity,
                start=date,
                end=date,
                obj=person_entity,
                source="mail",
                attrs=attrs,
            )
            # 2. Record superseding thread state fact
            self._store.assert_fact(
                EpisodeKind.THREAD_STATE_FACT,
                thread_entity,
                thread_state,
                valid_from=date,
                source="mail",
                attrs={"participant": p_slug, "last_message_id": msg_id},
            )

        # 3. Upsert graph nodes and edges
        thread_label = (
            subject_raw if (self.config.mail_retain_subject and subject_raw) else thread_entity
        )
        await self._upsert_graph_nodes(thread_entity, thread_label, person_entity, p_name)

    async def _upsert_graph_nodes(
        self, thread_entity: str, thread_label: str, person_entity: str, person_name: str
    ) -> None:
        await self._gm.upsert_node(
            thread_entity, NodeType.THREAD, attributes={"label": thread_label}
        )
        await self._gm.upsert_node(
            person_entity, NodeType.PERSON, attributes={"label": person_name}
        )
        await self._gm.add_edge(thread_entity, person_entity, relation=RelationType.RELATED_TO)
        if self._gm.has_node("domain:comms"):
            await self._gm.add_edge(thread_entity, "domain:comms", relation=RelationType.PART_OF)
            await self._gm.add_edge(person_entity, "domain:comms", relation=RelationType.PART_OF)

    async def check_resolved_threads(self) -> int:
        """Close open threads that have passed mail_resolved_after_days."""
        if self._store is None:
            return 0
        now = self._clock.now() if self._clock else datetime.now(UTC)
        cutoff = now - timedelta(days=self.config.mail_resolved_after_days)
        open_records = await self._store.at(now)
        resolved_count = 0
        for record in open_records:
            if (
                record.kind == str(EpisodeKind.THREAD_STATE_FACT)
                and record.object in ("awaiting_them", "awaiting_you")
                and record.t_valid is not None
                and record.t_valid < cutoff
            ):
                self._store.assert_fact(
                    EpisodeKind.THREAD_STATE_FACT,
                    record.subject,
                    "resolved",
                    valid_from=record.t_valid
                    + timedelta(days=self.config.mail_resolved_after_days),
                    source="mail",
                    attrs={"resolved_reason": "timeout"},
                )
                resolved_count += 1
        if resolved_count > 0:
            await self._store.flush()
        return resolved_count

    _check_resolved_threads = check_resolved_threads

    # ---------------------------------------------------------------- forget
    async def forget(self, person: str) -> int:
        """Scrub person from episodes, facts, graph, and spool, and record in forgotten.json."""
        async with self._lock:
            raw = (
                person.removeprefix("person:").strip()
                if person.startswith("person:")
                else person.strip()
            )
            name, addr = normalize_email_address(raw)
            slug = person_slug(name or raw, addr)
            person_entity = f"person:{slug}"

            # 1. Update forgotten.json deny-list
            self._forgotten.add(person_entity)
            self._forgotten.add(slug)
            if addr:
                self._forgotten.add(addr)
            if person:
                self._forgotten.add(person.strip().lower())
            if raw:
                self._forgotten.add(raw.lower())
            await self._save_forgotten()

            # 2. Scrub spool files
            scrubbed_lines = await asyncio.to_thread(self._scrub_spool_blocking)

            # 3. Scrub EpisodeStore
            removed_episodes = 0
            if self._store is not None:
                removed_episodes += await self._store.forget(person_entity)
                if addr:
                    removed_episodes += await self._store.forget(addr)

            # 4. Remove from GraphMemory
            if self._gm.has_node(person_entity):
                await self._gm.delete_node(person_entity)

            return scrubbed_lines + removed_episodes

    def _scrub_spool_blocking(self) -> int:
        scrubbed = 0
        if not self._spool_dir.exists():
            return 0
        for spool_file in self._spool_dir.glob("*.jsonl"):
            if spool_file.name.startswith("."):
                continue
            lines: list[str] = []
            modified = False
            with open(spool_file, encoding="utf-8") as fh:
                for line in fh:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if self._is_record_forgotten(record):
                        scrubbed += 1
                        modified = True
                    else:
                        lines.append(raw)
            if modified:
                # Atomically overwrite spool file
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self._spool_dir, delete=False
                ) as tf:
                    tmp_path = Path(tf.name)
                    for line in lines:
                        tf.write(line + "\n")
                    tf.flush()
                    os.fsync(tf.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, spool_file)
                self._watermarks[spool_file.name] = spool_file.stat().st_size
        return scrubbed

    # ------------------------------------------------------------- state IO
    async def _load_watermarks(self) -> None:
        if self._watermark_path.exists():
            try:
                data = json.loads(self._watermark_path.read_text("utf-8"))
                self._watermarks = data.get("offsets", {})
            except Exception:
                self._watermarks = {}

    async def _save_watermarks(self) -> None:
        if not self._spool_dir.exists():
            return
        payload = json.dumps({"offsets": self._watermarks}, indent=2)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self._spool_dir, delete=False
        ) as tf:
            tmp_path = Path(tf.name)
            tf.write(payload)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_path, self._watermark_path)

    async def _load_forgotten(self) -> None:
        if self._forgotten_path.exists():
            try:
                data = json.loads(self._forgotten_path.read_text("utf-8"))
                self._forgotten = set(data.get("forgotten", []))
            except Exception:
                self._forgotten = set()

    async def _save_forgotten(self) -> None:
        if not self._spool_dir.exists():
            return
        payload = json.dumps({"forgotten": sorted(self._forgotten)}, indent=2)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self._spool_dir, delete=False
        ) as tf:
            tmp_path = Path(tf.name)
            tf.write(payload)
            tf.flush()
            os.fsync(tf.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self._forgotten_path)

    async def _load_threader_state(self) -> None:
        if self._threader_path.exists():
            try:
                data = json.loads(self._threader_path.read_text("utf-8"))
                now = self._clock.now() if self._clock else datetime.now(UTC)
                cutoff = now - timedelta(days=30)
                self._threader.load_dict(data, cutoff_date=cutoff)
            except Exception:
                _log.warning("Failed to load threader state from %s", self._threader_path)

    async def _save_threader_state(self) -> None:
        if not self._spool_dir.exists():
            return
        now = self._clock.now() if self._clock else datetime.now(UTC)
        self._threader.prune(now)
        payload = json.dumps(self._threader.to_dict(), indent=2)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self._spool_dir, delete=False
        ) as tf:
            tmp_path = Path(tf.name)
            tf.write(payload)
            tf.flush()
            os.fsync(tf.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self._threader_path)


# gen-ref: ecefd81c
