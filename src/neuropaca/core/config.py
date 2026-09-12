# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""`Config` — loaded once at startup, immutable thereafter (Architecture.md §3.4).

`from_file()` reads TOML (`tomllib`, stdlib — no dependency). Every field is
validated in `__post_init__`; a bad value raises `ConfigError` and the daemon
refuses to start rather than run on a guess.

`inference_backend` (D-6): `"llama"` → the real llama.cpp backend, `"fake"` →
`FakeInferenceBackend` for tests. Validation is backend-aware — `model_path` is
only required to exist when the backend is `"llama"`.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from neuropaca.core.errors import ConfigError

_VALID_BACKENDS = frozenset({"llama", "fake"})
# B7 (D-14). The L7 action tiers. Mirrored by `action.base.ActionTier` — the enum
# lives in the layer that owns the behaviour, but `Config` cannot import L7 (that
# would invert the layering), so the closed set of *names* is spelled here, the
# same way `_VALID_BACKENDS` is. `action/base.py` asserts the two agree.
VALID_ACTION_TIERS = frozenset({"safe", "dangerous"})


@dataclass(frozen=True, slots=True)
class Config:
    """Daemon configuration. Frozen — nothing mutates it after startup (rules.md §3.4).

    Defaults are chosen so `Config(inference_backend="fake")` is a usable test
    fixture with no config file.
    """

    model_path: str = ""
    graph_db_path: str = "data/graph.json"
    action_log_path: str = "data/actions.jsonl"
    idle_threshold_seconds: int = 300
    # B7 · Drive (L5, D-14). The blueprint's single `pressure_threshold` is split
    # into the two gradient tiers of Architecture.md §7:
    #   - low  — one Diagnosis spike is enough; a *safe* action fires silently.
    #   - high — a *dangerous* action becomes permissible, and only with L3 and
    #     L4 corroborating inside the same window. Crossing it never executes
    #     anything on its own: the L7 gate still demands a confirmation.
    # `pressure_high_threshold > pressure_low_threshold` is validated below.
    pressure_low_threshold: float = 1.0
    pressure_high_threshold: float = 3.0
    # Exponential decay, expressed as a half-life so the constant is the thing
    # the exit criterion talks about: 60 s => exactly 50 %/min, and 10 min of
    # silence leaves 0.5**10 = 0.098 % — inside the "< 1 % within 10 min" bound.
    # `decay()` is applied on a timer AND lazily on read, so the value is exact
    # at any instant regardless of tick alignment.
    pressure_decay_half_life_seconds: int = 60
    pressure_decay_interval_seconds: int = 10
    # B8 · Agents (L8, D-16). `max_concurrent_agents` is load-bearing from here:
    # a spawn requested while that many agents are running is **refused and
    # logged**, never queued — an unbounded queue is how a load spike becomes a
    # thundering herd. The rest of the budgets:
    #   - agents_enabled — the kill switch, matching `activity_enabled`.
    #   - agent_wall_clock_budget_seconds — `asyncio.timeout` ceiling on one
    #     agent body; an overrun is cancelled and reported, never fatal.
    #   - agent_inference_budget — declared but **unspent in B8**: L8 ships
    #     structural plasticity only, because pressure crosses exactly when the
    #     box is busy and `rules.md §4` allows one inference system-wide. The
    #     field exists so a later phase cannot add inference without a budget.
    #   - agent_idle_ttl_days — apoptosis TTL for an ephemeral node (D-15's 14 d).
    #   - max_ephemeral_nodes — the hard cap `spawn_node()` checks **before**
    #     mutating (`rules.md §3`: an unbounded node-adding path is wrong).
    max_concurrent_agents: int = 2
    agents_enabled: bool = True
    agent_wall_clock_budget_seconds: int = 30
    agent_inference_budget: int = 1
    agent_idle_ttl_days: int = 14
    max_ephemeral_nodes: int = 50
    log_level: str = "INFO"
    # B9/BL-4 · the file sink `scripts/logrotate/neuropaca` rotates. journald
    # captures stderr under systemd, but the audit trail in data/ is useless
    # without the daemon log beside it when reconstructing an incident.
    log_to_file: bool = True
    log_file_path: str = "data/neuropaca.log"
    poll_intervals: dict[str, float] = field(
        default_factory=lambda: {"system": 60.0, "process": 60.0}
    )
    graph_save_interval_seconds: int = 300
    bitnet_max_tokens: int = 256
    # B4 · Learning (L4, D-11). model_context_tokens = llama.cpp n_ctx (the
    # extractive prompt is ~200 tokens, output <= 48 — 2048 is generous and keeps
    # the KV-cache small).
    model_context_tokens: int = 2048
    # B18 · L4's repeat gate. An insight fact (category/signal about a node)
    # reinforced within this window makes a new model call for the same
    # candidates pointless — skipped before inference. The check reads the graph,
    # so it survives restarts (the in-memory Jaccard buffer it replaces did not).
    insight_refractory_minutes: int = 360
    # T7 · Hebbian co-occurrence ("fire together, wire together", D-11). The
    # correlator wires activity nodes to `domain:` hubs, never to each other, so
    # `reinforce_cooccurrence` had no peer edges to strengthen and every weight
    # sat at 0.0. `wire_cooccurrence` now *creates* an `app:`/`webapp:` affinity
    # edge on co-activation and strengthens it on each recurrence; the B6 idle
    # sweep decays and prunes it so weight reads as a recency-weighted affinity.
    # V-1 reworked the rule: weights are a bounded affinity in [0, 1), the
    # focus switch wires star-shaped (new focus <-> each recent app, never the
    # recent apps among themselves), and decay follows elapsed time, not the
    # number of idle spells.
    #   - hebbian_delta — learning rate: each co-activation closes this fraction
    #     of the gap to 1.0 (w += rate * (1 - w)), scaled by how close in time.
    #   - hebbian_insight_multiplier — a model-confirmed episode (L4 insight)
    #     steps this many times harder than a bare focus co-activation.
    #   - coactivation_window_seconds — an app last active within this span of a
    #     new focus counts as co-active; credit falls linearly to 0 across it.
    #   - coactivation_max_nodes — cap on the correlator's co-activation deque
    #     (bounds the per-switch pair work).
    #   - hebbian_half_life_hours — an unused co-occurrence weight halves over
    #     this much daemon uptime ("use it or lose it").
    #   - hebbian_floor — a decayed co-occurrence edge below this is pruned
    #     (unless it is an endpoint's last edge — never re-orphan a node).
    #   - focus_exclude_app_ids — focused windows that are not an activity
    #     (dialogs, portals). Everything else focused joins the mesh, mapped in
    #     `app_map` or not.
    #   - coactivation_refractory_seconds — a pair strengthened this recently is
    #     not stepped again, so an alt-tab flurry (or a compositor glitch firing
    #     focus events) counts as one co-use and costs no graph lock. 0 disables.
    hebbian_delta: float = 0.1
    hebbian_insight_multiplier: float = 3.0
    coactivation_window_seconds: float = 300.0
    coactivation_max_nodes: int = 16
    coactivation_refractory_seconds: float = 30.0
    hebbian_half_life_hours: float = 72.0
    hebbian_floor: float = 0.02
    focus_exclude_app_ids: list[str] = field(
        default_factory=lambda: [
            "zenity",
            "xdg-desktop-portal-gtk",
            "xdg-desktop-portal-gnome",
            "xdg-desktop-portal-cosmic",
        ]
    )
    # B5 · dual-model routing (D-12), re-scoped in B12. The always-on loop
    # (L4/L6) uses the BitNet 2B4T model; the interactive model — a larger
    # Qwen2.5-3B-Instruct Q4 GGUF — is now used only by `neuropaca tell <path>
    # --explain`, which asks it to paraphrase the deterministic file summary in
    # plain words. Empty path => the interactive backend lazy-self-disables and
    # `tell --explain` just shows the deterministic block. Both models are
    # resident concurrently at peak (PRD §9); a single `_inference_lock` still
    # serialises every call system-wide (rules.md §4).
    interactive_model_path: str = ""
    # 2048 is generous: the explain prompt (system line + a clipped file summary)
    # is a few hundred tokens, the paraphrase <= EXPLAIN_MAX_TOKENS. A larger
    # n_ctx only inflates the interactive model's resident footprint (B5 finding).
    interactive_model_context_tokens: int = 2048
    # B6 · Idle Cognition (L6, D-13). Strict budgets on one DMN idle cycle:
    #   - dmn_cycle_wall_clock_seconds — `asyncio.timeout` ceiling for a whole
    #     cycle (reminiscence + imagination); an overrun is logged, not fatal.
    #   - dmn_max_inferences_per_cycle — at most this many idle-thought model
    #     calls per cycle; the DMN also bails the moment `BitNetRuntime.is_busy`.
    #   - dmn_idle_thought_ttl_hours — an `idle:` / `insight:` node past this age
    #     is pruned (the 48 h idle-thought cache lifetime, Architecture.md §8).
    #   - dmn_top_k — graph nodes offered to the model as one prompt's facts.
    #   - dmn_candidate_pool_k — V-4: how many top-scoring nodes the seed sample
    #     is drawn FROM. `dmn_top_k` was both the pool and the sample, so the
    #     argmax five nodes were the entire imagination for the life of the
    #     graph. Sampling `dmn_top_k` out of this pool (score-weighted, recent
    #     seeds penalised) costs one larger heap on the same O(N) scan — no new
    #     traversal, no extra inference — and lets imagination reach past the
    #     clique. Must be >= dmn_top_k; equal restores the old fixed behaviour.
    #   - dmn_seed_refractory_cycles — a node used as a seed is down-weighted
    #     for this many later cycles (0 disables the penalty).
    dmn_cycle_wall_clock_seconds: int = 60
    dmn_max_inferences_per_cycle: int = 3
    dmn_idle_thought_ttl_hours: int = 48
    dmn_top_k: int = 5
    dmn_candidate_pool_k: int = 24
    dmn_seed_refractory_cycles: int = 3
    # A2 · curiosity (VISION_PHASES.md §3.7). The seed choice becomes a mix:
    # with probability `1 - dmn_curiosity_epsilon` the pair among the candidate
    # pool with the highest expected information gain (§3.7's Beta-Bernoulli
    # posterior over `(u, v)` "belongs together"); with `dmn_curiosity_epsilon`
    # the existing V-4 score-weighted sample — kept as both the exploration term
    # and the fallback when there is no episode store, no history yet, or a read
    # fails. `dmn_curiosity_lookback_days` bounds the evidence query (a growing
    # log must not turn one idle cycle into an unbounded scan); `_top_pairs` is
    # how many of the candidate pool's C(k,2) pairs are ranked by IG and kept
    # as fallback candidates, most-uncertain first, before `_curious_seeds`
    # takes the highest one whose nodes are not entirely inside the DMN's own
    # V-4 refractory memory (`_recent_seeds`) — deliberately more than
    # `dmn_top_k` (default 8 vs 5), so that when the single top pair was just
    # used last cycle there is still somewhere else to go, rather than the
    # exact echo-chamber V-4's own refractory penalty already exists to avoid.
    dmn_curiosity_epsilon: float = 0.2
    dmn_curiosity_lookback_days: int = 14
    dmn_curiosity_top_pairs: int = 8
    # B7 · Action (L7, D-14). `action_dry_run` defaults **True**: the daemon
    # ships in the dry-run review period the B7 exit criteria require ("a review
    # period in dry-run with zero false positives before any tier goes live"),
    # so a fresh install cannot cause an effect. `action_enabled_tiers` gates
    # which tiers may run at all; "dangerous" additionally always needs a
    # recorded confirmation (rules.md §5.2 — no flag removes that).
    action_dry_run: bool = True
    action_enabled_tiers: list[str] = field(default_factory=lambda: ["safe"])
    # Nothing is ever deleted or overwritten in place (rules.md §5.7): the prior
    # bytes go to `quarantine_path` with a TTL and are swept only after it.
    quarantine_path: str = "data/quarantine"
    quarantine_ttl_hours: int = 168  # 7 days
    # `ApiCallAction` is the only component that would be allowed an outbound
    # socket (rules.md §5.5). B7 ships **no** such action — these two fields are
    # the reserved switches, and enabling the flag without an explicit allowlist
    # is a config error, never a silently-open socket.
    api_call_enabled: bool = False
    api_allowlist: list[str] = field(default_factory=list)
    # How long a paused dangerous action waits for the human. Expiry = refusal.
    action_confirmation_timeout_seconds: int = 60
    # V-12 · L9 hands a *live* notification intent to the desktop through the
    # session's `org.freedesktop.Notifications` server (the system `notify-send`,
    # argv only — rules.md §5.4). A dry-run intent is never delivered: that is
    # what dry-run means, and B7's review period depends on it — so with the
    # shipped `action_dry_run = True` nothing changes on screen. This is the
    # switch for the day actions go live; False keeps them terminal-only.
    notify_desktop: bool = True
    # A0 · the welcome-back moment (VISION_PHASES.md). `welcome_min_idle_minutes`
    # is the shortest idle spell worth greeting — below it, ACTIVITY_DETECTED's
    # own `idle_seconds` is used directly, no separate timer kept. Real-data spike
    # (2026-09-12 soak): `insights` stayed 0 across the whole sampled soak window,
    # so the "you were in {app}" half is the one that actually carries most
    # returns — the design already drops either half independently when it has
    # nothing grounded to say. `welcome_daily_cap` bounds how many fire per day
    # before A3's guardian exists to do that job properly.
    welcome_enabled: bool = True
    welcome_min_idle_minutes: int = 20
    welcome_daily_cap: int = 6
    # S0 · the episodic stream (VISION_PHASES.md §3.3-§3.9). `episodes_db_path`
    # is a separate sqlite file beside the graph, not a table inside it — the
    # spike's reason: the graph is one JSON document rewritten whole on every
    # save, sqlite is append-friendly under a 20k-event storm.
    # Off by default like every other brand-new S0-and-later subsystem
    # (`activity_enabled`, `raw_metrics_csv_path`) — existing installs, and
    # every test that builds a bare `Config()`, see no new module until this
    # is turned on.
    episodes_enabled: bool = False
    episodes_db_path: str = "data/episodes.sqlite"
    # Volume projection (RESEARCH_DOSSIER.md §21.15), from real numbers, not a
    # guess: the soak gate measured 39 switches/hour during active use
    # (`data/soak_gate_20260911T170701Z.log`); a measured `EpisodeStore` row
    # (`core/episodes.py`'s schema, two indexes) costs ~256 bytes on disk after
    # a WAL checkpoint. At ~8 active hours/day that is ~330 focus-span rows/day
    # -> ~85 KiB/day -> ~2.5 MiB/month — 90 days of retention is under 8 MiB,
    # nowhere near a real budget concern. `episode_retention_days` is therefore
    # generous, not tight; open question 2 in VISION.md §11 stays unresolved on
    # the *content* question (subjects+snippet vs full bodies for mail, S1),
    # not on episode volume.
    episode_retention_days: int = 90
    # §3.4's blend r(v) = alpha*pi(v) + beta*recency + gamma*score(v)/10, and the
    # Forward Push parameters pi is computed with. attention_recency_half_life_seconds
    # is the exponential half-life the recency term decays over.
    attention_alpha: float = 0.5
    attention_beta: float = 0.3
    attention_gamma: float = 0.2
    attention_recency_half_life_seconds: float = 86400.0
    attention_ppr_eps: float = 1e-4
    attention_ppr_alpha: float = 0.15
    # S0's briefing core (§3.9): k<=5 items, greedy submodular with a
    # redundancy penalty mu on neighbourhood-Jaccard similarity between two
    # candidates already selected.
    briefing_max_items: int = 5
    briefing_similarity_mu: float = 0.5
    briefing_idle_gap_hours: float = 6.0
    # A2 · the mirror (VISION_PHASES.md §3.8). Q = today's app x hour
    # distribution; P = an exponentially-weighted baseline over the trailing
    # `mirror_baseline_days`, same-weekday rows up-weighted by
    # `mirror_same_weekday_boost`; both Dirichlet(+1) smoothed before the KL
    # divergence. `mirror_kl_threshold` (tau) gates whether a day says anything
    # at all — the spike (§3.7's spike item 2) calls for picking it by replaying
    # two real weeks so an ordinary day stays silent; no such replay exists yet
    # (the live episode store is only hours old), so this starts at a reasoned
    # default and is the first thing to recalibrate once real days accumulate,
    # never a value to treat as validated. `mirror_evening_hour` is the
    # earliest hour a first-idle-after check may fire the day's mirror moment;
    # `mirror_top_contributors` bounds how many Q_i*log(Q_i/P_i) terms (and the
    # largest missing P_i) the rendered sentence names.
    mirror_enabled: bool = True
    mirror_baseline_days: int = 14
    # No half-life is specified in VISION.md §3.8 beyond "exponentially
    # weighted" — 7 days (half of the lookback) is the reasoned starting
    # point: a day one week old counts for half of yesterday, a day at the
    # 14-day edge for a quarter. Recalibrate once real days accumulate, same
    # as `mirror_kl_threshold` below.
    mirror_baseline_half_life_days: float = 7.0
    mirror_same_weekday_boost: float = 2.0
    mirror_kl_threshold: float = 0.5
    mirror_evening_hour: int = 18
    mirror_top_contributors: int = 3
    inference_backend: str = "llama"
    # Concept variant (Architecture.md §3.4).
    n_threads: int = 4
    max_failures: int = 3  # consecutive collect() failures before a collector self-disables (D-7)
    max_file_tokens: int = 4096
    # B2 · Sensing (L2, D-7). poll_intervals keys are collector names ("system",
    # "filesystem"). watch_paths empty => FileSystemCollector stays disabled.
    snapshot_buffer_size: int = 720
    # B3 · Diagnosis (L3, D-8). Bounds SignalCorrelator's per-collector snapshot
    # deques: maxlen = ceil(correlation_window_seconds / poll_intervals[name]) + 1.
    correlation_window_seconds: int = 1800
    # B2.5b · Process & Activity Sensing (D-10). app_map_path points at the
    # editable app_id/wm_class/path-glob -> domain rules file SignalCorrelator
    # loads at startup. A missing file is non-fatal — activity stays unclassified.
    app_map_path: str = "data/app_map.default.toml"
    # B17 (D-20) · app_identity_path points at the editable alias/non_app rules
    # file `SignalCorrelator` loads at startup so one real app is one `app:` node
    # regardless of whether the focus sensor (Wayland app_id) or the B13 census
    # (process name) saw it. A missing file is non-fatal — apps merge by
    # normalisation only.
    app_identity_path: str = "data/app_identity.default.toml"
    # B2.5 · Process & Activity Sensing (D-9). activity_enabled turns on the
    # Wayland ext-idle-notify ActivityCollector (needs `pip install .[activity]`);
    # when on, XMetricCollector stops emitting its CPU-derived idle stand-in.
    # top_process_count = top-N process-by-CPU rows in each system snapshot (0 = off).
    activity_enabled: bool = False
    top_process_count: int = 5
    # B13-B2 · ProcessCollector (D-19). A per-process RAM/CPU/runtime census
    # grouped by app name. On by default — RAM footprint is the stable "what is
    # this person working with" signal (Architecture.md §4). Names only, never
    # cmdline. `process_min_rss_mb` is the grouped-total threshold to census an
    # app (200 MB — an Electron renderer idles at 200-500 MB, D-19(f)).
    # `process_exclude_names` is the name-based exclusion list. Populated in B17
    # (D-20), soak-informed (D-19(c) kept it empty in round 1 so the operator
    # could see the raw census — it has been seen): the daemon's own footprint
    # and its tooling are not "an activity". Still a list the operator can empty.
    process_collector_enabled: bool = True
    process_min_rss_mb: float = 200.0
    process_exclude_names: list[str] = field(
        default_factory=lambda: [
            "neuropacad",
            "python3",
            "node",
            "chrome-devtools-mcp",
            "cosmic-comp",
            "Xwayland",
            # A test run (this build's own `pytest`) exceeds process_min_rss_mb
            # like any real app and got censused as `app:pytest` on the live
            # graph (VISION_PHASES.md, the friendly-names sweep) — the same
            # D-20 "the daemon's own tooling is not an activity" reasoning as
            # the entries above, just a tool that runs from a dev checkout
            # rather than the shipped daemon.
            "pytest",
            # The same D-20 reasoning again, caught on the live graph a second
            # time (2026-09-12): whatever `claude` CLI session is doing the
            # daemon-management work in a terminal window is not the user's
            # own activity either, and it was never added here — every
            # co-occurrence edge that session's own terminal use wired
            # (Hebbian "related_to" from `app:cosmic-term` to nearly
            # everything else touched nearby in time) was noise, not signal.
            "claude",
        ]
    )
    # B14 · web-app attribution. When the focused window's app_id is in
    # `webapp_browser_app_ids`, the ActivityCollector matches the window TITLE
    # against `webapp_map_path` (a site-name -> domain allowlist) and emits the
    # matched label ("gmail") on APP_SWITCH, so the browser stops being one
    # opaque `app:` node. The raw title never leaves the collector. Off, or an
    # empty browser set, or a missing map => the browser stays one node (B13).
    webapp_tracking_enabled: bool = True
    webapp_map_path: str = "data/webapp_map.default.toml"
    webapp_browser_app_ids: list[str] = field(default_factory=lambda: ["brave-browser"])
    # B13-B1 · MemoryPressurePattern (D-19). Fires when `system.mem_percent`
    # z-scores above `mem_pressure_z` OR `mem_available_mb` drops below
    # `mem_pressure_floor_mb`, sustained `mem_pressure_sustain_seconds` (shorter
    # than HighLoad's 300 s — a memory ceiling is a slower, more meaningful
    # event than a CPU spike).
    mem_pressure_z: float = 2.0
    mem_pressure_floor_mb: float = 1024.0
    mem_pressure_sustain_seconds: float = 180.0
    # B13 · raw-data CSV (operator request, this session). Empty => disabled.
    # A path => `RawMetricsRecorder` appends one row per collector reading
    # (system metrics wide + one row per censused app). Soak / dogfood only;
    # append-only, no rotation — point it somewhere you will sweep.
    raw_metrics_csv_path: str = ""
    # Post-terminal-removal replacement for what `neuropaca health` used to
    # answer over the L9 socket (removed — no CLI, no socket, nothing
    # human-facing). Empty => disabled, same convention as
    # `raw_metrics_csv_path` above. A path => the orchestrator writes its own
    # `health_check()` as JSON to this file every `health_dump_interval_seconds`
    # (atomically — temp file + rename, so a reader never sees a half-written
    # file). `scripts/soak_probe.py` reads it instead of a socket; nothing else
    # is a client of it, by design.
    health_dump_path: str = ""
    health_dump_interval_seconds: float = 30.0
    watch_paths: list[str] = field(default_factory=list)
    filesystem_ignore_globs: list[str] = field(
        default_factory=lambda: [
            "*/.git/*",
            "*/node_modules/*",
            "*/__pycache__/*",
            "*/.venv/*",
            "*/.mypy_cache/*",
            "*/target/*",
            "*/dist/*",
            "*/build/*",
            "*.pyc",
        ]
    )

    def __post_init__(self) -> None:
        errs: list[str] = []

        if self.inference_backend not in _VALID_BACKENDS:
            errs.append(
                f"inference_backend must be one of {sorted(_VALID_BACKENDS)}, "
                f"got {self.inference_backend!r}"
            )
        if self.inference_backend == "llama":
            if not self.model_path:
                errs.append("model_path is required when inference_backend='llama'")
            elif not Path(self.model_path).is_file():
                errs.append(f"model_path does not exist: {self.model_path}")

        if self.log_level.upper() not in logging.getLevelNamesMapping():
            errs.append(f"unknown log_level: {self.log_level!r}")
        if self.log_to_file and not self.log_file_path:
            errs.append("log_file_path must not be empty when log_to_file is on")

        for name in (
            "idle_threshold_seconds",
            "graph_save_interval_seconds",
            "bitnet_max_tokens",
            "n_threads",
            "max_failures",
            "max_file_tokens",
            "snapshot_buffer_size",
            "correlation_window_seconds",
            "model_context_tokens",
            "insight_refractory_minutes",
            "interactive_model_context_tokens",
            "dmn_cycle_wall_clock_seconds",
            "dmn_max_inferences_per_cycle",
            "dmn_idle_thought_ttl_hours",
            "dmn_top_k",
            "dmn_candidate_pool_k",
            "coactivation_window_seconds",
            "coactivation_max_nodes",
            "pressure_decay_half_life_seconds",
            "pressure_decay_interval_seconds",
            "quarantine_ttl_hours",
            "action_confirmation_timeout_seconds",
            "agent_wall_clock_budget_seconds",
            "agent_idle_ttl_days",
            "max_ephemeral_nodes",
            "welcome_min_idle_minutes",
            "welcome_daily_cap",
            "episode_retention_days",
            "briefing_max_items",
            "dmn_curiosity_lookback_days",
            "dmn_curiosity_top_pairs",
            "health_dump_interval_seconds",
            "mirror_baseline_days",
            "mirror_top_contributors",
        ):
            if getattr(self, name) <= 0:
                errs.append(f"{name} must be > 0, got {getattr(self, name)}")

        for name in (
            "attention_alpha",
            "attention_beta",
            "attention_gamma",
            "attention_recency_half_life_seconds",
            "attention_ppr_eps",
            "briefing_idle_gap_hours",
        ):
            if getattr(self, name) <= 0:
                errs.append(f"{name} must be > 0, got {getattr(self, name)}")
        if not 0.0 < self.attention_ppr_alpha < 1.0:
            errs.append(
                f"attention_ppr_alpha must be in (0.0, 1.0), got {self.attention_ppr_alpha}"
            )
        if self.briefing_similarity_mu < 0.0:
            errs.append(f"briefing_similarity_mu must be >= 0, got {self.briefing_similarity_mu}")

        if self.dmn_candidate_pool_k < self.dmn_top_k:
            # V-4 · the sample cannot be wider than the pool it is drawn from.
            errs.append(
                f"dmn_candidate_pool_k ({self.dmn_candidate_pool_k}) must be "
                f">= dmn_top_k ({self.dmn_top_k})"
            )
        if self.dmn_seed_refractory_cycles < 0:
            errs.append(
                f"dmn_seed_refractory_cycles must be >= 0, got {self.dmn_seed_refractory_cycles}"
            )
        if not 0.0 <= self.dmn_curiosity_epsilon <= 1.0:
            errs.append(
                f"dmn_curiosity_epsilon must be in [0.0, 1.0], got {self.dmn_curiosity_epsilon}"
            )
        if self.mirror_baseline_half_life_days <= 0.0:
            errs.append(
                "mirror_baseline_half_life_days must be > 0, got "
                f"{self.mirror_baseline_half_life_days}"
            )
        if self.mirror_same_weekday_boost <= 0.0:
            errs.append(
                f"mirror_same_weekday_boost must be > 0, got {self.mirror_same_weekday_boost}"
            )
        if self.mirror_kl_threshold <= 0.0:
            errs.append(f"mirror_kl_threshold must be > 0, got {self.mirror_kl_threshold}")
        if not 0 <= self.mirror_evening_hour <= 23:
            errs.append(f"mirror_evening_hour must be in [0, 23], got {self.mirror_evening_hour}")

        if self.pressure_low_threshold <= 0:
            errs.append(f"pressure_low_threshold must be > 0, got {self.pressure_low_threshold}")
        if self.pressure_high_threshold <= self.pressure_low_threshold:
            errs.append(
                "pressure_high_threshold must be > pressure_low_threshold, got "
                f"{self.pressure_high_threshold} <= {self.pressure_low_threshold}"
            )
        unknown_tiers = sorted(set(self.action_enabled_tiers) - VALID_ACTION_TIERS)
        if unknown_tiers:
            errs.append(
                f"action_enabled_tiers must be a subset of {sorted(VALID_ACTION_TIERS)}, "
                f"got unknown {unknown_tiers}"
            )
        if not self.quarantine_path:
            errs.append("quarantine_path must not be empty — nothing may be deleted in place")
        if self.api_call_enabled and not self.api_allowlist:
            errs.append("api_call_enabled requires a non-empty api_allowlist (rules.md §5.5)")
        if self.max_concurrent_agents < 0:
            errs.append(f"max_concurrent_agents must be >= 0, got {self.max_concurrent_agents}")
        if self.agent_inference_budget < 0:
            errs.append(f"agent_inference_budget must be >= 0, got {self.agent_inference_budget}")
        if self.top_process_count < 0:
            errs.append(f"top_process_count must be >= 0, got {self.top_process_count}")
        if self.process_min_rss_mb < 0:
            errs.append(f"process_min_rss_mb must be >= 0, got {self.process_min_rss_mb}")
        if self.mem_pressure_z <= 0:
            errs.append(f"mem_pressure_z must be > 0, got {self.mem_pressure_z}")
        if self.mem_pressure_floor_mb < 0:
            errs.append(f"mem_pressure_floor_mb must be >= 0, got {self.mem_pressure_floor_mb}")
        if self.mem_pressure_sustain_seconds <= 0:
            errs.append(
                f"mem_pressure_sustain_seconds must be > 0, got {self.mem_pressure_sustain_seconds}"
            )
        if not 0.0 < self.hebbian_delta <= 1.0:
            errs.append(f"hebbian_delta must be in (0.0, 1.0], got {self.hebbian_delta}")
        if self.hebbian_insight_multiplier < 1.0:
            errs.append(
                f"hebbian_insight_multiplier must be >= 1.0, got {self.hebbian_insight_multiplier}"
            )
        if self.coactivation_refractory_seconds < 0:
            errs.append(
                "coactivation_refractory_seconds must be >= 0, "
                f"got {self.coactivation_refractory_seconds}"
            )
        if self.hebbian_half_life_hours <= 0:
            errs.append(f"hebbian_half_life_hours must be > 0, got {self.hebbian_half_life_hours}")
        if self.hebbian_floor <= 0:
            errs.append(f"hebbian_floor must be > 0, got {self.hebbian_floor}")
        elif self.hebbian_floor >= self.hebbian_delta:
            # one co-activation is worth at most `hebbian_delta`; at or under the
            # floor, every new pair is pruned by the next sweep and nothing sticks
            errs.append(
                f"hebbian_floor ({self.hebbian_floor}) must be < hebbian_delta "
                f"({self.hebbian_delta})"
            )

        for key, val in self.poll_intervals.items():
            if val <= 0:
                errs.append(f"poll_intervals[{key!r}] must be > 0, got {val}")

        if errs:
            raise ConfigError("invalid Config: " + "; ".join(errs))

    @classmethod
    def from_file(cls, path: str | Path) -> Config:
        """Load and validate a TOML config. Raises `ConfigError` on any problem."""
        p = Path(path)
        try:
            raw = tomllib.loads(p.read_text("utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read config file {p}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"malformed TOML in {p}: {exc}") from exc

        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(f"unknown config keys in {p}: {sorted(unknown)}")

        return cls(**raw)


# gen-ref: a0f1b023
