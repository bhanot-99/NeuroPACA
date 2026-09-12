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

Deliberately not a `BaseModule` yet: nothing in S0 needs it to *react* to
events beyond what `EpisodicWriter` already logs — the trigger (first
activity of the day, or after `briefing_idle_gap_hours`) is decided by
whatever calls this once per trigger, kept here as `should_brief_now` so the
same rule drives both the daemon's proactive delivery and the on-demand
`neuropaca briefing` verb.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from neuropaca.core.config import Config
from neuropaca.core.episodes import EpisodeRecord, EpisodeStore
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Moment

_MOMENT_EXPIRES_MINUTES = 30


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


# gen-ref: 5e2b7c31
