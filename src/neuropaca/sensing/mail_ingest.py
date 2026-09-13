# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S1/S4 · `MailIngest` & `MailPlugin` — correspondence ledger and thread state.

Migrated under S4 Plugin Contract:
- `MailPlugin` implements the pure `Plugin` reader protocol over the mail spool.
- `MailIngest` hosts `MailPlugin` via `PluginHost`.

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
from neuropaca.sensing.plugin_host import (
    PluginDescriptor,
    PluginHost,
    PluginItem,
    PluginManifest,
)

_log = logging.getLogger(__name__)

__all__ = ["MailIngest", "MailPlugin"]


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


class MailPlugin:
    """Pure plugin reader tailing mail spool files and resolving threads."""

    def __init__(
        self,
        config: Config,
        clock: Clock | None = None,
        store: EpisodeStore | None = None,
    ) -> None:
        self.config = config
        self._clock: Clock = clock or SystemClock()
        self._store = store
        self._spool_dir = Path(config.mail_spool_dir)
        self._watermark_path = self._spool_dir / ".watermark.json"
        self._forgotten_path = self._spool_dir / "forgotten.json"
        self._threader_path = self._spool_dir / ".threader_state.json"
        self._threader = Threader()
        self._watermarks: dict[str, int] = {}
        self._forgotten: set[str] = set()
        self._ingested_count = 0
        self._last_batch_processed = 0
        self._entities: set[str] = set()

    @property
    def spool_dir(self) -> Path:
        return self._spool_dir

    @property
    def ingested_count(self) -> int:
        return self._ingested_count

    async def initialize(self) -> None:
        self._spool_dir.mkdir(parents=True, exist_ok=True)
        await self._load_watermarks()
        await self._load_forgotten()
        await self._load_threader_state()

    def describe(self) -> PluginDescriptor:
        spool_p = self._spool_dir.expanduser().resolve()
        return PluginDescriptor(
            name="mail",
            node_type=NodeType.THREAD,
            domain_hub="domain:comms",
            poll_interval_seconds=self.config.mail_poll_interval_seconds,
            span_kind=EpisodeKind.MESSAGE_RECEIVED,
            manifest=PluginManifest(
                allowed_read_paths=(spool_p,),
                allowed_write_paths=(spool_p,),
                allow_network=False,
                allow_subprocesses=False,
            ),
            entity_prefixes=("person:", "thread:"),
        )

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

    def _item_from_record(self, record: dict[str, Any]) -> PluginItem:
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
        self._entities.add(thread_entity)
        self._entities.add(person_entity)

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

        thread_label = (
            subject_raw if (self.config.mail_retain_subject and subject_raw) else thread_entity
        )
        fact_attrs = {
            "participant": p_slug,
            "last_message_id": msg_id,
            "valid_from": date,
        }

        return PluginItem(
            entity_id=thread_entity,
            label=thread_label,
            node_type=NodeType.THREAD,
            span=(date, date),
            span_kind=kind,
            span_obj=person_entity,
            span_attrs=attrs,
            fact=(EpisodeKind.THREAD_STATE_FACT, thread_state, fact_attrs),
            state_key=(thread_entity, thread_state, msg_id),
            extra_nodes=((person_entity, p_name, NodeType.PERSON),),
            edges=(
                (thread_entity, person_entity, RelationType.RELATED_TO),
                (person_entity, "domain:comms", RelationType.PART_OF),
            ),
        )

    async def items(self, since: datetime) -> list[PluginItem]:
        await self._load_forgotten()
        items: list[PluginItem] = []
        if not self._spool_dir.exists():
            self._last_batch_processed = 0
            return items

        spool_files = sorted(self._spool_dir.glob("*.jsonl"))
        total_processed = 0

        for spool_file in spool_files:
            if spool_file.name.startswith("."):
                continue
            records, new_offset = await asyncio.to_thread(
                self._read_new_records_blocking, spool_file
            )
            for record in records:
                items.append(self._item_from_record(record))
                total_processed += 1
            self._watermarks[spool_file.name] = new_offset

        if total_processed > 0:
            await self._save_watermarks()
            await self._save_threader_state()

        self._ingested_count += total_processed
        self._last_batch_processed = total_processed

        # Check resolved threads and yield items for any timed-out threads
        resolved_items = await self._check_resolved_thread_items()
        items.extend(resolved_items)

        return items

    async def _check_resolved_thread_items(self) -> list[PluginItem]:
        if self._store is None:
            return []
        now = self._clock.now() if self._clock else datetime.now(UTC)
        cutoff = now - timedelta(days=self.config.mail_resolved_after_days)
        open_records = await self._store.at(now)
        items: list[PluginItem] = []
        for record in open_records:
            if (
                record.kind == str(EpisodeKind.THREAD_STATE_FACT)
                and record.object in ("awaiting_them", "awaiting_you")
                and record.t_valid is not None
                and record.t_valid < cutoff
            ):
                valid_from = record.t_valid + timedelta(days=self.config.mail_resolved_after_days)
                items.append(
                    PluginItem(
                        entity_id=record.subject,
                        label=record.subject,
                        node_type=NodeType.THREAD,
                        fact=(
                            EpisodeKind.THREAD_STATE_FACT,
                            "resolved",
                            {"resolved_reason": "timeout", "valid_from": valid_from},
                        ),
                        state_key=(record.subject, "resolved"),
                    )
                )
        return items

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

    @property
    def watermarks(self) -> dict[str, int]:
        return dict(self._watermarks)

    @property
    def forgotten(self) -> set[str]:
        return set(self._forgotten)

    @property
    def threader(self) -> Threader:
        return self._threader

    @property
    def last_batch_processed(self) -> int:
        return self._last_batch_processed

    def entities(self) -> frozenset[str]:
        return frozenset(self._entities)

    def owns_entity(self, entity: str) -> bool:
        if entity in self._entities:
            return True
        if entity.startswith(("person:", "thread:")):
            return True
        if "@" in entity:
            return True
        return False

    async def forget(self, person: str) -> int:
        if not self.owns_entity(person):
            return 0
        raw = (
            person.removeprefix("person:").strip()
            if person.startswith("person:")
            else person.strip()
        )
        name, addr = normalize_email_address(raw)
        slug = person_slug(name or raw, addr)
        person_entity = f"person:{slug}"

        self._forgotten.add(person_entity)
        self._forgotten.add(slug)
        if addr:
            self._forgotten.add(addr)
        if person:
            self._forgotten.add(person.strip().lower())
        if raw:
            self._forgotten.add(raw.lower())
        await self._save_forgotten()

        self._entities.discard(person_entity)
        self._entities.discard(person)

        return await asyncio.to_thread(self._scrub_spool_blocking)

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

    async def save_watermarks(self) -> None:
        await self._save_watermarks()

    async def save_threader_state(self) -> None:
        await self._save_threader_state()


