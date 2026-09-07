"""L9 · `InterfaceLayer` — the only module that talks to the human
(Architecture.md §9, B5).

Shape (B5, re-scoped in B12):
- a **Unix-domain socket** at ``$XDG_RUNTIME_DIR/neuropaca.sock`` (JSONL framing:
  one JSON request per line, one JSON response per line). The thin CLI
  (`interface/cli.py`) is the only client. Ops: `health`, `insights`,
  `notifications`, `confirmations`, `confirm`, `run`, `explain`.
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
from datetime import date, datetime
from pathlib import Path
from typing import Any

from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, MessageRole, NodeType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.logging import redact
from neuropaca.core.models import Event, system_error_event
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
        self._pending_insights: list[Insight] = []
        self._pending_notifications: list[dict[str, Any]] = []
        self._pending_confirmations: dict[str, dict[str, Any]] = {}
        # dict, not set: insertion-ordered, so trimming drops the oldest ids.
        self._surfaced_ids: dict[str, None] = {}
        self._cap_day: date | None = None
        self._surfaced_today = 0
        self._queries = 0
        self._errors = 0
        self._interactive_disabled = False  # set once the interactive model is proven absent

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.event_bus.subscribe(EventType.INSIGHT_GENERATED, self.on_insight_generated)
        self.event_bus.subscribe(EventType.SYSTEM_HEALTH_REPORT, self._on_health_report)
        # B7 (D-14): L7 publishes intents and confirmation prompts; L9 is the
        # only module that may turn either into something a human sees.
        self.event_bus.subscribe(EventType.ACTION_TRIGGERED, self.on_action_triggered)
        self.event_bus.subscribe(
            EventType.ACTION_CONFIRMATION_REQUEST, self.on_confirmation_request
        )
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
        self.event_bus.unsubscribe(EventType.ACTION_TRIGGERED, self.on_action_triggered)
        self.event_bus.unsubscribe(
            EventType.ACTION_CONFIRMATION_REQUEST, self.on_confirmation_request
        )
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
        return {"ok": False, "error": f"unknown op: {op!r}"}

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
