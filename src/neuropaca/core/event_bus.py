"""L1 · `EventBus` — the only channel between modules (Architecture.md §3.1, §13).

Invariants (rules.md §0, §2):
- singleton; modules hold a reference, never subclass it
- `publish()` is fire-and-forget and never blocks — a full bounded queue drops
  the event and logs, it does not back-pressure the publisher
- a subscriber that raises is isolated: siblings still run, and a `SYSTEM_ERROR`
  event is published naming the handler — unless the event being dispatched is
  itself a `SYSTEM_ERROR`, in which case the failure is logged only (no recursion)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import ClassVar

from neuropaca.core.enums import EventType
from neuropaca.core.models import Event, system_error_event

_log = logging.getLogger(__name__)

# Bounded so a wedged dispatch loop can never exhaust memory. Module-level so a
# test can shrink it; read once when the bus is constructed.
_QUEUE_MAXSIZE = 1000

Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    """Async publish/subscribe over a bounded queue."""

    _instance: ClassVar[EventBus | None] = None

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers: dict[EventType, list[Subscriber]] = {}
        self._dispatch_task: asyncio.Task[None] | None = None
        self._running = False
        self._dropped_count = 0

    # ------------------------------------------------------------------ singleton
    @classmethod
    def get_instance(cls) -> EventBus:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def _reset_for_tests(cls) -> None:
        inst = cls._instance
        if inst is not None and inst._dispatch_task is not None:
            inst._dispatch_task.cancel()
        cls._instance = None

    # ---------------------------------------------------------------- properties
    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def is_running(self) -> bool:
        return self._running

    # ---------------------------------------------------------------- subscribe
    def subscribe(self, event_type: EventType, callback: Subscriber) -> None:
        subs = self._subscribers.setdefault(event_type, [])
        if callback not in subs:
            subs.append(callback)

    def unsubscribe(self, event_type: EventType, callback: Subscriber) -> None:
        subs = self._subscribers.get(event_type)
        if subs and callback in subs:
            subs.remove(callback)

    # ------------------------------------------------------------------ publish
    def publish(self, event: Event) -> None:
        """Enqueue and return. Never blocks, never raises (rules.md §2)."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_count += 1
            _log.error(
                "SYSTEM_ERROR: EventBus queue full (maxsize=%d) — dropped %s from %r "
                "(%d dropped in total)",
                self._queue.maxsize,
                event.event_type,
                event.source,
                self._dropped_count,
            )

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        await self._queue.join()  # deliver what is already queued
        task, self._dispatch_task = self._dispatch_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def join(self) -> None:
        """Block until every queued event has been dispatched."""
        await self._queue.join()

    # ------------------------------------------------------------------ internal
    async def _dispatch_loop(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._dispatch_one(event)
            finally:
                self._queue.task_done()

    async def _dispatch_one(self, event: Event) -> None:
        for callback in list(self._subscribers.get(event.event_type, ())):
            try:
                await callback(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Subscriber isolation (rules.md §2): one handler's failure must
                # not stop its siblings or the dispatch loop.
                self._on_subscriber_error(event, callback, exc)

    def _on_subscriber_error(self, event: Event, callback: Subscriber, exc: Exception) -> None:
        name = getattr(callback, "__qualname__", None) or repr(callback)
        if event.event_type is EventType.SYSTEM_ERROR:
            # Publishing another SYSTEM_ERROR here would loop forever.
            _log.error("SYSTEM_ERROR handler %s itself failed: %r (not re-published)", name, exc)
            return
        _log.error("subscriber %s failed handling %s: %r", name, event.event_type, exc)
        self.publish(system_error_event(module=name, exception=str(exc), severity="handler"))
