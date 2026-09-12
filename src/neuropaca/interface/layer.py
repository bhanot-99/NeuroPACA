# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L9 · `InterfaceLayer` — the only module that talks to the human
(Architecture.md §9, B5).

Shape (B5, re-scoped in B12):
- a **Unix-domain socket** at ``$XDG_RUNTIME_DIR/neuropaca.sock`` (JSONL framing:
  one JSON request per line, one JSON response per line). The thin CLI
  (`interface/cli.py`) is the only client. Ops: `health`, `insights`,
  `notifications`, `confirmations`, `confirm`, `run`, `explain`, `briefing`,
  `mirror` (A2, VISION_PHASES.md §3.8), `presence`, `pause`, `feedback` (A1,
  VISION_PHASES.md — `scripts/neuropaca_tray.py` is the only caller of these
  three today), `reload-graph` (internal — `interface/offline.py`'s
  `repair-graph` verb is the only caller, not a `neuropaca` verb of its own).
- the terminal is a **read-only project guide** now: `neuropaca tell` / `overview`
  answer deterministically on the client side (`interface/describe.py`) and never
  reach this module. There is no natural-language query of the behavioural graph
  here any more — that path (`$` / `$?`, `chat`, `KnowledgeIndex`) was removed.
- **`run`** (B7, D-14) — L9 publishes `USER_MESSAGE` with the internal `prefix`
  enum (`$!` = run, `$$` = run + state backup) and returns `queued` immediately.
  L7 owns what it means and answers through its own gate; L9 never executes
  anything. It is the human end of the confirmation handshake: it holds the
  `ACTION_CONFIRMATION_REQUEST`s L7 is blocked on (`confirmations`), relays the
  verdict as `ACTION_CONFIRMATION_RESPONSE` (`confirm`), and delivers L7's
  notification *intents* (`notifications`) — L7 never touches the desktop.
- **`explain`** (B12) — the one free-decode call (rules.md §4.1 carve-out): the
  interactive model paraphrases a first-party file summary the client generated.
  Bounded, advisory, never executed / stored / published. A missing interactive
  model just means no paraphrase.
- `conversation_history` is a `list[Message]` in RAM only — never disk, graph, or
  log; every logged IPC payload goes through `redact()` (rules.md §6, PRD §8.5).
- health: L9 cannot import L10, so `health` publishes `SYSTEM_HEALTH_REQUEST` and
  waits for L10's `SYSTEM_HEALTH_REPORT` (A6).
- `PATTERN_DETECTED` / `MEMORY_UPDATED` are deliberately **not** subscribed (B6).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, MessageRole, NodeType
from neuropaca.core.errors import GraphMemoryError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.logging import redact
from neuropaca.core.models import Event, Moment, system_error_event
from neuropaca.core.presence import PresenceInputs, compute_presence_state
from neuropaca.interface import desktop
from neuropaca.interface.message import Message
from neuropaca.learning.insight import Insight
from neuropaca.learning.prompts import (
    EXPLAIN_MAX_TOKENS,
    build_explain_prompt,
    clean_explain_answer,
)

_log = logging.getLogger(__name__)

_MAX_HISTORY = 50  # conversation_history turns kept in RAM (blueprint max_history_length)
_EXPLAIN_INFER_TIMEOUT = 45.0  # B12 — CPU wall-clock ceiling for the `tell --explain` paraphrase
_HEALTH_TIMEOUT = 2.0
# S0's on-demand briefing composes fresh (a PPR walk, not a cache read) —
# generous next to `_HEALTH_TIMEOUT`, still well under a human's patience.
_BRIEFING_TIMEOUT = 5.0
# A2 · the mirror composes a KL divergence over up to `mirror_baseline_days`
# of history, not a single PPR walk — a slightly longer budget than the
# briefing's, but still well under the test harness's fixed 8s client-side
# socket read timeout (tests/test_interface.py) — the exact race documented
# in RESEARCH_DOSSIER.md that made `_BRIEFING_TIMEOUT` need its own margin.
_MIRROR_TIMEOUT = 6.0

# Insight surfacing (B5, B3; B6 adds `proactive` — L6 idle thoughts, D-13)
_INSIGHT_MIN_CONFIDENCE = 0.75
_SURFACEABLE_CATEGORIES = frozenset({"anomaly", "distraction", "proactive"})
_DAILY_INSIGHT_CAP = 3
_SURFACEABLE_NODE_PREFIXES = ("insight:", "idle:")

