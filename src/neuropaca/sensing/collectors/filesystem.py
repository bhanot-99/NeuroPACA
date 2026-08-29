"""`FileSystemCollector` — batched file-change sensing via watchdog
(Architecture.md §4, D-7 B8).

Disabled unless `config.watch_paths` is non-empty. A `watchdog.Observer` thread
appends `{path, kind}` to a hard-bounded `deque` (paths only — never contents,
rules.md §6); `collect()` drains that deque into a snapshot on each poll.

The deque is guarded by a `threading.Lock`: it is touched only by watchdog
threads and the `to_thread` `collect()` worker, never the event loop, so the
asyncio-lock rule (rules.md §0.2, loop-resident code) does not apply. If the OS
refuses a watch (inotify limit) the collector self-disables and raises
`CollectorError`; `XMetricCollector` turns that into a `SYSTEM_ERROR` and keeps
the other collectors running.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Iterable
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
    def __init__(self, record: Any) -> None:
        self._record = record

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._record(str(event.src_path), str(event.event_type))


class FileSystemCollector(BaseCollector):
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
        self._lock = threading.Lock()
        self._recent: deque[dict[str, str]] = deque(maxlen=max(1, buffer_size))
        self._observer: Any = None
        if not self._watch_paths:
            self.is_enabled = False

    async def start(self) -> None:
        if not self._watch_paths:
            self.is_enabled = False
            return
        observer = Observer()
        handler = _ChangeHandler(self._record)
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
        with self._lock:
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

    # -- called on the watchdog thread ------------------------------------------
    def _record(self, path: str, kind: str) -> None:
        if any(fnmatch(path, pattern) for pattern in self._ignore_globs):
            return
        with self._lock:
            self._recent.append({"path": path, "kind": kind})
