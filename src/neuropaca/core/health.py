"""Health readouts (Architecture.md §12 — shapes are defined here, the blueprint
leaves them open).

`ModuleHealth` is what every `BaseModule.health()` returns; the contract is that
the call *never raises and never blocks* (Architecture.md §3.7), so it reads
cached counters only — no lock, no `await`.

`SystemHealth` is the orchestrator's aggregate: the module reports plus the L1
service counters and process RSS. Used by B9's `health_check()` and, from now, by
the B1 soak harness to prove RSS stays flat.
"""

from __future__ import annotations

import os
import resource
from dataclasses import dataclass, field
from datetime import datetime

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def current_rss_mb() -> float | None:
    """Current resident set size in MiB, or None if it can't be read.

    Reads Linux `/proc/self/statm` (field 2 = resident pages). Falls back to
    `resource.getrusage` peak RSS on non-Linux. Stdlib only — psutil is a
    spike-only dependency, never a daemon one.
    """
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            resident_pages = int(fh.read().split()[1])
        return resident_pages * _PAGE_SIZE / (1024 * 1024)
    except (OSError, IndexError, ValueError):
        pass
    try:
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports ru_maxrss in KiB, macOS in bytes; > 1 MiB-as-a-count => bytes.
        return max_rss / (1024 * 1024) if max_rss > 1 << 20 else max_rss / 1024
    except (OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class ModuleHealth:
    """One module's self-report. `ok=False` with a `detail` string on any problem."""

    name: str
    ok: bool
    detail: str = ""
    last_event_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SystemHealth:
    """The whole daemon at a glance. `ok` is the AND of every module plus the
    invariant checks the orchestrator runs."""

    ok: bool
    uptime_seconds: float
    modules: tuple[ModuleHealth, ...] = ()
    graph_nodes: int = 0
    graph_edges: int = 0
    graph_dirty: bool = False
    queue_depth: int = 0
    events_dropped: int = 0
    inference_loaded: bool = False
    rss_mb: float | None = None
    graph_schema_version: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> str:
        state = "ok" if self.ok else "DEGRADED"
        rss = f"{self.rss_mb:.0f}MiB" if self.rss_mb is not None else "rss?"
        return (
            f"[{state}] up {self.uptime_seconds:.0f}s · "
            f"{self.graph_nodes}n/{self.graph_edges}e"
            f"{' dirty' if self.graph_dirty else ''} · "
            f"q{self.queue_depth}"
            f"{f' drop{self.events_dropped}' if self.events_dropped else ''} · "
            f"{'model' if self.inference_loaded else 'no-model'} · {rss}"
        )
