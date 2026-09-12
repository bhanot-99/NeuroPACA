# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A2 · curiosity — what to wonder about (VISION_PHASES.md §3.7).

Treats each candidate association `(u, v)` as a `Beta(1 + s, 1 + f)` posterior
over "these belong together", where `s` is how often the episode log shows
them co-occurring within the coactivation window and `f` is how often one
occurred without the other. The DMN wonders about the pair whose *one more
observation* would teach it the most — the epistemic term of active
inference's expected free energy, closed form via the Beta-Bernoulli
predictive (no sampling, no scipy — `math.lgamma` is stdlib; the one missing
piece, the digamma function, is the standard asymptotic-series
implementation every stats library uses, written out here rather than adding
a dependency for one function).

Everything in this file is pure — no `EventBus`, no socket, no graph lock —
so the DMN (or a test) supplies whatever `EpisodeRecord`s it already has.
"""

from __future__ import annotations

import math
from datetime import datetime

from neuropaca.core.episodes import EpisodeRecord


def _digamma(x: float) -> float:
    """ψ(x), the standard recurrence + asymptotic-series implementation
    (accurate to double precision for x > 0) — not a dependency, a formula."""
    result = 0.0
    while x < 6.0:
        result -= 1.0 / x
        x += 1.0
    f = 1.0 / (x * x)
    result += math.log(x) - 0.5 / x - f * (
        1.0 / 12.0
        - f * (1.0 / 120.0 - f * (1.0 / 252.0 - f * (1.0 / 240.0 - f * (1.0 / 132.0))))
    )
    return result


def beta_entropy(a: float, b: float) -> float:
    """Differential entropy of `Beta(a, b)`."""
    return (
        math.lgamma(a)
        + math.lgamma(b)
        - math.lgamma(a + b)
        - (a - 1.0) * _digamma(a)
        - (b - 1.0) * _digamma(b)
        + (a + b - 2.0) * _digamma(a + b)
    )


def information_gain(s: int, f: int) -> float:
    """§3.7: `IG(u,v) = H[p(w)] - E_y H[p(w|y)]` for a `Beta(1+s, 1+f)`
    posterior, closed form — no sampling. `y` is the next observation (does
    the pair co-occur again, or not), weighted by the posterior predictive
    `P(y=1) = a/(a+b)`. Clamped at 0: the true quantity is always
    non-negative, but float subtraction of two close entropies can drift
    a shade negative right at the point of maximum certainty."""
    a, b = 1.0 + s, 1.0 + f
    h_now = beta_entropy(a, b)
    p1 = a / (a + b)
    p0 = 1.0 - p1
    h_after = p1 * beta_entropy(a + 1.0, b) + p0 * beta_entropy(a, b + 1.0)
    return max(0.0, h_now - h_after)


def association_evidence(
    rows: list[EpisodeRecord], u: str, v: str, window_seconds: float
) -> tuple[int, int]:
    """`(s, f)` for the pair `(u, v)` from `focus_span` episodes only: walking
    chronologically, every occurrence of *either* `u` or `v` checks whether
    the other was active within `window_seconds` beforehand — a co-use (`s`)
    if so, "one without the other" (`f`) if not. Symmetric by construction:
    `association_evidence(rows, u, v, w) == association_evidence(rows, v, u, w)`."""
    relevant = sorted(
        (
            r
            for r in rows
            if r.kind == "focus_span" and r.subject in (u, v) and r.t_start is not None
        ),
        key=lambda r: r.t_start,  # type: ignore[arg-type,return-value]
    )
    s = f = 0
    last_u: datetime | None = None
    last_v: datetime | None = None
    for row in relevant:
        t = row.t_start
        assert t is not None
        if row.subject == u:
            if last_v is not None and (t - last_v).total_seconds() < window_seconds:
                s += 1
            else:
                f += 1
            last_u = t
        else:
            if last_u is not None and (t - last_u).total_seconds() < window_seconds:
                s += 1
            else:
                f += 1
            last_v = t
    return s, f


def top_information_gain_pairs(
    rows: list[EpisodeRecord],
    candidate_ids: list[str],
    *,
    window_seconds: float,
    top_n: int,
) -> list[tuple[str, str, float]]:
    """The `top_n` pairs among `candidate_ids` with the highest information
    gain, highest first — computed for *this* candidate set only (S0's
    attention pool), never the whole graph (§3.7's "not about what it
    already knows, nor about noise" — a random pair from the whole graph is
    usually neither)."""
    scored: list[tuple[str, str, float]] = []
    for i, u in enumerate(candidate_ids):
        for v in candidate_ids[i + 1 :]:
            s, f = association_evidence(rows, u, v, window_seconds)
            scored.append((u, v, information_gain(s, f)))
    scored.sort(key=lambda t: t[2], reverse=True)
    return scored[:top_n]