# B7 (surfaced as `neuropaca run` since B12): L9 relays a command to L7 as
# `USER_MESSAGE` and returns immediately — a dangerous action then waits on a
# *separate* confirmation round-trip, so the socket is never held open for the
# length of a human decision. `$!` (run) / `$$` (run + state backup) are the
# internal wire enum L7 dispatches on; the user types `run` / `run --backup`.
_MAX_PENDING_NOTIFICATIONS = 50
# V-12 · at most one desktop popup per this many seconds, and one in flight.
# Anything over the rate still lands in the terminal queue — the popup is a
# nudge, `neuropaca notifications` and the audit log are the record.
_DESKTOP_MIN_GAP_SECONDS = 30.0
# Surface-once bookkeeping is the only state here that outlives the node it
# describes: an INSIGHT / IDLE_THOUGHT is pruned at its 48 h TTL, but its id had
# to stay remembered or the same thought could be surfaced twice. Remembering
# them *forever* is what made this a leak — on a daemon that runs for months the
# set only grows, and every id in it past the TTL refers to a node that no longer
# exists. Bounded to the newest N in insertion order; anything older than that is
# long past its TTL, so re-surfacing it is not a risk the cap creates.
_MAX_SURFACED_IDS = 512
# Insights queue here until a CLI client drains them. Nothing guarantees one ever
# connects, so the queue needs its own ceiling — the graph node and the audit log
# are the durable record, this is only what is waiting to be read out.
_MAX_PENDING_INSIGHTS = 50
# A prompt this far past its timeout is stale even if L7's completion event never
# arrived; L9 stops offering it rather than accept an answer nobody awaits.
_CONFIRMATION_GRACE = 5.0


def default_socket_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "neuropaca.sock"


def _moment_to_dict(moment: Moment) -> dict[str, Any]:
    """A `Moment` over the wire — `json.dumps(..., default=str)` cannot encode
    a dataclass or a tuple key-order-stably on its own, so this is explicit."""
    return {
        "kind": moment.kind,
        "text": moment.text,
        "evidence": list(moment.evidence),
        "value": moment.value,
        "context": moment.context,
    }


