"""`SystemMetricCollector` — CPU, RAM, disk, load, temperature via psutil
(Architecture.md §4).

`collect()` blocks for ~1 s inside `psutil.cpu_percent(interval=1)`; the module
runs it off the event loop (D-7 B3). The CPU reading also drives an
edge-triggered idle/activity signal in `XMetricCollector` — a stand-in for the
deferred `ActivityCollector` (D-7 A3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psutil

from neuropaca.sensing.base_collector import BaseCollector
from neuropaca.sensing.snapshot import MetricSnapshot

# CPU-load thresholds for the synthetic idle signal (D-7 A3). Hysteresis: enter
# idle below IDLE, leave idle only above ACTIVE, so it does not flap.
IDLE_CPU_PERCENT = 5.0
ACTIVE_CPU_PERCENT = 10.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SystemMetricCollector(BaseCollector):
    def __init__(self, poll_interval_seconds: float = 60.0, *, top_process_count: int = 5) -> None:
        super().__init__("system", poll_interval_seconds)
        self._primed = False
        self._top_n = max(0, top_process_count)

    def collect(self) -> MetricSnapshot:
        if not self._primed:
            psutil.cpu_percent(interval=None)  # prime — the next read is a delta, not 0.0
            _prime_process_cpu()
            self._primed = True
        cpu = psutil.cpu_percent(interval=1.0)
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        data: dict[str, Any] = {
            "cpu_percent": cpu,
            "mem_percent": vm.percent,
            "mem_available_mb": round(vm.available / (1024 * 1024), 1),
            "disk_percent": disk.percent,
            "disk_free_gb": round(disk.free / (1024**3), 2),
        }
        if self._top_n:
            data["top_processes"] = _top_processes_by_cpu(self._top_n)
        load = _load_avg_1m()
        if load is not None:
            data["load_avg_1m"] = load
        temp = _max_temp_c()
        if temp is not None:
            data["temp_c_max"] = temp
        return MetricSnapshot(
            collector_name=self.name, timestamp=_utcnow(), data=data, anomaly_score=0.0
        )


def _prime_process_cpu() -> None:
    # psutil.Process.cpu_percent() returns 0.0 on first read per process — prime
    # every process once so the collector's second poll onward has real deltas.
    for proc in psutil.process_iter():
        try:
            proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def _top_processes_by_cpu(limit: int) -> list[dict[str, Any]]:
    """Top-N processes by CPU. Names only — never cmdline/args (rules.md §6)."""
    rows: list[tuple[str, float]] = []
    for proc in psutil.process_iter(["name"]):
        try:
            rows.append((proc.info["name"] or "?", proc.cpu_percent(None)))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda r: r[1], reverse=True)
    return [{"name": n, "cpu_percent": round(c, 1)} for n, c in rows[:limit] if c > 0.0]


def _load_avg_1m() -> float | None:
    try:
        return round(float(psutil.getloadavg()[0]), 2)
    except (AttributeError, OSError):
        return None


def _max_temp_c() -> float | None:
    try:
        groups = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    readings = [float(s.current) for group in groups.values() for s in group if s.current]
    return round(max(readings), 1) if readings else None
