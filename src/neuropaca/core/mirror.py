# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A2 · the mirror — what changed today (VISION_PHASES.md §3.8, VISION.md §3.8).

Compares today's `(app, hour)` time distribution `Q` against a learned
baseline `P` — an exponentially-weighted average of the trailing
`mirror_baseline_days`, days on today's weekday up-weighted (a Thursday's
baseline should lean on other Thursdays). Dirichlet(+1) smoothing keeps every
bucket either distribution touches strictly positive, so the KL divergence

    D_KL(Q || P) = sum_i Q_i * log(Q_i / P_i)

never divides by zero. Above a threshold `tau`, the day is "surprising" and
the top contributors — the buckets with the largest `|Q_i * log(Q_i/P_i)|` —
become the day's sentences; a bucket present in the baseline but absent today
(a `Q_i` of exactly 0 contributes 0 to the sum, by convention, so it cannot
surface as a "top contributor") is instead the top *missing* contributor,
ranked by baseline mass alone — "you didn't open Obsidian today".

Pure functions only — no `EventBus`, no graph, no episode-store I/O. The
caller (`MirrorComposer`, or a test) supplies whatever `EpisodeRecord`s it
already fetched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from neuropaca.core.enums import EpisodeKind
from neuropaca.core.episodes import EpisodeRecord

Bucket = tuple[str, int]  # (subject, hour-of-day 0-23)


def bucket_seconds(rows: list[EpisodeRecord], t0: datetime, t1: datetime) -> dict[Bucket, float]:
    """Seconds of each `focus_span` clipped to `[t0, t1)`, bucketed by
    `(subject, hour of the clipped start)`. A span is not split across an hour
    boundary — bucketed by its start hour, the same simplification a calendar
    already makes ("the 14:00 slot", not spread across 14:00-14:55)."""
    out: dict[Bucket, float] = {}
    for row in rows:
        if row.kind != str(EpisodeKind.FOCUS_SPAN) or row.t_start is None or row.t_end is None:
            continue
        start = max(row.t_start, t0)
        end = min(row.t_end, t1)
        if end <= start:
            continue
        bucket = (row.subject, start.hour)
        out[bucket] = out.get(bucket, 0.0) + (end - start).total_seconds()
    return out


def normalize(counts: dict[Bucket, float]) -> dict[Bucket, float]:
    """Raw mass -> a probability distribution. Empty (or all-zero) input maps
    to an empty distribution, never a divide-by-zero."""
    total = sum(counts.values())
    if total <= 0.0:
        return {}
    return {bucket: mass / total for bucket, mass in counts.items()}


def daily_distribution(rows: list[EpisodeRecord], day_start: datetime) -> dict[Bucket, float]:
    """`Q`: today's `(app, hour)` distribution, `day_start` to `day_start + 1 day`."""
    return normalize(bucket_seconds(rows, day_start, day_start + timedelta(days=1)))


def baseline_distribution(
    rows: list[EpisodeRecord],
    day_start: datetime,
    *,
    lookback_days: int,
    half_life_days: float,
    same_weekday_boost: float,
) -> dict[Bucket, float]:
    """`P`: an exponentially-weighted average of each of the `lookback_days`
    days before `day_start` — a day `age` days back is weighted
    `0.5 ** (age / half_life_days)` (nearer days count for more), multiplied
    by `same_weekday_boost` when that day fell on the same weekday as the one
    being judged."""
    weighted: dict[Bucket, float] = {}
    for age in range(1, lookback_days + 1):
        window_start = day_start - timedelta(days=age)
        counts = bucket_seconds(rows, window_start, window_start + timedelta(days=1))
        if not counts:
            continue
        weight = 0.5 ** (age / half_life_days)
        if window_start.weekday() == day_start.weekday():
            weight *= same_weekday_boost
        for bucket, seconds in counts.items():
            weighted[bucket] = weighted.get(bucket, 0.0) + seconds * weight
    return normalize(weighted)


