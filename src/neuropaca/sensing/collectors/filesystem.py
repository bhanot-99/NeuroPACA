"""`FileSystemCollector` — batched file-change sensing via watchdog
(Architecture.md §4, D-7 B4/B8).

Disabled unless `config.watch_paths` is non-empty. The `watchdog.Observer` runs
on its own thread; its callbacks are marshalled back to the event loop with
`loop.call_soon_threadsafe` (D-7 B4 — `EventBus.publish` and the change deque are
loop-only). `_record` then runs on the loop and appends `{path, kind}` to a
hard-bounded `deque` (paths only — never contents, rules.md §6). `collect()`
drains that deque on the loop each poll (`is_blocking = False`), so no lock is
needed anywhere.

If the OS refuses a watch (inotify limit) the collector self-disables and raises
`CollectorError`; `XMetricCollector` turns that into a `SYSTEM_ERROR` and keeps
the other collectors running.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from neuropaca.core.errors import CollectorError
from neuropaca.sensing.base_collector import BaseCollector
from neuropaca.sensing.snapshot import MetricSnapshot

_SNAPSHOT_PATH_CAP = 100


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _ChangeHandler(FileSystemEventHandler):
    """Runs on the watchdog thread. Does nothing but hand the event to the
    loop-side sink — never touches the deque or the bus directly."""

    def __init__(self, on_thread_event: Callable[[str, str], None]) -> None:
        self._on_thread_event = on_thread_event

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._on_thread_event(str(event.src_path), str(event.event_type))


class FileSystemCollector(BaseCollector):
    is_blocking = False  # collect() is a loop-safe deque drain (D-7 B4)

    def __init__(
        self,
        *,
        watch_paths: Iterable[str],
        ignore_globs: Iterable[str],
        buffer_size: int,
        poll_interval_seconds: float = 60.0,
    ) -> None:
        super().__init__("filesystem", poll_interval_seconds)
        self._watch_paths = [Path(p).expanduser() for p in watch_paths]
        self._ignore_globs = list(ignore_globs)
        self._recent: deque[dict[str, str]] = deque(maxlen=max(1, buffer_size))
        self._observer: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        if not self._watch_paths:
            self.is_enabled = False

    async def start(self) -> None:
        if not self._watch_paths:
            self.is_enabled = False
            return
        self._loop = asyncio.get_running_loop()
        observer = Observer()
        handler = _ChangeHandler(self._on_thread_event)
        try:
            scheduled = 0
            for path in self._watch_paths:
                if path.is_dir():
                    observer.schedule(handler, str(path), recursive=True)
                    scheduled += 1
            if scheduled == 0:
                self.is_enabled = False
                return
            observer.start()
        except OSError as exc:  # inotify watch limit, permissions, … (D-7 B8)
            self.is_enabled = False
            raise CollectorError(f"filesystem watch setup failed: {exc}") from exc
        self._observer = observer

    async def stop(self) -> None:
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            await asyncio.to_thread(observer.join, 5.0)

    def collect(self) -> MetricSnapshot:
        # Runs on the event loop (is_blocking=False) — the deque is loop-owned.
        changes = list(self._recent)
        self._recent.clear()
        kinds: dict[str, int] = {}
        for change in changes:
            kinds[change["kind"]] = kinds.get(change["kind"], 0) + 1
        data: dict[str, Any] = {
            "change_count": len(changes),
            "changed_paths": [c["path"] for c in changes[-_SNAPSHOT_PATH_CAP:]],
            "kinds": kinds,
        }
        return MetricSnapshot(
            collector_name=self.name, timestamp=_utcnow(), data=data, anomaly_score=0.0
        )

    # -- watchdog thread ------------------------------------------------------
    def _on_thread_event(self, path: str, kind: str) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._record, path, kind)

    # -- event loop ---------------------------------------------------------
    def _record(self, path: str, kind: str) -> None:
        if any(fnmatch(path, pattern) for pattern in self._ignore_globs):
            return
        self._recent.append({"path": path, "kind": kind})
