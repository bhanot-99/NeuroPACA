"""`RawMetricsRecorder` — an append-only CSV of every collector reading (B13,
operator request).

Not part of the cognitive loop: a passive `METRIC_COLLECTED` subscriber that
writes one flat row per reading so the raw sensed data can be eyeballed or loaded
into a dataframe during the soak. A `system` reading is one wide row; a `process`
census is one row per censused app; anything else lands with its `data` dict in
the `detail` column as compact JSON.

Enabled only when `config.raw_metrics_csv_path` is set. Append-only, **no
rotation** — soak / dogfood use, point it at a path you will sweep. The file
write is offloaded with `asyncio.to_thread`, and a write failure is swallowed
after one `SYSTEM_ERROR` (rules.md §2) — a diagnostic log must never take the
daemon down.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from neuropaca.core.base_module import BaseModule
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.sensing.snapshot import MetricSnapshot

_log = logging.getLogger(__name__)

_HEADER = (
    "timestamp",
    "collector",
    "cpu_percent",
    "mem_percent",
    "mem_available_mb",
    "disk_percent",
    "disk_free_gb",
    "load_avg_1m",
    "temp_c_max",
    "app_name",
    "app_rss_mb",
    "app_cpu_percent",
    "app_proc_count",
    "app_running_seconds",
    "detail",
)
_SYSTEM_KEYS = (
    "cpu_percent",
    "mem_percent",
    "mem_available_mb",
    "disk_percent",
    "disk_free_gb",
    "load_avg_1m",
    "temp_c_max",
)


class RawMetricsRecorder(BaseModule):
    def __init__(self, event_bus: EventBus, config: Config) -> None:
        super().__init__("raw_recorder", event_bus, config)
        self._path = Path(config.raw_metrics_csv_path)
        self._rows_written = 0
        self._errored = False
        self._last_at: datetime | None = None

    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.METRIC_COLLECTED, self.on_metric)
        try:
            await asyncio.to_thread(self._ensure_header)
        except OSError as exc:
            self._errored = True
            _log.warning("raw_recorder cannot open %s: %r", self._path, exc)
            self.event_bus.publish(
                system_error_event(module=self.name, exception=str(exc), severity="init")
            )

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.METRIC_COLLECTED, self.on_metric)

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=not self._errored,
            detail=f"{self._rows_written} rows -> {self._path.name}",
            last_event_at=self._last_at,
        )

    # ------------------------------------------------------------------ handler
    async def on_metric(self, event: Event) -> None:
        try:
            snapshot = event.payload.get("snapshot")
            if not isinstance(snapshot, MetricSnapshot):
                return
            rows = self._rows_for(snapshot)
            if rows:
                await asyncio.to_thread(self._append, rows)
                self._rows_written += len(rows)
                self._last_at = snapshot.timestamp
        except Exception as exc:  # a handler never raises (rules.md §2)
            if not self._errored:
                self._errored = True
                _log.exception("raw_recorder append failed")
                self.event_bus.publish(
                    system_error_event(module=self.name, exception=str(exc), severity="handler")
                )

    # ------------------------------------------------------------------ rows
    def _rows_for(self, snapshot: MetricSnapshot) -> list[list[str]]:
        ts = snapshot.timestamp.isoformat()
        name = snapshot.collector_name
        data = snapshot.data

        if name == "system":
            row = [ts, name] + [_fmt(data.get(k)) for k in _SYSTEM_KEYS] + ["", "", "", "", "", ""]
            return [row]

        if name == "process":
            procs = data.get("processes")
            procs = procs if isinstance(procs, list) else []
            if not procs:
                return [[ts, name, "", "", "", "", "", "", "", "", "", "", "", "", "0 groups"]]
            rows: list[list[str]] = []
            for p in procs:
                p = p if isinstance(p, dict) else {}
                rows.append(
                    [
                        ts,
                        name,
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        str(p.get("name", "")),
                        _fmt(p.get("rss_mb")),
                        _fmt(p.get("cpu_percent")),
                        _fmt(p.get("proc_count")),
                        _fmt(p.get("running_seconds")),
                        "",
                    ]
                )
            return rows

        # activity / filesystem / anything else — keep the raw dict in `detail`
        return [[ts, name] + [""] * 12 + [_compact_json(data)]]

    # ------------------------------------------------------------------ file io
    def _ensure_header(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists() and self._path.stat().st_size > 0:
            return
        with self._path.open("w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerow(_HEADER)

    def _append(self, rows: list[list[str]]) -> None:
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        with self._path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(round(value, 3))
    return str(value)


def _compact_json(data: dict[str, Any]) -> str:
    try:
        return json.dumps(data, default=str, separators=(",", ":"))[:2000]
    except (TypeError, ValueError):
        return ""