class MailIngest(BaseModule):
    """Daemon-side correspondence sensor reading from the external spool.

    Hosts MailPlugin within PluginHost under S4 plugin contract.
    """

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
        self._lock = asyncio.Lock()

        self._plugin = MailPlugin(config, clock=self._clock, store=episode_store)
        self._host = PluginHost(
            event_bus,
            config,
            graph_memory,
            episode_store,
            plugins=[self._plugin],
            clock=self._clock,
        )

    @property
    def spool_dir(self) -> Path:
        return self._plugin.spool_dir

    @property
    def ingested_count(self) -> int:
        return self._plugin.ingested_count

    @property
    def _watermarks(self) -> dict[str, int]:
        return self._plugin.watermarks

    @property
    def _forgotten(self) -> set[str]:
        return self._plugin.forgotten

    @property
    def _threader(self) -> Threader:
        return self._plugin.threader

    async def initialize(self) -> None:
        await self._plugin.initialize()
        await self._host.initialize()

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        await self.ingest_spool()
        await self._host.start()

    async def stop(self) -> None:
        self.is_running = False
        await self._host.stop()
        await self._plugin.save_watermarks()
        await self._plugin.save_threader_state()

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=True,
            detail=(
                f"messages_ingested={self.ingested_count}, "
                f"tracked_files={len(self._watermarks)}, "
                f"forgotten_count={len(self._forgotten)}"
            ),
        )

    async def ingest_spool(self) -> int:
        """Tail all *.jsonl files in spool_dir from their saved watermark offsets."""
        async with self._lock:
            await self._host.poll_tick("mail")
            if self._plugin.last_batch_processed > 0:
                await self.check_resolved_threads()
            return self._plugin.last_batch_processed

    async def poll_tick(self) -> int:
        """Run a single polling cycle: ingest pending records and resolve timed-out threads."""
        processed = await self.ingest_spool()
        await self.check_resolved_threads()
        return processed

    async def check_resolved_threads(self) -> int:
        """Close open threads that have passed mail_resolved_after_days."""
        return await self._plugin.check_resolved_threads()

    _check_resolved_threads = check_resolved_threads

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

            scrubbed_lines = await self._plugin.forget(person)
            removed_episodes = await self._host.forget(person_entity)
            if addr and self._store is not None:
                removed_episodes += await self._store.forget(addr)
            return scrubbed_lines + removed_episodes
