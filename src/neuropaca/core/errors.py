"""The NeuroPACA exception hierarchy.

One root (`NeuroPACAError`) so a supervising layer can catch everything the
system raises on purpose without also swallowing `KeyboardInterrupt`,
`SystemExit`, or `asyncio.CancelledError` (rules.md §1 — cancellation is always
re-raised, never caught here).

The tree mirrors the layer map in Architecture.md §2. Subclasses carry no
behaviour yet; they exist so call sites and tests can be specific from B1
onward. Add a subclass when a handler needs to distinguish a failure — not
preemptively.
"""

from __future__ import annotations


class NeuroPACAError(Exception):
    """Base class for every error NeuroPACA raises deliberately."""


class ConfigError(NeuroPACAError):
    """Malformed, missing, or contradictory configuration (Architecture.md §3.4)."""


class EventBusError(NeuroPACAError):
    """Failure inside the L1 EventBus itself — not a subscriber raising.

    A subscriber that raises is isolated and reported as ``SYSTEM_ERROR``
    (rules.md §2); this is for the bus's own invariants breaking.
    """


class GraphMemoryError(NeuroPACAError):
    """L1 GraphMemory failure — corrupt store, failed atomic save, schema drift."""


class InferenceError(NeuroPACAError):
    """The BitNet runtime failed: load failure, timeout, or a broken backend.

    Model output that is merely low-quality or fails the validation gate is not
    an error — it is discarded or retried per rules.md §4.1.
    """


class InferenceTimeout(InferenceError):
    """An inference call exceeded its wall-clock budget (rules.md §4)."""


class SafetyGateError(NeuroPACAError):
    """The L7 action safety gate refused or could not guarantee a safe effect
    (sandbox, backup, or rollback unavailable) — Architecture.md §11b."""


class ModuleLifecycleError(NeuroPACAError):
    """A module failed its ``initialize()`` / ``start()`` / ``stop()`` contract
    (Architecture.md §3.7)."""
