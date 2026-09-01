"""L7 · `SafetyGate` — the single path from intent to effect
(Architecture.md §11b, rules.md §5, D-14).

No module calls `action.execute()`. Everything goes through `run()`, in this
order, and every branch out of it writes both audit lines:

1. **Tier enabled?** `action_enabled_tiers` gates whether this class of effect
   may run at all. A disabled tier is a refusal, and a refusal is an attempt —
   it is logged.
2. **Audit the attempt.** Before anything happens. If the log cannot be written
   the action is *refused*: an unrecordable effect is not permitted to happen
   (rules.md §5.6 is a precondition, not a report).
3. **Validate.** The action's own preconditions — sandbox containment, argv
   shape. Refusal here has caused nothing.
4. **Dry run?** With `action_dry_run = True` (the shipped default) the gate stops
   at `dry_run()`. Nothing is confirmed, nothing is backed up, nothing happens —
   this is the review period the B7 exit criteria require.
5. **Confirm.** `DANGEROUS` pauses for a recorded human answer. Timeout, denial,
   and "nobody is listening" are the same outcome: refusal.
6. **Back up.** Every path the action declares is copied to quarantine first.
   A backup that cannot be taken is a refusal — never a write without one.
7. **Execute**, and on any failure `rollback()` immediately.
8. **Audit the result** and publish `ACTION_TRIGGERED` (L9 renders it, L4 scores
   from it).

The gate holds no per-action state; it can run several actions concurrently, and
each one's `request_id` ties its two audit lines and its confirmation together.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from neuropaca.action.audit import ActionAudit
from neuropaca.action.base import ActionResult, ActionTier, BaseAction
from neuropaca.action.confirm import ConfirmationBroker
from neuropaca.action.quarantine import Quarantine
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.errors import SafetyGateError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event

_log = logging.getLogger(__name__)


class SafetyGate:
    def __init__(
        self,
        config: Config,
        event_bus: EventBus,
        audit: ActionAudit,
        quarantine: Quarantine,
        confirmations: ConfirmationBroker,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._audit = audit
        self._quarantine = quarantine
        self._confirmations = confirmations
        self._executed = 0
        self._refused = 0
        self._dry_runs = 0
        self._rollbacks = 0

    @property
    def counters(self) -> dict[str, int]:
        return {
            "executed": self._executed,
            "refused": self._refused,
            "dry_runs": self._dry_runs,
            "rollbacks": self._rollbacks,
        }

    async def run(self, action: BaseAction, *, trigger: str) -> ActionResult:
        request_id = uuid4().hex[:12]
        tier = action.tier

        if tier.value not in self._config.action_enabled_tiers:
            return await self._refuse(
                action, request_id, trigger, f"tier {tier.value!r} is not enabled"
            )

        recorded = await self._audit.record(
            "attempt",
            request_id=request_id,
            action=action.name,
            tier=tier.value,
            trigger=trigger,
            reason=action.reason,
            dry_run=self._config.action_dry_run,
            intent=action.payload(),
        )
        if not recorded:
            # No log, no action. Refusing here is the only way "the audit log is
            # complete for every attempt" can be an invariant rather than a hope.
            self._refused += 1
            result = ActionResult(
                action=action.name,
                tier=tier,
                ok=False,
                detail="refused: the action audit log could not be written",
                request_id=request_id,
            )
            _log.error("L7 refused %s — audit log unwritable", action.name)
            self._publish(action, result, trigger)
            return result

        try:
            await action.validate()
        except SafetyGateError as exc:
            return await self._finish(
                action, request_id, trigger, ok=False, detail=f"refused: {exc}"
            )

        if self._config.action_dry_run:
            try:
                described = await action.dry_run()
            except SafetyGateError as exc:
                return await self._finish(
                    action, request_id, trigger, ok=False, detail=f"refused: {exc}"
                )
            self._dry_runs += 1
            return await self._finish(
                action,
                request_id,
                trigger,
                ok=True,
                detail=f"dry-run: {described}",
                dry_run=True,
            )

        confirmed: bool | None = None
        confirmation_id = ""
        if tier is ActionTier.DANGEROUS:
            summary = await action.dry_run()
            confirmation_id, approved = await self._confirmations.request(
                action=action.name, tier=tier.value, summary=summary, reason=action.reason
            )
            confirmed = approved
            if not approved:
                return await self._finish(
                    action,
                    request_id,
                    trigger,
                    ok=False,
                    detail="refused: not confirmed (denied or expired)",
                    confirmed=False,
                    confirmation_id=confirmation_id,
                )

        try:
            await self._back_up(action)
        except SafetyGateError as exc:
            return await self._finish(
                action,
                request_id,
                trigger,
                ok=False,
                detail=f"refused: {exc}",
                confirmed=confirmed,
                confirmation_id=confirmation_id,
            )

        try:
            detail = await action.execute()
        except Exception as exc:
            rolled_back = await self._try_rollback(action)
            return await self._finish(
                action,
                request_id,
                trigger,
                ok=False,
                detail=f"failed: {exc}",
                confirmed=confirmed,
                rolled_back=rolled_back,
                confirmation_id=confirmation_id,
            )

        self._executed += 1
        return await self._finish(
            action,
            request_id,
            trigger,
            ok=True,
            detail=detail,
            confirmed=confirmed,
            confirmation_id=confirmation_id,
        )

    # ----------------------------------------------------------------- helpers
    async def _back_up(self, action: BaseAction) -> None:
        for target in action.backup_targets():
            token = await self._quarantine.stash(target)
            if token is not None:
                action.record_backup(target, token)

    async def _try_rollback(self, action: BaseAction) -> bool:
        try:
            rolled_back = await action.rollback()
        except Exception:
            _log.exception("L7 rollback of %s failed", action.name)
            return False
        if rolled_back:
            self._rollbacks += 1
        return rolled_back

    async def _refuse(
        self, action: BaseAction, request_id: str, trigger: str, why: str
    ) -> ActionResult:
        await self._audit.record(
            "attempt",
            request_id=request_id,
            action=action.name,
            tier=action.tier.value,
            trigger=trigger,
            reason=action.reason,
            dry_run=self._config.action_dry_run,
            intent=action.payload(),
        )
        return await self._finish(action, request_id, trigger, ok=False, detail=f"refused: {why}")

    async def _finish(
        self,
        action: BaseAction,
        request_id: str,
        trigger: str,
        *,
        ok: bool,
        detail: str,
        dry_run: bool = False,
        confirmed: bool | None = None,
        rolled_back: bool = False,
        confirmation_id: str = "",
    ) -> ActionResult:
        if not ok:
            self._refused += 1
        result = ActionResult(
            action=action.name,
            tier=action.tier,
            ok=ok,
            detail=detail,
            dry_run=dry_run,
            confirmed=confirmed,
            rolled_back=rolled_back,
            request_id=request_id,
        )
        await self._audit.record(
            "result",
            request_id=request_id,
            action=action.name,
            tier=action.tier.value,
            trigger=trigger,
            ok=ok,
            detail=detail,
            dry_run=dry_run,
            confirmed=confirmed,
            rolled_back=rolled_back,
        )
        _log.info("L7 %s %s: %s", "ran" if ok else "refused", action.name, detail)
        self._publish(action, result, trigger, confirmation_id)
        return result

    def _publish(
        self, action: BaseAction, result: ActionResult, trigger: str, confirmation_id: str = ""
    ) -> None:
        """`ACTION_TRIGGERED` — consumed by L9 (renders it, delivers notification
        intents) and L4 (scores the outcome back into the graph).

        `confirmation_id` is how a prompt gets retired: an attempt that asked for
        a confirmation always ends here, whatever the verdict, so L9 can drop the
        prompt the moment L7 is no longer waiting on it. Without it an expired
        request would sit in the human's terminal forever and the next `confirm`
        would answer a question nobody is listening to."""
        self._event_bus.publish(
            Event(
                event_type=EventType.ACTION_TRIGGERED,
                source="action",
                payload={
                    "result": result,
                    "intent": action.payload(),
                    "trigger": trigger,
                    "action": action.name,
                    "tier": action.tier.value,
                    "ok": result.ok,
                    "dry_run": result.dry_run,
                    "detail": result.detail,
                    "request_id": result.request_id,
                    "confirmation_id": confirmation_id,
                },
            )
        )
