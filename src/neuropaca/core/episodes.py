# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · `EpisodeStore` — the bi-temporal event log (VISION_PHASES.md §3.3).

The graph (`GraphMemory`) answers "what goes with what"; this answers "what
happened, and when was it true". `sqlite3` in WAL mode (stdlib — no new
dependency, rules.md §9): the spike (VISION_PHASES.md, S0) measured JSONL's
append throughput as fine but its "what was I doing Tuesday 15:00" query as an
O(file) scan, where a `(subject, t_start)` index answers the same query in
O(log n).

Every row is either a **span** (`t_start`/`t_end` set — "this happened, for
this long") or a **fact** (`t_valid`/`t_invalid` set — "this was true, until
it wasn't") — never both. `record_span` writes the first kind; `assert_fact`
the second, and **closes** any contradicting open fact (`t_invalid = now`)
rather than deleting it, so `at(t)` can still answer "what was true then" for
a fact that has since been superseded.

Shape, mirroring `EventBus` deliberately (Architecture.md §3.1):
- `record_span` / `assert_fact` are synchronous, fire-and-forget, non-blocking
  `put_nowait` onto a bounded queue — called from inside a bus subscriber's
  handler, which must never block on disk I/O (rules.md §3's < 5 ms budget).
- one writer task drains the queue, batching whatever is already waiting into
  one transaction per `asyncio.to_thread` call, so a 20k-event storm costs a
  handful of commits, not thousands.
- `at` / `between` / `since` are read-only queries, each opening its own
  short-lived connection off the loop (`asyncio.to_thread`) — WAL mode lets
  those overlap the writer without either blocking the other.
- `episode_seq` is the table's `INTEGER PRIMARY KEY AUTOINCREMENT` — monotonic
  and gap-free by construction, no separate counter to keep in sync.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from neuropaca.core.enums import EpisodeKind
from neuropaca.core.errors import EpisodeStoreError

_log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_QUEUE_MAXSIZE = 20_000  # the 20k-switch storm the spike sizes against
_DEFAULT_BATCH_SIZE = 500
_DRAIN_TIMEOUT_SECONDS = 5.0

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS episode (
    episode_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    subject     TEXT NOT NULL,
    object      TEXT,
    t_start     TEXT,
    t_end       TEXT,
    t_valid     TEXT,
    t_invalid   TEXT,
    t_seen      TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    attrs_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_episode_subject_start ON episode(subject, t_start);
CREATE INDEX IF NOT EXISTS idx_episode_kind_start ON episode(kind, t_start);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def episodes_schema_version() -> int:
    """The on-disk episode-store schema version this build writes."""
    return _SCHEMA_VERSION


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise EpisodeStoreError(f"naive datetime not allowed: {value!r}")
    return value.isoformat()


def _iso_opt(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_dt_opt(value: str | None) -> datetime | None:
    return None if value is None else _parse_dt(value)


@dataclass(frozen=True, slots=True)
class EpisodeRecord:
    """One row of the episode log, as read back."""

    episode_seq: int
    id: str
    kind: str
    subject: str
    object: str | None
    t_start: datetime | None
    t_end: datetime | None
    t_valid: datetime | None
    t_invalid: datetime | None
    t_seen: datetime
    source: str
    attrs: dict[str, Any]

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> EpisodeRecord:
        return cls(
            episode_seq=int(row["episode_seq"]),
            id=str(row["id"]),
            kind=str(row["kind"]),
            subject=str(row["subject"]),
            object=row["object"],
            t_start=_parse_dt_opt(row["t_start"]),
            t_end=_parse_dt_opt(row["t_end"]),
            t_valid=_parse_dt_opt(row["t_valid"]),
            t_invalid=_parse_dt_opt(row["t_invalid"]),
            t_seen=_parse_dt(row["t_seen"]),
            source=str(row["source"]),
            attrs=json.loads(row["attrs_json"]) if row["attrs_json"] else {},
        )


# A queued write job: (kind, payload, the future its caller — if any — awaits).
_WriteOp = Literal["span", "fact", "forget"]
_Job = tuple[_WriteOp, dict[str, Any], "asyncio.Future[Any] | None"]


class EpisodeStore:
    """The durable bi-temporal log beside the graph. One per daemon process —
    held, not a singleton (`Orchestrator` constructs and passes it, like
    `GraphMemory`), so tests can point many instances at many `tmp_path`s."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        queue_maxsize: int = _QUEUE_MAXSIZE,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._path = Path(db_path)
        self._batch_size = batch_size
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=queue_maxsize)
        self._writer_task: asyncio.Task[None] | None = None
        self._running = False
        self._dropped_count = 0
        self._written_count = 0

    # ---------------------------------------------------------------- properties
    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def written_count(self) -> int:
        return self._written_count

    @property
    def is_running(self) -> bool:
        return self._running

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._running:
            return
        await asyncio.to_thread(self._init_schema_blocking)
        self._running = True
        self._writer_task = asyncio.create_task(self._writer_loop())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        await self.flush()
        task, self._writer_task = self._writer_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def flush(self) -> bool:
        """Wait for every queued write to land. Bounded like `EventBus.join()`
        (core/event_bus.py) — an undrained queue is reported, not waited out
        forever, so a caller on the shutdown path never hangs on this."""
        if self._queue.empty():
            return True
        task = self._writer_task
        if task is None or task.done():
            _log.error(
                "EpisodeStore drain skipped: no live writer task, %d job(s) undelivered",
                self._queue.qsize(),
            )
            return False
        try:
            async with asyncio.timeout(_DRAIN_TIMEOUT_SECONDS):
                await self._queue.join()
        except TimeoutError:
            _log.error(
                "EpisodeStore did not drain within %.1fs — %d job(s) still queued",
                _DRAIN_TIMEOUT_SECONDS,
                self._queue.qsize(),
            )
            return False
        return True

    # ------------------------------------------------------------------ writes
    def record_span(
        self,
        kind: EpisodeKind,
        subject: str,
        start: datetime,
        end: datetime,
        *,
        obj: str | None = None,
        source: str = "",
        attrs: dict[str, Any] | None = None,
    ) -> None:
        """One completed episode: "this happened, from `start` to `end`".
        Fire-and-forget — never blocks, never raises (same contract as
        `EventBus.publish`). The caller (the S0 writer module) owns tracking
        which span is still open; this only records a finished one."""
        self._enqueue(
            "span",
            {
                "kind": str(kind),
                "subject": subject,
                "object": obj,
                "t_start": start,
                "t_end": end,
                "source": source,
                "attrs": attrs or {},
            },
        )

    def assert_fact(
        self,
        kind: EpisodeKind,
        subject: str,
        obj: str,
        *,
        valid_from: datetime,
        source: str = "",
        attrs: dict[str, Any] | None = None,
    ) -> None:
        """ "This is now true": close any open (`t_invalid IS NULL`) fact of the
        same `(kind, subject)` at `valid_from`, then insert the new one, open-
        ended. The old interval is never deleted — `at(t)` before `valid_from`
        still sees it (§3.3)."""
        self._enqueue(
            "fact",
            {
                "kind": str(kind),
                "subject": subject,
                "object": obj,
                "t_valid": valid_from,
                "source": source,
                "attrs": attrs or {},
            },
        )

    def _enqueue(self, op: _WriteOp, payload: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((op, payload, None))
        except asyncio.QueueFull:
            self._dropped_count += 1
            _log.error(
                "EpisodeStore queue full (maxsize=%d) — dropped a %s write (%d dropped in total)",
                self._queue.maxsize,
                op,
                self._dropped_count,
            )

    async def forget(self, entity: str) -> int:
        """Remove every episode naming `entity` as subject or object
        (VISION_PHASES.md exit: "`forget <app>` removes its episodes"). Flushes
        first so nothing already queued survives under the entity's name, then
        deletes directly — a rare, destructive, immediate operation, not one
        that belongs in the ordinary batched path."""
        await self.flush()
        return await asyncio.to_thread(self._forget_blocking, entity)

    # ------------------------------------------------------------------ reads
    async def at(self, when: datetime) -> list[EpisodeRecord]:
        """Every span covering `when`, and every fact valid at `when`."""
        return await asyncio.to_thread(self._at_blocking, when)

    async def between(self, t0: datetime, t1: datetime) -> list[EpisodeRecord]:
        """Every span or fact whose interval overlaps `[t0, t1)`."""
        return await asyncio.to_thread(self._between_blocking, t0, t1)

    async def since(self, episode_seq: int) -> list[EpisodeRecord]:
        """Every row past `episode_seq`, ascending — the watermark-catch-up read
        (VISION_PHASES.md: "GraphMemory persists `last_episode_seq`; every tick,
        `since(last_episode_seq)` replays any missed rows")."""
        return await asyncio.to_thread(self._since_blocking, episode_seq)

    # ------------------------------------------------------------ writer task
    async def _writer_loop(self) -> None:
        while True:
            job = await self._queue.get()
            batch = [job]
            while len(batch) < self._batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await asyncio.to_thread(self._write_batch_blocking, batch)
                self._written_count += len(batch)
            except Exception:
                _log.exception("EpisodeStore writer batch of %d failed", len(batch))
            finally:
                for _ in batch:
                    self._queue.task_done()

    # -------------------------------------------------------- blocking workers
    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema_blocking(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA_SQL)
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            else:
                on_disk = int(row["value"])
                if on_disk > _SCHEMA_VERSION:
                    raise EpisodeStoreError(
                        f"episode store {self._path} is schema v{on_disk}, this build reads "
                        f"up to v{_SCHEMA_VERSION} — upgrade NeuroPACA"
                    )
            conn.commit()
        finally:
            conn.close()

    def _write_batch_blocking(self, batch: list[_Job]) -> None:
        conn = self._connect()
        try:
            for op, payload, _fut in batch:
                if op == "span":
                    self._insert_span(conn, payload)
                elif op == "fact":
                    self._assert_fact(conn, payload)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _insert_span(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO episode (id, kind, subject, object, t_start, t_end, t_seen, "
            "source, attrs_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id(),
                payload["kind"],
                payload["subject"],
                payload["object"],
                _iso(payload["t_start"]),
                _iso(payload["t_end"]),
                _iso(_utcnow()),
                payload["source"],
                json.dumps(payload["attrs"]),
            ),
        )

    @staticmethod
    def _assert_fact(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        valid_from: datetime = payload["t_valid"]
        conn.execute(
            "UPDATE episode SET t_invalid = ? WHERE kind = ? AND subject = ? "
            "AND t_valid IS NOT NULL AND t_invalid IS NULL",
            (_iso(valid_from), payload["kind"], payload["subject"]),
        )
        conn.execute(
            "INSERT INTO episode (id, kind, subject, object, t_valid, t_invalid, t_seen, "
            "source, attrs_json) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                _new_id(),
                payload["kind"],
                payload["subject"],
                payload["object"],
                _iso(valid_from),
                _iso(_utcnow()),
                payload["source"],
                json.dumps(payload["attrs"]),
            ),
        )

    def _forget_blocking(self, entity: str) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM episode WHERE subject = ? OR object = ?", (entity, entity)
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def _at_blocking(self, when: datetime) -> list[EpisodeRecord]:
        conn = self._connect()
        try:
            iso = _iso(when)
            rows = conn.execute(
                "SELECT * FROM episode WHERE "
                "(t_start IS NOT NULL AND t_start <= ? AND t_end >= ?) OR "
                "(t_valid IS NOT NULL AND t_valid <= ? AND (t_invalid IS NULL OR t_invalid > ?)) "
                "ORDER BY episode_seq ASC",
                (iso, iso, iso, iso),
            ).fetchall()
            return [EpisodeRecord._from_row(r) for r in rows]
        finally:
            conn.close()

    def _between_blocking(self, t0: datetime, t1: datetime) -> list[EpisodeRecord]:
        conn = self._connect()
        try:
            iso0, iso1 = _iso(t0), _iso(t1)
            rows = conn.execute(
                "SELECT * FROM episode WHERE "
                "(t_start IS NOT NULL AND t_start < ? AND t_end > ?) OR "
                "(t_valid IS NOT NULL AND t_valid < ? AND (t_invalid IS NULL OR t_invalid > ?)) "
                "ORDER BY episode_seq ASC",
                (iso1, iso0, iso1, iso0),
            ).fetchall()
            return [EpisodeRecord._from_row(r) for r in rows]
        finally:
            conn.close()

    def _since_blocking(self, episode_seq: int) -> list[EpisodeRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM episode WHERE episode_seq > ? ORDER BY episode_seq ASC",
                (episode_seq,),
            ).fetchall()
            return [EpisodeRecord._from_row(r) for r in rows]
        finally:
            conn.close()


def _new_id() -> str:
    from uuid import uuid4

    return f"episode:{uuid4().hex}"


# gen-ref: 4b2f0a1e
