"""L7 · the `BaseAction` contract (Architecture.md §11b, B7, D-14).

Every effect the daemon can have on the world is an object with four methods and
no shortcuts:

- `validate()` — refuse now, before anything has happened. Raises
  `SafetyGateError`; a valid action is one that *could* run, not one that may.
- `dry_run()` — describe the effect in one line without causing it. The daemon
  ships with `action_dry_run = True`, so this is the only path a fresh install
  ever takes.
- `execute()` — cause the effect. Reached only through `SafetyGate.run`, never
  called directly by a module.
- `rollback()` — undo it. Returns False when there is nothing to undo, which is
  not a failure; an action that *should* be reversible and is not must say so in
  `validate()` instead of discovering it after the fact.

`payload()` is the action's intent as plain data. It is what rides on
`ACTION_TRIGGERED`, and it is how L7 stays decoupled from the host: a
notification is an intent L9 renders, not a desktop call L7 makes.

`ActionTier` is L7-local on purpose. It is not a graph, wire, or persistence
type, so it does not belong in `core/enums.py` (rules.md §9's schema-bump rule
covers those five). `Config` cannot import this layer, so it spells the same
closed set as `VALID_ACTION_TIERS`; the assertion below keeps the two honest.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum, auto
from pathlib import Path
from typing import Any

from neuropaca.core.config import VALID_ACTION_TIERS


class ActionTier(StrEnum):
    """How much damage the action could do if it were wrong.

    `SAFE` — reversible, local, and worthless to an attacker: a graph write, a
    notification. Fires silently at the low pressure threshold.

    `DANGEROUS` — touches the filesystem or runs a process. Requires a recorded
    human confirmation at execution time, always, whatever the pressure and
    whatever the config says (rules.md §5.2).
    """

    SAFE = auto()
    DANGEROUS = auto()


assert {t.value for t in ActionTier} == set(VALID_ACTION_TIERS), (
    "ActionTier and Config.VALID_ACTION_TIERS have drifted apart"
)


@dataclass(frozen=True, slots=True)
class ActionResult:
    """The outcome of one gated attempt — also the shape of the 'after' audit line."""

    action: str
    tier: ActionTier
    ok: bool
    detail: str
    dry_run: bool = False
    confirmed: bool | None = None  # None = confirmation was not required
    rolled_back: bool = False
    request_id: str = ""
    finished_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class BaseAction(ABC):
    """One reversible, explainable effect."""

    #: Stable identifier used in the audit log and on `ACTION_TRIGGERED`.
    name: str = "action"
    #: Damage class. Read by the gate before anything else happens.
    tier: ActionTier = ActionTier.SAFE

    def __init__(self, *, reason: str) -> None:
        if not reason:
            # Architecture.md §7: an action that cannot explain itself must not
            # fire. Cheaper to make that unconstructable than to check it later.
            raise ValueError("an action requires a reason")
        self.reason = reason
        #: origin path -> quarantine token, filled by the gate before `execute()`
        #: so `rollback()` knows what to put back (rules.md §5.1).
        self.backups: dict[str, str] = {}

    def record_backup(self, origin: Path, token: str) -> None:
        self.backups[str(origin)] = token

    def backup_targets(self) -> tuple[Path, ...]:
        """Existing paths the gate must quarantine before `execute()` runs.

        Empty for actions that overwrite nothing (rules.md §5.1 — backup before
        any write; rules.md §5.7 — the prior bytes are moved, never deleted).
        """
        return ()

    def payload(self) -> dict[str, Any]:
        """The intent, as data, for `ACTION_TRIGGERED`. No live objects."""
        return {"kind": self.name, "reason": self.reason}

    @abstractmethod
    async def validate(self) -> None:
        """Raise `SafetyGateError` if this must not run. No side effects."""

    @abstractmethod
    async def dry_run(self) -> str:
        """One line describing what `execute()` would do. No side effects."""

    @abstractmethod
    async def execute(self) -> str:
        """Cause the effect and return a one-line result. Gate-only entry point."""

    @abstractmethod
    async def rollback(self) -> bool:
        """Undo a completed `execute()`. False = nothing to undo."""