def smooth(
    q: dict[Bucket, float], p: dict[Bucket, float]
) -> tuple[dict[Bucket, float], dict[Bucket, float]]:
    """Dirichlet(+1) smoothing over the union of buckets either distribution
    touches. `kl_divergence` divides by `P_i` — an unsmoothed `P_i` of 0 for a
    bucket `Q` visited would be a divide-by-zero, not a rare shift."""
    support = set(q) | set(p)
    if not support:
        return {}, {}
    n = float(len(support))
    q_total = sum(q.values()) + n
    p_total = sum(p.values()) + n
    q_smoothed = {bucket: (q.get(bucket, 0.0) + 1.0) / q_total for bucket in support}
    p_smoothed = {bucket: (p.get(bucket, 0.0) + 1.0) / p_total for bucket in support}
    return q_smoothed, p_smoothed


def kl_divergence(q: dict[Bucket, float], p: dict[Bucket, float]) -> float:
    """`D_KL(Q || P)`, over `Q`'s support. Callers pass smoothed distributions
    (every bucket in `q` is also in `p`, both strictly positive) — an
    unsmoothed pair with a bucket only `Q` touches would `KeyError` here,
    deliberately: that is exactly the case `smooth()` exists to prevent."""
    return sum(qi * math.log(qi / p[bucket]) for bucket, qi in q.items())


def top_contributors(
    q: dict[Bucket, float], p: dict[Bucket, float], top_n: int
) -> list[tuple[Bucket, float]]:
    """The `top_n` buckets with the largest `|Q_i * log(Q_i/P_i)|` — the
    biggest swings either direction, "more than usual" and "less than usual"
    alike. Ranked, so the sum of *all* of them (not just the top `n`) equals
    `kl_divergence(q, p)` exactly."""
    scored = [(bucket, qi * math.log(qi / p[bucket])) for bucket, qi in q.items()]
    scored.sort(key=lambda t: abs(t[1]), reverse=True)
    return scored[:top_n]


def missing_contributors(
    q_raw: dict[Bucket, float], p_raw: dict[Bucket, float], top_n: int
) -> list[tuple[Bucket, float]]:
    """The `top_n` buckets with substantial baseline mass but none at all
    today — "you didn't open Obsidian today". A `Q_i` of exactly 0 makes
    `Q_i * log(Q_i/P_i)` vanish (by convention), so `top_contributors` alone
    can never surface this half of §3.8's examples; unsmoothed (`_raw`)
    distributions, so "none today" means the literal absence, not a smoothed
    fraction of a bucket."""
    scored = [(bucket, mass) for bucket, mass in p_raw.items() if bucket not in q_raw]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:top_n]


@dataclass(frozen=True, slots=True)
class MirrorResult:
    """The whole pipeline's output. `surprising` is `kl > tau` — the render
    layer (`MirrorComposer`) only turns `top`/`missing` into sentences when
    this is `True`; both are always computed here so a test can inspect them
    regardless."""

    kl: float
    surprising: bool
    top: list[tuple[Bucket, float]]
    missing: list[tuple[Bucket, float]]


def compute_mirror(
    rows: list[EpisodeRecord],
    day_start: datetime,
    *,
    baseline_days: int,
    half_life_days: float,
    same_weekday_boost: float,
    threshold: float,
    top_n: int,
) -> MirrorResult:
    """The full §3.8 pipeline. `day_start` to `day_start + 1 day` is `Q`; the
    `baseline_days` before it are `P`. No baseline history at all (day one of
    the daemon, or a quiet fortnight) means there is nothing to call
    "usual" — silent, never a spurious surprise on a distribution compared
    against itself."""
    q_raw = daily_distribution(rows, day_start)
    if not q_raw:
        return MirrorResult(kl=0.0, surprising=False, top=[], missing=[])
    p_raw = baseline_distribution(
        rows,
        day_start,
        lookback_days=baseline_days,
        half_life_days=half_life_days,
        same_weekday_boost=same_weekday_boost,
    )
    if not p_raw:
        return MirrorResult(kl=0.0, surprising=False, top=[], missing=[])
    q, p = smooth(q_raw, p_raw)
    kl = kl_divergence(q, p)
    surprising = kl > threshold
    if not surprising:
        return MirrorResult(kl=kl, surprising=False, top=[], missing=[])
    return MirrorResult(
        kl=kl,
        surprising=True,
        top=top_contributors(q, p, top_n),
        missing=missing_contributors(q_raw, p_raw, top_n),
    )
