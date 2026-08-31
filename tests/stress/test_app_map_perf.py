"""Stress · AppMap classification throughput — the zero-inference fast path (D-10).

Every `APP_SWITCH` is classified synchronously on the event loop before any
pattern runs (`SignalCorrelator.on_app_switch`). `rules.md §4.1` forbids a model
call there, so the lookup has to be pure table work. This drives 100 000
`classify()` calls against the shipped `data/app_map.default.toml` and proves:

- **O(1) exact path** — 100 000 pure `app_id` dict hits in well under the 50 ms
  design target (warm dev machine: ~15 ms).
- **O(k) glob fallback** — `fnmatch`-compiled-to-regex, k = rule count; per-lookup
  cost stays ~1 µs even when every lookup falls all the way through to a miss.
- **realistic mixed stream** (80 % known app / 15 % path-glob / 5 % miss) stays
  inside a generous CI budget; warm-machine figure ~40 ms.

Budgets follow the house style (`test_l3_throughput.py`): a tight micro-budget
that actually encodes the complexity claim, plus a wall-clock bound with CI
headroom over the warm number.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from neuropaca.diagnosis.app_map import AppMap

pytestmark = pytest.mark.stress

_LOOKUPS = 100_000
_EXACT_BUDGET_MS = 50.0  # the spec's design target — the O(1) path clears it ~3x
_GLOB_PER_LOOKUP_US = 3.0  # O(k), k ~= 7 rules; warm ~1.1 µs
_MIXED_BUDGET_MS = 120.0  # CI headroom over the ~40 ms warm mixed run
_RULES = Path(__file__).resolve().parents[2] / "data" / "app_map.default.toml"

_EXACT = (
    "dev.zed.Zed",
    "md.obsidian.Obsidian",
    "com.slack.Slack",
    "us.zoom.Zoom",
    "org.gnome.Nautilus",
    "com.spotify.Client",
)
_GLOB_PATHS = (
    "/home/u/proj/src/train.py",
    "/home/u/papers/attention.pdf",
    "/home/u/notes/idea.md",
    "/home/u/work/tests/test_z.py",
)
_MISSES = ("org.unknown.Thing", "weird-wm-class", "aa.bb.Cc")


@pytest.fixture(scope="module")
def app_map() -> AppMap:
    m = AppMap.from_file(_RULES)
    assert m.rule_count > 0, "shipped app_map.default.toml parsed to nothing"
    return m


def test_exact_app_id_path_is_o1(app_map: AppMap) -> None:
    rng = random.Random(20260831)
    plan = [rng.choice(_EXACT) for _ in range(_LOOKUPS)]

    start = time.perf_counter()
    hits = sum(app_map.classify(app_id) is not None for app_id in plan)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert hits == _LOOKUPS  # every exact key resolves
    assert elapsed_ms < _EXACT_BUDGET_MS, (
        f"{_LOOKUPS} exact lookups took {elapsed_ms:.1f} ms "
        f"(design target {_EXACT_BUDGET_MS:.0f} ms) — the dict path is not O(1)"
    )


def test_glob_fallback_path_is_cheap_per_lookup(app_map: AppMap) -> None:
    rng = random.Random(20260901)
    # every call misses app_id + wm_class and walks the full compiled-glob list
    plan = [rng.choice(_GLOB_PATHS) for _ in range(_LOOKUPS)]

    start = time.perf_counter()
    for path in plan:
        app_map.classify("no.exact.match", path=path)
    per_lookup_us = (time.perf_counter() - start) / _LOOKUPS * 1e6

    assert per_lookup_us < _GLOB_PER_LOOKUP_US, (
        f"glob fallback is {per_lookup_us:.2f} µs/lookup "
        f"(budget {_GLOB_PER_LOOKUP_US} µs) — O(k) walk got expensive"
    )


def test_realistic_mixed_stream_stays_in_budget(app_map: AppMap) -> None:
    rng = random.Random(20260902)
    plan: list[tuple[str, str | None]] = []
    for _ in range(_LOOKUPS):
        r = rng.random()
        if r < 0.80:
            plan.append((rng.choice(_EXACT), None))
        elif r < 0.95:
            plan.append(("no.exact.match", rng.choice(_GLOB_PATHS)))
        else:
            plan.append((rng.choice(_MISSES), None))

    hits = 0
    start = time.perf_counter()
    for app_id, path in plan:
        if app_map.classify(app_id, path=path) is not None:
            hits += 1
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert 0 < hits < _LOOKUPS  # the mix really classifies and really misses
    assert elapsed_ms < _MIXED_BUDGET_MS, (
        f"{_LOOKUPS} mixed lookups took {elapsed_ms:.1f} ms (budget {_MIXED_BUDGET_MS:.0f} ms)"
    )