class InterfaceLayer(BaseModule):
    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        bitnet_runtime: BitNetRuntime,
        *,
        clock: Clock | None = None,
        socket_path: str | Path | None = None,
    ) -> None:
        super().__init__("interface", event_bus, config)
        self._graph = graph_memory
        self._runtime = bitnet_runtime
        self._clock: Clock = clock or SystemClock()
        self._socket_path = Path(socket_path) if socket_path is not None else default_socket_path()
        self._server: asyncio.Server | None = None
        self._conversation_history: list[Message] = []
        self._latest_health: dict[str, Any] | None = None
        self._health_waiters: list[asyncio.Future[dict[str, Any] | None]] = []
        # S0 · `neuropaca briefing` (VISION_PHASES.md). Keyed by request_id,
        # not a broadcast list like health's — a report legitimately differs
        # request to request (a fresh composition), so one waiter must not
        # resolve on another's answer.
        self._briefing_waiters: dict[str, asyncio.Future[dict[str, Any] | None]] = {}
        # A2 · `neuropaca mirror` (VISION_PHASES.md §3.8). Same shape as the
        # briefing waiters above, for the same reason.
        self._mirror_waiters: dict[str, asyncio.Future[dict[str, Any] | None]] = {}
        self._pending_insights: list[Insight] = []
        self._pending_notifications: list[dict[str, Any]] = []
        # V-12 · desktop delivery state
        self._desktop_task: asyncio.Task[None] | None = None
        self._desktop_last_at = float("-inf")
        self._desktop_sent = 0
        self._desktop_failed = 0
        self._desktop_skipped = 0
        self._pending_confirmations: dict[str, dict[str, Any]] = {}
        # dict, not set: insertion-ordered, so trimming drops the oldest ids.
        self._surfaced_ids: dict[str, None] = {}
        self._cap_day: date | None = None
        self._surfaced_today = 0
        self._queries = 0
        self._errors = 0
        self._interactive_disabled = False  # set once the interactive model is proven absent
        # A1 · the tray's presence state (VISION_PHASES.md, `core/presence.py`).
        # `_awake_since` is stamped in `start()` — the daemon's own boot time,
        # the fallback "since" when nothing more specific is known yet.
        self._awake_since: datetime = self._clock.now()
        self._dmn_thinking = False
        self._dmn_thinking_since: datetime | None = None
        self._idle_since: datetime | None = None
        self._focused_since: datetime | None = None
        self._last_moment: Moment | None = None
        self._last_moment_at: datetime | None = None
        self._last_insight_at: datetime | None = None
        self._thoughts_today: list[dict[str, Any]] = []
        self._thoughts_cap_day: date | None = None
        self._paused_until: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.INSIGHT_GENERATED, self.on_insight_generated)
        self.event_bus.subscribe(EventType.SYSTEM_HEALTH_REPORT, self._on_health_report)
        self.event_bus.subscribe(EventType.BRIEFING_REPORT, self._on_briefing_report)
        self.event_bus.subscribe(EventType.MIRROR_REPORT, self._on_mirror_report)
        # B7 (D-14): L7 publishes intents and confirmation prompts; L9 is the
        # only module that may turn either into something a human sees.
        self.event_bus.subscribe(EventType.ACTION_TRIGGERED, self.on_action_triggered)
        self.event_bus.subscribe(
            EventType.ACTION_CONFIRMATION_REQUEST, self.on_confirmation_request
        )
        # A1 · presence (VISION_PHASES.md) — the state machine's five inputs.
        self.event_bus.subscribe(EventType.APP_SWITCH, self.on_app_switch)
        self.event_bus.subscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.subscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)
        self.event_bus.subscribe(EventType.DMN_CYCLE_STARTED, self.on_dmn_cycle_started)
        self.event_bus.subscribe(EventType.DMN_CYCLE_ENDED, self.on_dmn_cycle_ended)
        self.event_bus.subscribe(EventType.MOMENT_PROPOSED, self.on_moment_proposed)
        # PATTERN_DETECTED / MEMORY_UPDATED are intentionally NOT subscribed (B6).

    async def start(self) -> None:
        if self.is_running:
            return
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        with contextlib.suppress(OSError):
            os.chmod(self._socket_path, 0o600)  # owner-only (rules.md §6 spirit)
        self._rehydrate_surfaced_ids()
        self._awake_since = self._clock.now()
        self.is_running = True
        _log.info("L9 interface listening on %s", self._socket_path)

    def _rehydrate_surfaced_ids(self) -> None:
        """Surface-once survives a restart: any INSIGHT node already stamped
        `surfaced_at` (schema v2) is treated as seen."""
        for node_id in self._graph.node_ids:
            if node_id.startswith(_SURFACEABLE_NODE_PREFIXES):
                node = self._graph.get_node(node_id)
                if node is not None and node.surfaced_at is not None:
                    self._remember_surfaced(node_id)

    def _remember_surfaced(self, node_id: str) -> None:
        """Record an id as already surfaced, keeping only the newest N."""
        self._surfaced_ids.pop(node_id, None)  # re-insert so it counts as newest
        self._surfaced_ids[node_id] = None
        while len(self._surfaced_ids) > _MAX_SURFACED_IDS:
            self._surfaced_ids.pop(next(iter(self._surfaced_ids)))

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.INSIGHT_GENERATED, self.on_insight_generated)
        self.event_bus.unsubscribe(EventType.SYSTEM_HEALTH_REPORT, self._on_health_report)
        self.event_bus.unsubscribe(EventType.BRIEFING_REPORT, self._on_briefing_report)
        self.event_bus.unsubscribe(EventType.MIRROR_REPORT, self._on_mirror_report)
        self.event_bus.unsubscribe(EventType.ACTION_TRIGGERED, self.on_action_triggered)
        desktop_task, self._desktop_task = self._desktop_task, None
        if desktop_task is not None and not desktop_task.done():
            desktop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await desktop_task
        self.event_bus.unsubscribe(
            EventType.ACTION_CONFIRMATION_REQUEST, self.on_confirmation_request
        )
        self.event_bus.unsubscribe(EventType.APP_SWITCH, self.on_app_switch)
        self.event_bus.unsubscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.unsubscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)
        self.event_bus.unsubscribe(EventType.DMN_CYCLE_STARTED, self.on_dmn_cycle_started)
        self.event_bus.unsubscribe(EventType.DMN_CYCLE_ENDED, self.on_dmn_cycle_ended)
        self.event_bus.unsubscribe(EventType.MOMENT_PROPOSED, self.on_moment_proposed)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()
        self._conversation_history.clear()  # RAM-only history dies with the process

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running and self._server is not None,
            detail=(
                f"socket {self._socket_path.name} · {self._queries} requests · "
                f"{self._surfaced_today} insights surfaced today · "
                f"{len(self._pending_confirmations)} confirmations waiting · "
                f"{self._errors} errors"
            ),
        )

    # ------------------------------------------------------------ bus handlers
    async def _on_health_report(self, event: Event) -> None:
        health = event.payload.get("health")
        self._latest_health = health if isinstance(health, dict) else None
        for fut in self._health_waiters:
            if not fut.done():
                fut.set_result(self._latest_health)
        self._health_waiters.clear()

    async def _on_briefing_report(self, event: Event) -> None:
        request_id = str(event.payload.get("request_id", ""))
        fut = self._briefing_waiters.pop(request_id, None)
        if fut is not None and not fut.done():
            moment = event.payload.get("moment")
            fut.set_result(_moment_to_dict(moment) if isinstance(moment, Moment) else None)

    async def _on_mirror_report(self, event: Event) -> None:
        request_id = str(event.payload.get("request_id", ""))
        fut = self._mirror_waiters.pop(request_id, None)
        if fut is not None and not fut.done():
            moment = event.payload.get("moment")
            fut.set_result(_moment_to_dict(moment) if isinstance(moment, Moment) else None)

    async def on_insight_generated(self, event: Event) -> None:
        try:
            insight = event.payload.get("insight")
            if isinstance(insight, Insight):
                await self._consider_insight(insight)
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._errors += 1
            _log.exception("interface on_insight_generated failed")
            self.event_bus.publish(
                system_error_event(module="interface", exception=str(exc), severity="handler")
            )

    async def _consider_insight(self, insight: Insight) -> None:
        """Priority filter -> surface-once -> daily cap (resets at local midnight).
        Accepted insights queue for the next `insights` request; `surfaced_at` is
        stamped on the graph node in step 4."""
        if insight.confidence < _INSIGHT_MIN_CONFIDENCE:
            return
        if insight.category not in _SURFACEABLE_CATEGORIES:
            return
        if not insight.node_id or insight.node_id in self._surfaced_ids:
            return

        today = self._clock.now().date()
        if today != self._cap_day:
            self._cap_day = today
            self._surfaced_today = 0
        if self._surfaced_today >= _DAILY_INSIGHT_CAP:
            return

        self._remember_surfaced(insight.node_id)
        self._surfaced_today += 1
        self._pending_insights.append(insight)
        if len(self._pending_insights) > _MAX_PENDING_INSIGHTS:
            del self._pending_insights[:-_MAX_PENDING_INSIGHTS]

        # A1 · presence's "noticed" input (any surfaceable insight); the
        # tray's own "today's thoughts" menu only wants the idle-thought half.
        now = self._clock.now()
        self._last_insight_at = now
        if insight.category == "proactive":
            self._roll_thoughts_today(now)
            self._thoughts_today.append(
                {"text": insight.label, "node_id": insight.node_id, "at": now.isoformat()}
            )

        # Stamp the graph so surface-once survives a restart (schema v2). The
        # mutating module publishes MEMORY_UPDATED, never GraphMemory (D-5.3).
        # `upsert_node` protects `node_type` on an existing node, so passing the
        # right kind only matters if the node somehow vanished — pick it by id.
        node_type = (
            NodeType.IDLE_THOUGHT if insight.node_id.startswith("idle:") else NodeType.INSIGHT
        )
        await self._graph.upsert_node(
            insight.node_id, node_type, {"surfaced_at": self._clock.now()}
        )
        self.event_bus.publish(
            Event(
                event_type=EventType.MEMORY_UPDATED,
                source="interface",
                payload={"node_ids": [insight.node_id], "operation": "insight_surfaced"},
            )
        )

    def _roll_thoughts_today(self, now: datetime) -> None:
        today = now.date()
        if today != self._thoughts_cap_day:
            self._thoughts_cap_day = today
            self._thoughts_today = []

    # --------------------------------------------------------- A1 · presence
    async def on_app_switch(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            if self._idle_since is None:  # a switch during an open idle spell is not a return
                self._focused_since = self._clock.now()
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._on_presence_error(exc)

    async def on_idle_detected(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._idle_since = self._clock.now()
            self._focused_since = None
        except Exception as exc:
            self._on_presence_error(exc)

    async def on_activity_detected(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._idle_since = None
            self._focused_since = self._clock.now()
        except Exception as exc:
            self._on_presence_error(exc)

    async def on_dmn_cycle_started(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._dmn_thinking = True
            self._dmn_thinking_since = self._clock.now()
        except Exception as exc:
            self._on_presence_error(exc)

    async def on_dmn_cycle_ended(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._dmn_thinking = False
        except Exception as exc:
            self._on_presence_error(exc)

    async def on_moment_proposed(self, event: Event) -> None:
        try:
            if not self.is_running:
                return
            moment = event.payload.get("moment")
            if isinstance(moment, Moment):
                self._last_moment = moment
                self._last_moment_at = self._clock.now()
        except Exception as exc:
            self._on_presence_error(exc)

    def _is_paused(self, now: datetime) -> bool:
        return self._paused_until is not None and now < self._paused_until

    def _presence_payload(self) -> dict[str, Any]:
        now = self._clock.now()
        self._roll_thoughts_today(now)
        state, since = compute_presence_state(
            PresenceInputs(
                now=now,
                awake_since=self._awake_since,
                dmn_thinking=self._dmn_thinking,
                dmn_thinking_since=self._dmn_thinking_since,
                last_moment_at=self._last_moment_at,
                last_insight_at=self._last_insight_at,
                idle_since=self._idle_since,
                focused_since=self._focused_since,
            )
        )
        paused_until = self._paused_until if self._is_paused(now) else None
        return {
            "ok": True,
            "state": str(state),
            "since": since.isoformat(),
            "thoughts_today": list(self._thoughts_today),
            "last_moment": _moment_to_dict(self._last_moment) if self._last_moment else None,
            "paused_until": paused_until.isoformat() if paused_until is not None else None,
        }

    def _pause(self, minutes: float) -> dict[str, Any]:
        now = self._clock.now()
        self._paused_until = now + timedelta(minutes=max(0.0, minutes))
        return {"ok": True, "paused_until": self._paused_until.isoformat()}

    def _submit_feedback(self, outcome: str) -> dict[str, Any]:
        """F2's minimal first mover: the tray is the first thing that ever
        actually publishes `MOMENT_FEEDBACK` — `EpisodicWriter` (S0) has been
        able to record it since it was built, nothing before A1 ever sent one."""
        if self._last_moment is None:
            return {"ok": False, "error": "no moment to give feedback on yet"}
        self.event_bus.publish(
            Event(
                event_type=EventType.MOMENT_FEEDBACK,
                source="interface",
                payload={"moment": self._last_moment, "outcome": outcome},
            )
        )
        return {"ok": True, "outcome": outcome}

    def _on_presence_error(self, exc: Exception) -> None:
        self._errors += 1
        _log.exception("interface presence handler failed")
        self.event_bus.publish(
            system_error_event(module="interface", exception=str(exc), severity="handler")
        )

    def _deliver_to_desktop(self, text: str, node_ids: list[str]) -> None:
        """V-12 · schedule one desktop popup. Never blocks the handler, never
        raises: the spawn runs as its own task. Rate-limited and one-in-flight,
        so a burst of intents cannot flood the screen — the excess is still in
        the terminal queue."""
        if not self.config.notify_desktop:
            return
        now = self._clock.monotonic()
        busy = self._desktop_task is not None and not self._desktop_task.done()
        if busy or now - self._desktop_last_at < _DESKTOP_MIN_GAP_SECONDS:
            self._desktop_skipped += 1
            return
        self._desktop_last_at = now
        self._desktop_task = asyncio.create_task(self._send_desktop(self._readable(text, node_ids)))

    def _readable(self, text: str, node_ids: list[str]) -> str:
        """Put real names where L7 wrote node ids. A popup reading
        `app:brave is under corroborated pressure` is exactly the raw-id label
        B18 removed from the graph; the ids are in the intent, so name them."""
        for node_id in sorted(set(node_ids), key=len, reverse=True):
            if node_id and self._graph.has_node(node_id):
                text = text.replace(node_id, self._graph.display_name(node_id))
        return text

    async def _send_desktop(self, body: str) -> None:
        try:
            ok = await desktop.notify(desktop.APP_NAME, body)
        except Exception:  # a delivery failure is logged, never raised (rules.md §2)
            _log.exception("desktop notification failed")
            ok = False
        if ok:
            self._desktop_sent += 1
        else:
            self._desktop_failed += 1

    async def on_action_triggered(self, event: Event) -> None:
        """L7 finished an attempt. Two jobs:

        1. retire the confirmation prompt, if this attempt had one — L7 is no
           longer waiting, so the human must stop being asked;
        2. turn a *notification intent* into a line the human can read. Every
           other action result is left to the audit log — L9 does not narrate
           the daemon's housekeeping."""
        try:
            confirmation_id = str(event.payload.get("confirmation_id", ""))
            if confirmation_id:
                self._pending_confirmations.pop(confirmation_id, None)
            intent = event.payload.get("intent")
            if not isinstance(intent, dict) or intent.get("kind") != "notification":
                return
            text = str(intent.get("text", "")).strip()
            if not text or not bool(event.payload.get("ok")):
                return
            self._pending_notifications.append(
                {
                    "text": text,
                    "reason": str(intent.get("reason", "")),
                    "node_ids": list(intent.get("node_ids", [])),
                    "dry_run": bool(event.payload.get("dry_run")),
                    "at": self._clock.now().isoformat(),
                }
            )
            if len(self._pending_notifications) > _MAX_PENDING_NOTIFICATIONS:
                # Bounded: an undrained queue must not grow without limit. The
                # audit log is the complete record; this is only the tail.
                del self._pending_notifications[:-_MAX_PENDING_NOTIFICATIONS]
            # V-12 · a live intent also reaches the desktop. Never a dry-run one:
            # "would have told you" on screen is an effect, and dry-run causes
            # none. A1's pause also blocks it — still queued above, so
            # `neuropaca notifications` still shows it, but the popup is
            # silenced until `paused_until` (VISION_PHASES.md's exit criterion:
            # "pause silences every moment until it expires").
            if not bool(event.payload.get("dry_run")) and not self._is_paused(self._clock.now()):
                self._deliver_to_desktop(text, [str(n) for n in intent.get("node_ids", [])])
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._errors += 1
            _log.exception("interface on_action_triggered failed")
            self.event_bus.publish(
                system_error_event(module="interface", exception=str(exc), severity="handler")
            )

    async def on_confirmation_request(self, event: Event) -> None:
        """L7 has paused a dangerous action and is waiting on a human. Hold the
        prompt until a terminal asks for it — L9 never answers on its own."""
        try:
            request_id = str(event.payload.get("request_id", ""))
            if not request_id:
                return
            self._pending_confirmations[request_id] = {
                "request_id": request_id,
                "action": str(event.payload.get("action", "")),
                "tier": str(event.payload.get("tier", "")),
                "summary": str(event.payload.get("summary", "")),
                "reason": str(event.payload.get("reason", "")),
                "requested_at": str(event.payload.get("requested_at", "")),
            }
            _log.warning("L9 holding confirmation %s for the user", request_id)
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._errors += 1
            _log.exception("interface on_confirmation_request failed")
            self.event_bus.publish(
                system_error_event(module="interface", exception=str(exc), severity="handler")
            )

    def _expire_stale_confirmations(self) -> None:
        """Belt and braces behind the `confirmation_id` retirement above: if L7's
        completion event were ever lost, a prompt older than the timeout is one
        nobody can still be waiting on. Showing it would invite an approval that
        silently goes nowhere."""
        grace = float(self.config.action_confirmation_timeout_seconds) + _CONFIRMATION_GRACE
        now = self._clock.now()
        for request_id, pending in list(self._pending_confirmations.items()):
            raw = str(pending.get("requested_at", ""))
            try:
                requested_at = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if (now - requested_at).total_seconds() > grace:
                self._pending_confirmations.pop(request_id, None)

    def _answer_confirmation(self, request_id: str, approved: bool) -> dict[str, Any]:
        """Relay one human verdict to L7. Unknown or already-answered ids are
        rejected rather than published — an approval must correspond to a prompt
        that is actually outstanding."""
        self._expire_stale_confirmations()
        if request_id not in self._pending_confirmations:
            return {"ok": False, "error": f"no confirmation is waiting with id {request_id!r}"}
        pending = self._pending_confirmations.pop(request_id)
        self.event_bus.publish(
            Event(
                event_type=EventType.ACTION_CONFIRMATION_RESPONSE,
                source="interface",
                priority=10,
                payload={"request_id": request_id, "approved": approved},
            )
        )
        _log.warning(
            "L9 relayed confirmation %s: %s", request_id, "APPROVED" if approved else "DENIED"
        )
        return {
            "ok": True,
            "request_id": request_id,
            "approved": approved,
            "action": pending.get("action", ""),
        }

    # ------------------------------------------------------------ IPC server
    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while not reader.at_eof():
                try:
                    raw = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await self._write(writer, {"ok": False, "error": "request line too long"})
                    break
                if not raw:
                    break
                _log.debug("L9 <- %s", redact(raw.decode("utf-8", "replace"), keep=12))
                try:
                    req = json.loads(raw)
                except ValueError:
                    await self._write(writer, {"ok": False, "error": "malformed JSON"})
                    continue
                resp = await self._route(req if isinstance(req, dict) else {})
                await self._write(writer, resp)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:  # never let a client crash the server (rules.md §2)
            self._errors += 1
            _log.exception("L9 client handler failed")
            self.event_bus.publish(
                system_error_event(module="interface", exception=str(exc), severity="ipc")
            )
        finally:
            with contextlib.suppress(OSError):
                writer.close()
                await writer.wait_closed()

    async def _write(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, default=str) + "\n"
        _log.debug("L9 -> %s", redact(line, keep=12))
        writer.write(line.encode("utf-8"))
        await writer.drain()

    async def _route(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        if op == "health":
            health = await self._request_health()
            if health is None:
                return {"ok": False, "error": "health request timed out"}
            return {"ok": True, "health": health}
        if op == "insights":
            drained = [self._insight_line(i) for i in self._pending_insights]
            self._pending_insights.clear()
            return {"ok": True, "insights": drained}
        if op == "notifications":
            drained = list(self._pending_notifications)
            self._pending_notifications.clear()
            return {"ok": True, "notifications": drained}
        if op == "confirmations":
            self._expire_stale_confirmations()
            return {"ok": True, "confirmations": list(self._pending_confirmations.values())}
        if op == "confirm":
            request_id = str(req.get("request_id", ""))
            return self._answer_confirmation(request_id, bool(req.get("approved", False)))
        if op == "run":
            self._queries += 1
            prefix = "$$" if req.get("backup") else "$!"
            return self._relay_command(prefix, str(req.get("cmd", "")).strip())
        if op == "explain":
            self._queries += 1
            return await self._explain(
                str(req.get("target", "")).strip(), str(req.get("summary", "")).strip()
            )
        if op == "briefing":
            self._queries += 1
            return await self._request_briefing()
        if op == "mirror":
            self._queries += 1
            return await self._request_mirror()
        if op == "reload-graph":
            return await self._reload_graph()
        if op == "presence":
            return self._presence_payload()
        if op == "pause":
            return self._pause(float(req.get("minutes", 60.0)))
        if op == "feedback":
            return self._submit_feedback(str(req.get("outcome", "")))
        return {"ok": False, "error": f"unknown op: {op!r}"}

    async def _reload_graph(self) -> dict[str, Any]:
        """S0 · pick up a graph `neuropaca repair-graph` just rebuilt, without
        a restart. `GraphMemory.load()` already fully replaces `self._graph`
        in place — BL-2's boot-recovery path re-enters it for that exact
        reason — so hot-reloading the live singleton is calling it again,
        nothing new. `self._graph` is the same object every other module in
        this process holds too; a mutation-in-place is visible to all of them
        the instant the lock releases, no restart, no re-wiring."""
        try:
            await self._graph.load()
        except GraphMemoryError as exc:
            return {"ok": False, "error": f"reload failed: {exc}"}
        return {"ok": True, "nodes": self._graph.node_count, "edges": self._graph.edge_count}

    async def _request_health(self) -> dict[str, Any] | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any] | None] = loop.create_future()
        self._health_waiters.append(fut)
        self.event_bus.publish(
            Event(event_type=EventType.SYSTEM_HEALTH_REQUEST, source="interface")
        )
        try:
            return await asyncio.wait_for(fut, _HEALTH_TIMEOUT)
        except TimeoutError:
            return None
        finally:
            if fut in self._health_waiters:
                self._health_waiters.remove(fut)

    async def _request_briefing(self) -> dict[str, Any]:
        """S0's on-demand path (`neuropaca briefing`). `None` (no live
        `BriefingComposer` — episodes disabled) reads the same as a timeout to
        the caller: nothing to brief right now."""
        loop = asyncio.get_running_loop()
        request_id = uuid4().hex[:12]
        fut: asyncio.Future[dict[str, Any] | None] = loop.create_future()
        self._briefing_waiters[request_id] = fut
        self.event_bus.publish(
            Event(
                event_type=EventType.BRIEFING_REQUEST,
                source="interface",
                payload={"request_id": request_id},
            )
        )
        try:
            moment = await asyncio.wait_for(fut, _BRIEFING_TIMEOUT)
        except TimeoutError:
            return {"ok": False, "error": "briefing request timed out"}
        finally:
            self._briefing_waiters.pop(request_id, None)
        return {"ok": True, "moment": moment}

    async def _request_mirror(self) -> dict[str, Any]:
        """A2's on-demand path (`neuropaca mirror`). `None` (no live
        `MirrorComposer` — episodes disabled, or nothing surprising today)
        reads the same as a timeout to the caller."""
        loop = asyncio.get_running_loop()
        request_id = uuid4().hex[:12]
        fut: asyncio.Future[dict[str, Any] | None] = loop.create_future()
        self._mirror_waiters[request_id] = fut
        self.event_bus.publish(
            Event(
                event_type=EventType.MIRROR_REQUEST,
                source="interface",
                payload={"request_id": request_id},
            )
        )
        try:
            moment = await asyncio.wait_for(fut, _MIRROR_TIMEOUT)
        except TimeoutError:
            return {"ok": False, "error": "mirror request timed out"}
        finally:
            self._mirror_waiters.pop(request_id, None)
        return {"ok": True, "moment": moment}

    # ------------------------------------------------------------ run relay (B7)
    def _relay_command(self, prefix: str, text: str) -> dict[str, Any]:
        """Hand a `neuropaca run` command to L7 over the bus. `prefix` is the
        internal wire enum L7 dispatches on — `$!` (run) or `$$` (run + state
        backup). L9 holds no gate of its own: it does not decide, back up,
        sandbox, or execute anything, and it does not pre-empt L7's refusal. An
        empty command is the one thing it can reject without guessing at L7's
        policy."""
        if not text:
            return {"ok": False, "error": "empty command", "prefix": prefix}
        self._store_message(MessageRole.USER, f"run {text}")
        self.event_bus.publish(
            Event(
                event_type=EventType.USER_MESSAGE,
                source="interface",
                priority=5,
                payload={"text": text, "prefix": prefix},
            )
        )
        if self.config.action_dry_run:
            note = "queued — the action layer is in dry-run, so nothing will be executed"
        else:
            note = (
                "queued — a dangerous action needs your confirmation: "
                "run `neuropaca confirmations`, then `neuropaca confirm <id>`"
            )
        return {"ok": True, "queued": True, "prefix": prefix, "note": note}

    # ------------------------------------------------------------ tell --explain
    async def _explain(self, target: str, summary: str) -> dict[str, Any]:
        """B12 · the one free-decode L9 call (rules.md §4.1 carve-out).

        `neuropaca tell <path>` already answered deterministically from the
        file's docstring and defs; this optional step asks the interactive model
        to paraphrase that summary in plain words. The model's **input** is a
        first-party summary the client generated from repo docstrings — never
        model-chosen context, never user free text — and its **output** is
        bounded (`EXPLAIN_MAX_TOKENS`, wall clock, `clean_explain_answer`), never
        executed, never a path, never published, never stored. A timeout / empty
        result just drops the paraphrase; the deterministic block still stands.
        """
        if not summary:
            return {"ok": False, "error": "nothing to explain"}
        prompt = build_explain_prompt(target or "this file", summary)
        answer: str | None = None
        if await self._ensure_interactive_model():
            raw = await self._infer(prompt, temperature=self.config.explain_temperature)
            answer = clean_explain_answer(raw) if raw is not None else None
        if answer:
            return {"ok": True, "answer": answer, "source": "model", "confidence": 0.6}
        return {"ok": True, "answer": "", "source": "template-nomodel", "confidence": 0.0}

    async def _ensure_interactive_model(self) -> bool:
        if self._interactive_disabled or not self._runtime.interactive_configured:
            return False
        if self._runtime.interactive_loaded:
            return True
        loaded = await self._runtime.load_interactive_model_async()
        if not loaded:
            self._interactive_disabled = True
            _log.warning("L9 interactive model unavailable — `tell --explain` stays deterministic")
        return loaded

    async def _infer(self, prompt: str, *, temperature: float) -> str | None:
        try:
            return await asyncio.wait_for(
                self._runtime.infer_async(
                    prompt, EXPLAIN_MAX_TOKENS, temperature, None, interactive=True
                ),
                _EXPLAIN_INFER_TIMEOUT,
            )
        except TimeoutError:
            _log.warning("L9 interactive inference timed out after %ss", _EXPLAIN_INFER_TIMEOUT)
            return None
        except Exception:  # a model crash (context overflow, backend fault) → drop the paraphrase
            _log.exception("L9 interactive inference failed — dropping the paraphrase")
            return None

    # ------------------------------------------------------------ history / output
    def _store_message(
        self, role: MessageRole, content: str, related_node_ids: tuple[str, ...] = ()
    ) -> None:
        self._conversation_history.append(
            Message(role=role, content=content, related_node_ids=related_node_ids)
        )
        if len(self._conversation_history) > _MAX_HISTORY:
            del self._conversation_history[: len(self._conversation_history) - _MAX_HISTORY]

    def send_to_user(self, message: Message) -> None:
        """Record an outbound turn in RAM history. The wire delivery is the
        socket response in `_handle_client`; this exists for the blueprint
        contract and future channels."""
        self._store_message(message.role, message.content, message.related_node_ids)

    @staticmethod
    def _insight_line(insight: Insight) -> dict[str, Any]:
        return {
            "text": insight.summary,
            "category": insight.category,
            "cited": list(insight.cited_node_ids),
            "confidence": round(insight.confidence, 3),
        }

    @property
    def conversation_history(self) -> tuple[Message, ...]:
        return tuple(self._conversation_history)


# gen-ref: 490a1937
