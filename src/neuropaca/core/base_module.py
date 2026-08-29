"""`BaseModule` — the lifecycle contract every L2-L9 module implements
(Architecture.md §3.7).

Services are *held, not inherited* (rules.md §0.1): a module receives the
`EventBus` (and, where needed, `GraphMemory` / `BitNetRuntime`) as constructor
arguments and never subclasses them.

`health()` returns this module's `ModuleHealth`; the orchestrator folds every
module's report into the system-wide `SystemHealth`. It must never raise and
never block (no `await`, no lock) — read cached counters only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from neuropaca.core.config import Config
from neuropaca.core.event_bus import EventBus
from neuropaca.core.health import ModuleHealth


class BaseModule(ABC):
    """Abstract lifecycle: initialize -> start -> (running) -> stop."""

    def __init__(self, name: str, event_bus: EventBus, config: Config) -> None:
        self.name = name
        self.event_bus = event_bus
        self.config = config
        self.is_running = False

    @abstractmethod
    async def initialize(self) -> None:
        """Subscribe to events, allocate resources, validate preconditions."""

    @abstractmethod
    async def start(self) -> None:
        """Begin work. Must be idempotent."""

    @abstractmethod
    async def stop(self) -> None:
        """Unsubscribe, cancel tasks, flush. Must be idempotent."""

    @abstractmethod
    def health(self) -> ModuleHealth:
        """A non-raising, non-blocking self-report."""
