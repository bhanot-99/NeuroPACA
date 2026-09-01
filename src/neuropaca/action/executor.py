"""L7 · `ActionExecutor` — the module that owns the gate (Architecture.md §11b,
B7, D-14).

It is the only subscriber to `PRESSURE_THRESHOLD_REACHED`, and it turns pressure
into *proposals*, never directly into effects — every proposal goes through
`SafetyGate.run`.

What each tier does in B7:

- **low** — a `MemoryWriteAction`: the pressure entry, its reason, and its
  sources are written into the graph as an `EVENT_LOG` node edged to the node
  under pressure. Safe, silent, reversible — exactly the blueprint's "a safe
  action fires silently".
- **high** — a `NotificationAction`. B7 deliberately registers **no autonomous
  dangerous action**: reaching the high threshold makes a dangerous action
  *permissible*, and Architecture.md §7's own fallback is to prompt the user in
  the terminal. So the system asks; it does not kill anything by itself. When B8+
  adds a concrete dangerous action, it lands here behind the same gate, and the
  confirmation handshake is already the thing standing in front of it.

The two human-driven prefixes arrive as `USER_MESSAGE` (L9 parses the grammar;
L7 owns what they mean):

- **`$!`** — the emergency prefix. It skips L3 and L4 (no diagnosis, no learning
  — that is what "skips Y + Z" means), and it *forces the dangerous tier*: the
  human's explicit instruction stands in for accumulated pressure. It does not
  and cannot skip the confirmation handshake or the sandbox. Forcing the tier
  buys you past the pressure gradient, nothing else.
- **`$$`** — safe mode. Same command, same gate, plus a quarantined copy of the
  daemon's own state taken before it runs, and a non-zero exit treated as a
  failure to roll back from.

Neither prefix ever reaches L5: `PressureAccumulator` subscribes to L3 and L4
only (D-14), so a typed command cannot poison the pressure map.

Handlers hand off to background tasks. A dangerous action waits on a human for
up to `action_confirmation_timeout_seconds`, and a bus handler must never block
the dispatch loop for a minute (rules.md §2).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from neuropaca.action.actions import (
    FileWriteAction,
    MemoryWriteAction,
    NotificationAction,
    RunCommandAction,
)
from neuropaca.action.audit import ActionAudit
from neuropaca.action.base import ActionResult, BaseAction
from neuropaca.action.confirm import ConfirmationBroker
from neuropaca.action.gate import SafetyGate
from neuropaca.action.quarantine import Quarantine
from neuropaca.action.sandbox import Sandbox
from neuropaca.core.base_module import BaseModule
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, RelationType
from neuropaca.core.errors import SafetyGateError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, system_error_event
from neuropaca.drive.pressure import PressureEntry

_log = logging.getLogger(__name__)

_COMMAND_PREFIXES = frozenset({"$!", "$$"})
_COMMAND_TIMEOUT_SECONDS = 30.0

__all__ = [
    "ActionExecutor",
    "FileWriteAction",
    "MemoryWriteAction",
    "NotificationAction",
    "RunCommandAction",
]


class ActionExecutor(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
    ) -> None:
        super().__init__("action", event_bus, config)
        self._graph = graph_memory
        # Write containment: the daemon may only write where the user already
        # told it to look. `watch_paths` empty (the default) => no file action
        # can resolve a path at all, which is the fail-closed default we want.
        self.sandbox = Sandbox(config.watch_paths)
        self.quarantine = Quarantine(config.quarantine_path, config.quarantine_ttl_hours)
        self.audit = ActionAudit(config.action_log_path)
        self.confirmations = ConfirmationBroker(
            event_bus, config.action_confirmation_timeout_seconds
        )
        self.gate = SafetyGate(config, event_bus, self.audit, self.quarantine, self.confirmations)
        self._tasks: set[asyncio.Task[None]] = set()
        self._proposals = 0
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.PRESSURE_THRESHOLD_REACHED, self.on_pressure_threshold)
        self.event_bus.subscribe(EventType.USER_MESSAGE, self.on_user_message)
        self.event_bus.subscribe(
            EventType.ACTION_CONFIRMATION_RESPONSE, self.confirmations.on_response
        )

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        swept = await self.quarantine.purge_expired()
        if swept:
            _log.info("L7 swept %d expired quarantine entries at start", swept)

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.PRESSURE_THRESHOLD_REACHED, self.on_pressure_threshold)
        self.event_bus.unsubscribe(EventType.USER_MESSAGE, self.on_user_message)
        self.event_bus.unsubscribe(
            EventType.ACTION_CONFIRMATION_RESPONSE, self.confirmations.on_response
        )
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._tasks.clear()

    def health(self) -> ModuleHealth:
        counters = self.gate.counters
        approved, denied, expired = self.confirmations.counters
        mode = "dry-run" if self.config.action_dry_run else "live"
        return ModuleHealth(
            name=self.name,
            ok=self.is_running and self.audit.failures == 0,
            detail=(
                f"{mode} · tiers {','.join(self.config.action_enabled_tiers) or 'none'} · "
                f"{self._proposals} proposed · {counters['executed']} executed · "
                f"{counters['dry_runs']} dry-run · {counters['refused']} refused · "
                f"{counters['rollbacks']} rolled back · confirmations "
                f"{approved}/{denied}/{expired} ok/no/expired · "
                f"{len(self.confirmations.pending)} pending · {self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handlers
    async def on_pressure_threshold(self, event: Event) -> None:
        """L5 crossed a threshold. Build the tier's proposal and gate it."""
        try:
            entry = event.payload.get("entry")
            tier = str(event.payload.get("tier", ""))
            if not isinstance(entry, PressureEntry):
                return
            action = (
                self._high_tier_proposal(entry)
                if tier == "high"
                else self._low_tier_proposal(entry)
            )
            self._spawn(action, trigger=f"pressure:{tier}:{entry.node_id}")
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._fail("on_pressure_threshold", exc)

    async def on_user_message(self, event: Event) -> None:
        """`$!` / `$$` only. `$` and `$?` are questions and never reach L7."""
        try:
            prefix = str(event.payload.get("prefix", ""))
            if prefix not in _COMMAND_PREFIXES:
                return
            text = str(event.payload.get("text", "")).strip()
            action = self._command_proposal(prefix, text)
            self._spawn(action, trigger=f"user:{prefix}")
        except SafetyGateError as exc:
            # A malformed command is refused here, before an action exists to
            # gate — still logged, so the audit trail has no silent gaps.
            self._errors += 1
            _log.warning("L7 refused a %s command: %s", event.payload.get("prefix"), exc)
            await self.audit.record(
                "attempt",
                request_id="",
                action="run_command",
                tier="dangerous",
                trigger=f"user:{event.payload.get('prefix')}",
                reason="user command",
                refused=str(exc),
            )
            await self.audit.record(
                "result",
                request_id="",
                action="run_command",
                tier="dangerous",
                trigger=f"user:{event.payload.get('prefix')}",
                ok=False,
                detail=f"refused: {exc}",
            )
        except Exception as exc:
            self._fail("on_user_message", exc)

    def _fail(self, where: str, exc: Exception) -> None:
        self._errors += 1
        _log.exception("action %s failed", where)
        self.event_bus.publish(
            system_error_event(module="action", exception=str(exc), severity="handler")
        )

    # ------------------------------------------------------------- proposals
    def _low_tier_proposal(self, entry: PressureEntry) -> MemoryWriteAction:
        """Safe and silent: remember that this node got hot, and why."""
        node_id = f"action:{uuid4().hex[:12]}"
        return MemoryWriteAction(
            self._graph,
            reason=entry.reason,
            node_id=node_id,
            node_type=NodeType.EVENT_LOG,
            attributes={
                "label": f"pressure {entry.pressure:.2f} on {entry.node_id}",
                "pressure": round(entry.pressure, 4),
                "sources": ",".join(entry.sources),
            },
            edges=((entry.node_id, RelationType.RELATED_TO),),
        )

    def _high_tier_proposal(self, entry: PressureEntry) -> NotificationAction:
        """Corroborated and hot. B7 asks rather than acts (Architecture.md §7)."""
        return NotificationAction(
            reason=entry.reason,
            text=(
                f"{entry.node_id} is under corroborated pressure "
                f"({entry.pressure:.2f}, {'+'.join(entry.sources)}): {entry.reason}"
            ),
            node_ids=(entry.node_id,),
        )

    def _command_proposal(self, prefix: str, text: str) -> RunCommandAction:
        """Turn the human's typed command into an argv. `shlex.split` parses the
        *user's own* literal text — never model output, never graph content
        (rules.md §5.4) — and the result is exec'd as a list with no shell."""
        if not text:
            raise SafetyGateError("empty command")
        try:
            argv = tuple(shlex.split(text))
        except ValueError as exc:
            raise SafetyGateError(f"unparseable command: {exc}") from exc
        if not argv:
            raise SafetyGateError("empty command")

        backups: tuple[Path, ...] = ()
        if prefix == "$$":
            # Safe mode: preserve the daemon's own state before running.
            backups = (Path(self.config.graph_db_path),)
        return RunCommandAction(
            self.sandbox,
            reason=f"user requested via {prefix}",
            argv=argv,
            timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
            backup_paths=backups,
        )

    # ---------------------------------------------------------------- running
    def _spawn(self, action: BaseAction, *, trigger: str) -> None:
        """Run one gated action off the dispatch loop and keep a strong reference
        to the task (an unreferenced task can be garbage-collected mid-flight)."""
        self._proposals += 1
        task = asyncio.create_task(self._run(action, trigger))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, action: BaseAction, trigger: str) -> None:
        try:
            result = await self.gate.run(action, trigger=trigger)
            self._last_at = result.finished_at
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail("gate run", exc)

    async def run_now(self, action: BaseAction, *, trigger: str) -> ActionResult:
        """Gate one action and wait for its result. For the validation scripts
        and tests — the daemon itself always goes through `_spawn`."""
        return await self.gate.run(action, trigger=trigger)
