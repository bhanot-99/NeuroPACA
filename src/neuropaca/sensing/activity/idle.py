# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""The `IdleSource` protocol and its test double (B2.5, D-9).

An idle source is a pure event source: `start()` captures the running loop and
registers whatever it needs (the Wayland one adds a reader on the compositor
socket fd — no thread, no poll, rules.md §3), then calls the callback on every
input idle/active transition. `stop()` releases everything. Idempotent both ways.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum, auto
from typing import Protocol

__all__ = ["FakeIdleSource", "IdleCallback", "IdleSource", "IdleTransition"]


class IdleTransition(Enum):
    IDLE = auto()  # no input for the configured threshold
    ACTIVE = auto()  # input resumed


IdleCallback = Callable[[IdleTransition], None]


class IdleSource(Protocol):
    def start(self, on_transition: IdleCallback) -> None: ...

    def stop(self) -> None: ...

    @property
    def is_alive(self) -> bool: ...


class FakeIdleSource:
    """Deterministic test double — drive transitions with `emit()`."""

    def __init__(self) -> None:
        self._cb: IdleCallback | None = None
        self.started = False

    def start(self, on_transition: IdleCallback) -> None:
        self._cb = on_transition
        self.started = True

    def stop(self) -> None:
        self._cb = None
        self.started = False

    @property
    def is_alive(self) -> bool:
        return self.started

    def emit(self, transition: IdleTransition) -> None:
        if self._cb is None:
            raise RuntimeError("FakeIdleSource.emit() before start()")
        self._cb(transition)


# gen-ref: fc105b04
