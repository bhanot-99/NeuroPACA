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
    poll_intervals: dict[str, float] = field(default_factory=lambda: {"system": 60.0})
    graph_save_interval_seconds: int = 300
    bitnet_max_tokens: int = 256
    # B4 · Learning (L4, D-11). model_context_tokens = llama.cpp n_ctx (the
    # extractive prompt is ~200 tokens, output <= 48 — 2048 is generous and keeps
    # the KV-cache small). adaptation_buffer_size bounds BitNetPlasticity's
    # (Signal, Insight) deque + the Jaccard-novelty comparison set.
    model_context_tokens: int = 2048
    adaptation_buffer_size: int = 64
    # B5 · Interface (L9, A2/B4). max_context_tokens bounds the distilled
    # retrieval context handed to the interactive model; `_build_context`
    # truncates the joined node lines to `max_context_tokens * _CHARS_PER_TOKEN`
    # characters — a cheap proxy that needs no tokenizer and keeps a confused
    # small model from drowning in facts (problems.md 1.13).
    max_context_tokens: int = 512
    # B5 · dual-model routing (D-12). The always-on loop (L4/L6) uses the BitNet
    # 2B4T model; the interactive `$` / `$?` path uses a larger Qwen2.5-3B-Instruct
    # Q4 GGUF that can actually write a grounded sentence. Empty path => the
    # interactive backend lazy-self-disables and `$?` falls back to the extractive
    # template. Both models are resident concurrently at peak (PRD §9); a single
    # `_inference_lock` still serialises every call system-wide (rules.md §4).
    interactive_model_path: str = ""
    # 2048 is already generous: the `$?` prompt (few-shot + <=5 nodes + question +
    # a live-snapshot line) is ~300 tokens, the answer <= 96. A larger n_ctx only
    # inflates the interactive model's resident footprint (B5 memory finding).
    interactive_model_context_tokens: int = 2048
    # B5 · L9 IPC. Empty => `$XDG_RUNTIME_DIR/neuropaca.sock` (falls back to the
    # system temp dir). Tests point this at a `tmp_path` so no test binds a
    # socket outside its sandbox (rules.md §8).
    interface_socket_path: str = ""
    # B6 · Idle Cognition (L6, D-13). Strict budgets on one DMN idle cycle:
    #   - dmn_cycle_wall_clock_seconds — `asyncio.timeout` ceiling for a whole
    #     cycle (reminiscence + imagination); an overrun is logged, not fatal.
    #   - dmn_max_inferences_per_cycle — at most this many idle-thought model
    #     calls per cycle; the DMN also bails the moment `BitNetRuntime.is_busy`.
    #   - dmn_idle_thought_ttl_hours — an `idle:` / `insight:` node past this age
    #     is pruned (the 48 h idle-thought cache lifetime, Architecture.md §8).
    #   - dmn_top_k — graph nodes pulled (by relevance_score) to seed imagination.
    dmn_cycle_wall_clock_seconds: int = 60
    dmn_max_inferences_per_cycle: int = 3
    dmn_idle_thought_ttl_hours: int = 48
    dmn_top_k: int = 5
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
    # B2.5 · Process & Activity Sensing (D-9). activity_enabled turns on the
    # Wayland ext-idle-notify ActivityCollector (needs `pip install .[activity]`);
    # when on, XMetricCollector stops emitting its CPU-derived idle stand-in.
    # top_process_count = top-N process-by-CPU rows in each system snapshot (0 = off).
    activity_enabled: bool = False
    top_process_count: int = 5
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
            "adaptation_buffer_size",
            "max_context_tokens",
            "interactive_model_context_tokens",
            "dmn_cycle_wall_clock_seconds",
            "dmn_max_inferences_per_cycle",
            "dmn_idle_thought_ttl_hours",
            "dmn_top_k",
            "pressure_decay_half_life_seconds",
            "pressure_decay_interval_seconds",
            "quarantine_ttl_hours",
            "action_confirmation_timeout_seconds",
            "agent_wall_clock_budget_seconds",
            "agent_idle_ttl_days",
            "max_ephemeral_nodes",
        ):
            if getattr(self, name) <= 0:
                errs.append(f"{name} must be > 0, got {getattr(self, name)}")

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
