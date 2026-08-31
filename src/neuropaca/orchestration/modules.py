"""`build_modules()` — construct the L2-L9 modules for the phases built so far,
in dependency order (D-7 B6).

The orchestrator calls this in `initialize()` and drives the returned list
through `initialize -> start -> stop`; list order is start order
(L2 -> L3 -> ...). `bitnet_runtime` is passed for the modules that will need it
from B4 on.
"""

from __future__ import annotations

from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.diagnosis.correlator import SignalCorrelator
from neuropaca.sensing.activity.collector import ActivityCollector
from neuropaca.sensing.collector_module import XMetricCollector
from neuropaca.sensing.collectors.filesystem import FileSystemCollector
from neuropaca.sensing.collectors.system import SystemMetricCollector


def build_modules(
    config: Config,
    event_bus: EventBus,
    graph_memory: GraphMemory,
    bitnet_runtime: BitNetRuntime,
) -> list[BaseModule]:
    del bitnet_runtime  # unused until B4

    # B2.5 (D-9): when the real Wayland ActivityCollector is on, XMetricCollector
    # stops emitting its CPU-derived IDLE_DETECTED / ACTIVITY_DETECTED stand-in.
    sensing = XMetricCollector(event_bus, config, emit_idle_from_cpu=not config.activity_enabled)
    sensing.register_collector(
        SystemMetricCollector(
            poll_interval_seconds=config.poll_intervals.get("system", 60.0),
            top_process_count=config.top_process_count,
        )
    )
    if config.watch_paths:
        sensing.register_collector(
            FileSystemCollector(
                watch_paths=config.watch_paths,
                ignore_globs=config.filesystem_ignore_globs,
                buffer_size=config.snapshot_buffer_size,
                poll_interval_seconds=config.poll_intervals.get("filesystem", 60.0),
            )
        )

    diagnosis = SignalCorrelator(event_bus, config, graph_memory)

    # Start order = list order: L2 Sensing before L3 Diagnosis (Architecture.md §10).
    modules: list[BaseModule] = [sensing]
    if config.activity_enabled:
        modules.append(ActivityCollector(event_bus, config))
    modules.append(diagnosis)
    return modules
