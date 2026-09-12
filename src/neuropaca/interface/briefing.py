# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S0 · the briefing core — "what is waiting, what changed, where you left off"
(VISION_PHASES.md, §3.9).

`compose_briefing` is a pure function over `GraphMemory` + `EpisodeStore`: it
takes candidates (open threads, new insights since the last briefing),
ranks them by §3.4's attention blend, picks at most `k` with a greedy
submodular selection that penalises near-duplicates by neighbourhood Jaccard
similarity, and renders the result as a `Moment` every claim of which resolves
to graph or episode evidence.

`BriefingComposer(BaseModule)` is the daemon-side driver: it is the only
module that tracks focus history (the last few focused nodes, for §3.4's PPR
seeds) and decides *when* to call `compose_briefing` — on `should_brief_now`'s
rule, checked on every `APP_SWITCH` / `ACTIVITY_DETECTED`. Two paths out:
- **proactive** — publishes `MOMENT_PROPOSED`; A3's `Guardian` decides
  whether it is ever turned into an `ACTION_PROPOSAL` `notification`.
- **on demand** (`neuropaca briefing`) — L9 cannot import this module
  (rules.md §0), so it asks over the bus: `BRIEFING_REQUEST` in,
  `BRIEFING_REPORT` out, the same request/report shape as L9's own health
  bridge (`SYSTEM_HEALTH_REQUEST`/`_REPORT`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.episodes import EpisodeRecord, EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.models import Event, Moment, system_error_event
from neuropaca.diagnosis.app_identity import AppIdentity

_log = logging.getLogger(__name__)

_MOMENT_EXPIRES_MINUTES = 30
_FOCUS_HISTORY_DEPTH = 4  # current focus + the last three (§3.4's seed weighting)
_BRIEFING_REPORT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class BriefingItem:
    """One ranked candidate before selection. `anchor` is the graph node id
    its Jaccard-similarity neighbourhood is computed from (a subject id that
    may or may not itself be a live node — a candidate whose anchor is absent
    from the graph gets similarity 0 to everything, never an error)."""

    anchor: str
    text: str
    evidence: tuple[str, ...]
    value: float


def _jaccard(gm: GraphMemory, a: str, b: str) -> float:
    if a == b:
        return 1.0
    na = {n.id for n in gm.find_related(a, depth=1)}
    if gm.has_node(a):
        na.add(a)
    nb = {n.id for n in gm.find_related(b, depth=1)}
    if gm.has_node(b):
        nb.add(b)
    if not na or not nb:
        return 0.0
    union = len(na | nb)
    return len(na & nb) / union if union else 0.0


def select_greedy_submodular(
    candidates: list[BriefingItem], gm: GraphMemory, *, k: int, mu: float
) -> list[BriefingItem]:
    """§3.9: max sum r(i) - mu*sum_{i!=j} sim(i,j), greedy — within (1 - 1/e)
    of optimal for a monotone submodular objective (Nemhauser-Wolsey-Fisher
    1978). At each step, pick the remaining candidate with the highest
    marginal gain (its own value minus `mu` times its similarity to every item
    already chosen); stop at `k` or when the pool is empty."""
    selected: list[BriefingItem] = []
    pool = list(candidates)
    while pool and len(selected) < k:
        best_item: BriefingItem | None = None
        best_gain = float("-inf")
        for item in pool:
            penalty = mu * sum(_jaccard(gm, item.anchor, s.anchor) for s in selected)
            gain = item.value - penalty
            if gain > best_gain:
                best_gain, best_item = gain, item
        assert best_item is not None  # pool is non-empty in this branch
        selected.append(best_item)
        pool.remove(best_item)
    return selected


def _recency_seeds(focus_history: list[tuple[str, datetime]], *, now: datetime) -> dict[str, float]:
    """Seeds for `personalized_pagerank`: the current focus node and the last
    three, weighted by recency (§3.4) — newest first gets the most weight."""
    weights: dict[str, float] = {}
    for node_id, seen_at in focus_history[-4:]:
        age = max(0.0, (now - seen_at).total_seconds())
        weights[node_id] = weights.get(node_id, 0.0) + 1.0 / (1.0 + age / 3600.0)
    return weights


def _open_thread_candidates(
    gm: GraphMemory, rows: list[EpisodeRecord], *, now: datetime
) -> list[BriefingItem]:
    """The last focus span per subject that has not been revisited since — a
    proxy "open thread" until S2's real project state exists. A subject
    revisited more recently than `now` minus its own last span is not open."""
    last_by_subject: dict[str, EpisodeRecord] = {}
    for row in rows:
        if row.kind != "focus_span" or row.t_end is None:
            continue
        prior = last_by_subject.get(row.subject)
        if prior is None or row.t_end > prior.t_end:  # type: ignore[operator]
            last_by_subject[row.subject] = row

    items: list[BriefingItem] = []
    for subject, row in last_by_subject.items():
        node = gm.get_node(subject)
        if node is None or row.t_end is None:
            continue
        items.append(
            BriefingItem(
                anchor=subject,
                text=f"You were last in {gm.display_name(subject)}.",
                evidence=(subject,),
                value=0.0,  # filled in by retrieval_scores below
            )
        )
    return items


def _insight_candidates(gm: GraphMemory, rows: list[EpisodeRecord]) -> list[BriefingItem]:
    items: list[BriefingItem] = []
    for row in rows:
        if row.kind != "insight":
            continue
        label = str(row.attrs.get("label", "")).strip()
        if not label or not gm.has_node(row.subject):
            continue
        items.append(
            BriefingItem(
                anchor=row.subject,
                text=f"While you were away: {label}",
                evidence=(row.subject,),
                value=0.0,
            )
        )
    return items


async def build_candidates(
    gm: GraphMemory,
    store: EpisodeStore,
    *,
    now: datetime,
    last_briefing_seq: int,
    lookback: timedelta = timedelta(days=7),
) -> list[BriefingItem]:
    """Everything the briefing might say, unranked and unfiltered: open threads
    (from recent focus spans) and insights since the last briefing."""
    recent = await store.between(now - lookback, now)
    since_last = await store.since(last_briefing_seq)
    return [*_open_thread_candidates(gm, recent, now=now), *_insight_candidates(gm, since_last)]


def rank_candidates(
    gm: GraphMemory,
    candidates: list[BriefingItem],
    *,
    focus_history: list[tuple[str, datetime]],
    now: datetime,
    config: Config,
) -> list[BriefingItem]:
    """§3.4's blend, applied to each candidate's own anchor node."""
    if not candidates:
        return []
    seeds = _recency_seeds(focus_history, now=now)
    scores = gm.retrieval_scores(
        seeds,
        [c.anchor for c in candidates],
        now=now,
        alpha=config.attention_alpha,
        beta=config.attention_beta,
        gamma=config.attention_gamma,
        recency_half_life_seconds=config.attention_recency_half_life_seconds,
        eps=config.attention_ppr_eps,
        ppr_alpha=config.attention_ppr_alpha,
    )
    return [
        BriefingItem(
            anchor=c.anchor, text=c.text, evidence=c.evidence, value=scores.get(c.anchor, 0.0)
        )
        for c in candidates
    ]


async def compose_briefing(
    gm: GraphMemory,
    store: EpisodeStore,
    *,
    focus_history: list[tuple[str, datetime]],
    now: datetime,
    last_briefing_seq: int,
    config: Config,
) -> Moment | None:
    """The whole pipeline: candidates -> rank -> greedy submodular select ->
    render. `None` when there is nothing grounded to say (F1's grounding
    check — every candidate here already required a live graph node, so
    nothing ungrounded can reach this point)."""
    raw = await build_candidates(gm, store, now=now, last_briefing_seq=last_briefing_seq)
    ranked = rank_candidates(gm, raw, focus_history=focus_history, now=now, config=config)
    ranked = [item for item in ranked if item.value > 0.0]
    if not ranked:
        return None
    ranked.sort(key=lambda item: item.value, reverse=True)
    selected = select_greedy_submodular(
        ranked, gm, k=config.briefing_max_items, mu=config.briefing_similarity_mu
    )
    if not selected:
        return None

    text = " ".join(item.text for item in selected)
    evidence = tuple(dict.fromkeys(e for item in selected for e in item.evidence))
    return Moment(
        kind="briefing",
        text=text,
        evidence=evidence,
        value=sum(item.value for item in selected),
        context={"item_count": len(selected), "hour": now.hour},
        expires_at=now + timedelta(minutes=_MOMENT_EXPIRES_MINUTES),
    )


def should_brief_now(
    *,
    now: datetime,
    last_briefing_at: datetime | None,
    last_activity_gap_seconds: float,
    config: Config,
) -> bool:
    """Open question 4 (VISION.md §11): first activity of a calendar day, OR
    the first after `briefing_idle_gap_hours` of quiet — whichever fires
    first. `last_briefing_at is None` (never briefed) always fires."""
    if last_briefing_at is None:
        return True
    if now.date() != last_briefing_at.date():
        return True
    return last_activity_gap_seconds >= config.briefing_idle_gap_hours * 3600.0


class BriefingComposer(BaseModule):
    """The daemon-side driver over the pure `compose_briefing` pipeline above."""

    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        graph_memory: GraphMemory,
        episode_store: EpisodeStore,
        *,
        clock: Clock | None = None,
        identity: AppIdentity | None = None,
    ) -> None:
        super().__init__("briefing", event_bus, config)
        self._graph = graph_memory
        self._store = episode_store
        self._clock: Clock = clock or SystemClock()
        self._identity_path = config.app_identity_path
        self._identity = identity if identity is not None else AppIdentity.empty()
        self._focus_history: list[tuple[str, datetime]] = []
        self._last_event_at: datetime | None = None
        self._last_briefing_at: datetime | None = None
        self._last_briefing_seq = 0
        self._composed = 0
        self._nothing_to_say = 0
        self._errors = 0
        self._last_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        if self._identity.alias_count == 0:
            self._identity = AppIdentity.from_file(self._identity_path)
        self.event_bus.subscribe(EventType.APP_SWITCH, self.on_app_switch)
        self.event_bus.subscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.subscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)
        self.event_bus.subscribe(EventType.BRIEFING_REQUEST, self.on_briefing_request)

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.event_bus.unsubscribe(EventType.APP_SWITCH, self.on_app_switch)
        self.event_bus.unsubscribe(EventType.IDLE_DETECTED, self.on_idle_detected)
        self.event_bus.unsubscribe(EventType.ACTIVITY_DETECTED, self.on_activity_detected)
        self.event_bus.unsubscribe(EventType.BRIEFING_REQUEST, self.on_briefing_request)

    def health(self) -> ModuleHealth:
        return ModuleHealth(
            name=self.name,
            ok=self.is_running,
            detail=(
                f"{self._composed} composed · {self._nothing_to_say} nothing-to-say · "
                f"{self._errors} errors"
            ),
            last_event_at=self._last_at,
        )

    # --------------------------------------------------------- event handlers
    async def on_app_switch(self, event: Event) -> None:
        try:
            if not self.is_running:
                return
            now = self._clock.now()
            payload = event.payload
            webapp = payload.get("webapp")
            if isinstance(webapp, str) and webapp:
                subject: str | None = f"webapp:{webapp}"
            else:
                app_id = payload.get("app_id")
                subject = None
                if isinstance(app_id, str) and app_id:
                    key = self._identity.resolve(app_id) or app_id
                    subject = f"app:{key}"
            if subject is not None:
                self._focus_history.append((subject, now))
                del self._focus_history[:-_FOCUS_HISTORY_DEPTH]
            await self._maybe_brief(now)
            self._last_event_at = now
        except Exception as exc:  # a handler never raises (rules.md §2)
            self._on_error(exc)

    async def on_idle_detected(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            self._last_event_at = self._clock.now()
        except Exception as exc:
            self._on_error(exc)

    async def on_activity_detected(self, _event: Event) -> None:
        try:
            if not self.is_running:
                return
            now = self._clock.now()
            await self._maybe_brief(now)
            self._last_event_at = now
        except Exception as exc:
            self._on_error(exc)

    async def on_briefing_request(self, event: Event) -> None:
        """The on-demand path (`neuropaca briefing`): compose fresh, right
        now, from whatever focus history and evidence exist — never the stale
        text of whatever last fired proactively. Answers even when there is
        nothing to say, so L9 never times out waiting for a report that a
        `None` result would otherwise silently skip."""
        try:
            if not self.is_running:
                return
            request_id = event.payload.get("request_id")
            now = self._clock.now()
            moment = await compose_briefing(
                self._graph,
                self._store,
                focus_history=self._focus_history,
                now=now,
                last_briefing_seq=0,  # on demand: consider every insight, not just new ones
                config=self.config,
            )
            self.event_bus.publish(
                Event(
                    event_type=EventType.BRIEFING_REPORT,
                    source="briefing",
                    payload={"request_id": request_id, "moment": moment},
                )
            )
        except Exception as exc:
            self._on_error(exc)

    # --------------------------------------------------------------- compose
    async def _maybe_brief(self, now: datetime) -> None:
        gap = (now - self._last_event_at).total_seconds() if self._last_event_at else 0.0
        if not should_brief_now(
            now=now,
            last_briefing_at=self._last_briefing_at,
            last_activity_gap_seconds=gap,
            config=self.config,
        ):
            return
        # One attempt per trigger, whether or not it finds anything to say —
        # otherwise a quiet morning re-checks on every single event all day.
        self._last_briefing_at = now
        moment = await compose_briefing(
            self._graph,
            self._store,
            focus_history=self._focus_history,
            now=now,
            last_briefing_seq=self._last_briefing_seq,
            config=self.config,
        )
        newest = await self._store.since(self._last_briefing_seq)
        if newest:
            self._last_briefing_seq = max(row.episode_seq for row in newest)
        if moment is None:
            self._nothing_to_say += 1
            return
        self._composed += 1
        self._last_at = now
        self.event_bus.publish(
            Event(
                event_type=EventType.MOMENT_PROPOSED, source="briefing", payload={"moment": moment}
            )
        )

    def _on_error(self, exc: Exception) -> None:
        self._errors += 1
        _log.exception("briefing handler failed")
        self.event_bus.publish(
            system_error_event(module="briefing", exception=str(exc), severity="handler")
        )


# gen-ref: 5e2b7c31
