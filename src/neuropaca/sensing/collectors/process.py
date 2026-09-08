# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""`ProcessCollector` — a per-process RAM / CPU / runtime census, grouped by app
(Architecture.md §4, B13-B2, D-19).

CPU is the wrong primary signal for "what is this person doing": it is bursty and
mostly ~0. **RAM footprint is the stable indicator** of what is loaded and being
worked with — browser tab-sets, IDE projects, VMs, containers, media tools. This
collector adds memory as a first-class sensed dimension, per app, with **how long
the app has been running** as the third axis.

`collect()` blocks (`process_iter` + memory reads cost tens of ms); the module
always runs it via `asyncio.to_thread` (`is_blocking = True`, D-7 B3).

Grouping is by process `name` (D-19(f)): a 15-process browser collapses to one
`brave` row whose `rss_mb` is the summed RSS across those processes. Summed RSS
double-counts shared libraries (D-19(b)) — accepted for B13 as fast and
loop-safe; PSS via `/proc/<pid>/smaps_rollup` is the documented follow-up.

Privacy (`rules.md §6`): **process names only**. Never `cmdline`, `exe`,
`environ`, `open_files`, `connections`. `test_never_reads_cmdline_or_environ`
greps this module and fails if any of those tokens appear.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import psutil

from neuropaca.sensing.base_collector import BaseCollector
from neuropaca.sensing.snapshot import MetricSnapshot

# psutil attributes read per process. Names only — extending this list is a
# privacy review block (rules.md §6).
_PROC_ATTRS = ("name", "memory_info", "cpu_percent", "create_time")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _Group:
    __slots__ = ("cpu_percent", "oldest_start", "proc_count", "rss_bytes")

    def __init__(self) -> None:
        self.rss_bytes = 0.0
        self.cpu_percent = 0.0
        self.oldest_start = float("inf")
        self.proc_count = 0

    def add(self, rss: float, cpu: float, start: float) -> None:
        self.rss_bytes += rss
        self.cpu_percent += cpu
        self.oldest_start = min(self.oldest_start, start)
        self.proc_count += 1


class ProcessCollector(BaseCollector):
    is_blocking = True

    def __init__(
        self,
        poll_interval_seconds: float = 60.0,
        *,
        min_rss_mb: float = 200.0,
        exclude_names: Iterable[str] = (),
    ) -> None:
        super().__init__("process", poll_interval_seconds)
        self._min_rss_mb = max(0.0, min_rss_mb)
        self._exclude = {n.strip().lower() for n in exclude_names if n.strip()}
        self._primed = False

    def collect(self) -> MetricSnapshot:
        if not self._primed:
            self._prime_cpu()
            self._primed = True

        now = _utcnow()
        now_epoch = now.timestamp()
        groups: dict[str, _Group] = {}

        for proc in psutil.process_iter(_PROC_ATTRS):
            try:
                info = proc.info
                name = (info.get("name") or "?").strip() or "?"
                if name.lower() in self._exclude:
                    continue
                mem = info.get("memory_info")
                rss = float(getattr(mem, "rss", 0.0)) if mem is not None else 0.0
                cpu = float(info.get("cpu_percent") or 0.0)
                start = float(info.get("create_time") or now_epoch)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue  # per-process failure is best-effort — keep censusing
            groups.setdefault(name, _Group()).add(rss, cpu, start)

        rows: list[dict[str, Any]] = []
        for name, g in groups.items():
            rss_mb = g.rss_bytes / (1024 * 1024)
            if rss_mb < self._min_rss_mb:
                continue
            oldest = g.oldest_start if g.oldest_start != float("inf") else now_epoch
            rows.append(
                {
                    "name": name,
                    "rss_mb": round(rss_mb, 1),
                    "cpu_percent": round(g.cpu_percent, 1),
                    "proc_count": g.proc_count,
                    "running_seconds": round(max(0.0, now_epoch - oldest), 1),
                }
            )

        # RAM is the primary sort key (D-19) — heaviest app first.
        rows.sort(key=lambda r: r["rss_mb"], reverse=True)
        return MetricSnapshot(
            collector_name=self.name,
            timestamp=now,
            data={"processes": rows, "group_count": len(rows)},
            anomaly_score=0.0,
        )

    @staticmethod
    def _prime_cpu() -> None:
        # psutil.Process.cpu_percent() returns 0.0 on its first read per process;
        # prime every process once so the second poll onward has real deltas.
        for proc in psutil.process_iter():
            try:
                proc.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue


# gen-ref: 6a3356ce
