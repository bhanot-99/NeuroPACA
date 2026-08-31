# phases.md — Build Roadmap

**Status:** Draft

Derived from the "Full System Workflow" in `neuropaca-v4.html` and the layer dependency chain in `Architecture.md`.

---

## The runtime lifecycle (what the finished system does)

| Phase | Layer | What happens |
| --- | --- | --- |
| **0 · Init** | L10 | Boot. Load the BitNet model via llama.cpp. Register the 8 modules. Start the EventBus. |
| **1 · Sensing** | L2 | Passive collection every 60 s — CPU, RAM, disk, temp, processes, network, logs. Published async; all modules subscribe. |
| **2 · Diagnosis** | L3 | Correlate signals, filter noise, threshold decisions. Local LLM only for complex cases. |
| **3 · Learning** *(parallel)* | L4, L6 | Update `relevance_score`s in the graph (Hebbian co-occurrence). The DMN replays nodes and consolidates the graph during idle. Raw sensor buffers purged past TTL. *(Model weight adaptation is deferred — `pruning.md`.)* |
| **4 · Action** | L5, L7 | Pressure accumulates; safe actions fire silently at low threshold, dangerous ones need synchronised spikes or a terminal prompt. Safety gate: sandbox, backup, rollback. |
| **5 · Comms** | L9 | Filter, notify only when needed, format `$` responses, tray icon, daily report. |

Feedback loop: action result → L4 score update → graph → next retrieval and next idle cycle reflect the outcome.
Override channels: `$` / `$?` / `$!` (skips L3+L4) / `$$` (full backup + verify).

---

## Build order

Each step produces a runnable, demonstrable daemon. Build against the dependency chain, not by layer number.

### B0 · Foundation
Repo skeleton per `Architecture.md §15`; `pyproject.toml`, `uv` lockfile, `ruff` + `mypy` + `pre-commit`, CI. `core/logging.py`, `core/errors.py`.
**De-risking spike (highest risk):** benchmark BitNet b1.58 2B4T via llama.cpp on the target machine — RSS after load, tokens/sec, thermal behaviour over 30 min. Then the coherence test (`problems.md` 1.13, `rules.md §4.1`), run as a **controlled ablation over context size** (K = 1 / 3 / 5 / 8 distilled nodes): ~20 synthetic `(signal + K nodes)` fixtures per K, through a GBNF-constrained schema with `cited_nodes` locked to that prompt's aliases, greedy. Report valid-parse rate, citation-accuracy(K) as a curve, and correct-abstain rate on deliberately weak inputs. Set `_build_context()`'s production K at the knee. If citation accuracy can't reach ~80 % at any K → 3B Q4 fallback for `$?`. If it doesn't fit the RAM/latency budget → L4/L6/L9 all need a fallback.

### B1 · Core Infrastructure (L1 + minimal L10)
`Event`/`EventType`, `EventBus` (bounded queue, subscriber isolation → `SYSTEM_ERROR`), `Node`/`Edge`/enums, `GraphMemory` (CRUD, `find_related`, atomic save, `asyncio.Lock`, score recalculation, `consolidate`, `prune`), `Config` + validation, `BaseModule` ABC, `SystemHealth`, `InferenceBackend` protocol + `BitNetRuntime` skeleton + `FakeInferenceBackend`, `NeuroPACAOrchestrator`, `Scheduler`, `daemon.py`.
**Exit:** daemon starts, runs an idle event loop, holds a graph, `SIGTERM`s clean with a flat RSS over a 1 h soak. 10 k-node graph loads < 2 s, `find_related(depth=2)` < 50 ms. Concurrent writers serialise correctly.

### B2 · Sensing (L2)
`MetricSnapshot`, `BaseCollector`, `XMetricCollector` (one `asyncio.Task` per collector, ring buffer, per-collector failure isolation), `SystemMetricCollector`, `FileSystemCollector` (watchdog). `Clock` protocol + `FakeClock`. `build_modules()` factory + orchestrator wiring. `anomaly_score` is hardcoded `0.0` in L2 — all baselining is L3 (D-7, Architecture.md §4).

> **`ActivityCollector` + `get_idle_seconds()` + `IDLE_DETECTED` / `ACTIVITY_DETECTED` + `FocusSessionPattern` / `DistractionPattern` are DEFERRED to B2.5** (D-7, D-8, `problems.md` 1.7). The target machine is Wayland + COSMIC with no unified idle API — a dedicated spike, not a collector. B2 ships **system + filesystem collectors only**. B6's DMN will trigger on a synthetic **CPU < 5%** event from `SystemMetricCollector` instead of `IDLE_DETECTED`.

**Exit:** 24 h soak < 1 % mean CPU, RSS flat, buffer provably bounded, killing one collector leaves the others running.

### B2.5 · Process & Activity Sensing *(spike + build, inserted D-8, split D-9)*

**B2.5a · Idle (DONE 2026-08-31).** `IdleSource` protocol + `WaylandIdleSource` (`ext-idle-notify-v1` via pywayland, `loop.add_reader`, no thread) + `FakeIdleSource`; `ActivityCollector` (BaseModule) → real `IDLE_DETECTED` / `ACTIVITY_DETECTED`, superseding the D-7 CPU-derived stand-in; `top_processes` (names only) in `SystemMetricCollector`. `pywayland` is a `[activity]` optional extra, lazy-imported, self-disables when absent.
> **Exit:** `FakeIdleSource` drives the two edges (edge-triggered, degraded path self-disables + `SYSTEM_ERROR`, no crash); live-verified on cosmic-comp.

**B2.5b · Active window (TODO).** Vendor `zcosmic_toplevel_info_v1` XML + `pywayland.scanner` dep chain; `ToplevelSource` + `APP_SWITCH` (new EventType, Architecture §13 bump); `app_map.toml` (app_id / wm_class / path-glob → domain), O(1) lookup, loaded once; `FocusSessionPattern` + `DistractionPattern`; domain classification in `SignalCorrelator._update_graph` (`domain:*` edges, `bridge_value` goes live).
> **Exit:** active window read reliably on the target box; the two patterns fire on fixtures and stay silent on negatives; classification is dict + glob only, zero inference in the poll path.

### B3 · Diagnosis (L3)
`Signal` / `SignalDraft`, `BasePattern` (pure, synchronous, edge-triggered), `MetricBaseline` (rolling mean/stddev, confidence-scaling only), `SignalCorrelator` (per-collector `Dict[str, Deque[MetricSnapshot]]`, `_update_graph` via `upsert_node` before publish, publishes `SIGNAL_CORRELATED` + `MEMORY_UPDATED` + `PATTERN_DETECTED`), the **two** patterns `HighLoadPattern` (`cpu > 90` for ≥ 5 min) and `IdlePattern` (`cpu < 5` for ≥ `idle_threshold`), pattern contract suite. `GraphMemory.upsert_node` and `Config.correlation_window_seconds = 1800` land here (D-8). Absolute blueprint thresholds trigger; `MetricBaseline` only scales `confidence`. `FocusSession` / `Distraction` are **not** in B3 — see B2.5.
**Exit:** `HighLoadPattern` and `IdlePattern` each fire against a recorded fixture and stay silent against a negative fixture; a synthetic 5th pattern registers with zero changes to `SignalCorrelator`; **zero inference calls in L3**; the per-collector deques are provably bounded; `_update_graph` never resets an existing node's `relevance_score`.

### B4 · Learning (L4)
Full `BitNetRuntime` (`load`/`unload`, `infer`, `infer_async`, dedicated executor, `build_context_from_nodes`, `get_ram_usage_mb`), the ADR-chosen backend, `Insight`, `learning/prompts.py`, `BitNetPlasticity` (subscribe, gate on confidence/novelty/`is_busy`, generate, store as an `INSIGHT` node edged to its sources, publish), bounded `adaptation_buffer`, Hebbian edge updates.
**Exit:** model loads within budget; loop-lag monitor shows no stall > 50 ms during a 10 s inference; every stored insight traces to ≥ 1 snapshot and ≥ 1 node; gating drops ≥ 50 % of signals under a storm.

### B5 · Interface (L9)
`Message`/`InterfaceChannel`, `InterfaceLayer` (`on_user_input`, retrieval `_build_context`, `_generate_response`, `send_to_user`), RAM-only history + persistence-absence test, insight priority filter + daily cap + surface-once, unix-socket IPC, thin CLI client (`ask`, `health`), `rich` renderers (see `design.md`), shell hooks `$`/`$?`/`$!`/`$$`.
**Exit:** `$ what's using my CPU` returns an answer citing real node labels; `conversation_history` provably absent from every file on disk; CLI responds < 100 ms for non-inference commands.

### B6 · Idle Cognition (L6)
`DefaultModeNetwork` — cancellable `idle_task`, instant cancel on activity, `consolidate_memory` / `link_orphan_nodes` / `prune_stale_nodes` / `generate_proactive_insights`, 48 h TTL, `save_to_disk`, one lock-cycle per merge.
**Exit:** returning to the keyboard cancels the cycle < 1 s with no partial corruption; a cycle never exceeds its wall-clock and inference budget; `consolidate()` shrinks a duplicate-heavy fixture.

### B7 · Drive & Action (L5 + L7)
`PressureEntry`, `PressureAccumulator` (two independent sources, `decay()` on a timer, low/high thresholds, `_publish_if_over_threshold`), `$ pressure` view. `BaseAction`, safety gate (sandbox, backup, rollback), JSONL audit, `NotificationAction`/`MemoryWriteAction`/`FileWriteAction`/`ApiCallAction` (disabled by default)/`RunCommandAction` (terminal confirmation), quarantine directory.
**Exit:** a single signal never crosses the high threshold; pressure decays to < 1 % within 10 min of the last contribution; dangerous actions cannot execute without a recorded confirmation; audit log complete for every attempt; a review period in dry-run with zero false positives before any tier goes live.

### B8 · Agents & structural plasticity (L8)
`AgentSupervisor` (bounded tasks, `max_concurrent_agents`), `spawn_node()` / `kill_node()`, apoptosis after `idle_ttl = 14d`. Confirm the class shape against a full-width diagram re-export first.

### B9 · Hardening
systemd user unit, crash recovery, graph schema versioning, full `health_check()`, log rotation, 7-day soak, fault injection, egress test in CI, `neuropaca export` / `panic` / `doctor`.

---

## Deferred — after the rest of the project

### D1 · Personal model pruning
Everything in [`pruning.md`](pruning.md): the `domain_to_heads` probing spike, `relevance_score` → prune-mask generator, `load_gguf_with_mask` applied once at load, A/B quality harness (sparse vs base vs random-head-cut vs magnitude-pruned), one-command rollback. Starts as a throwaway spike in `experiments/pruning/`; promoted to `src/neuropaca/sparsity/` only if the spike shows promise. Never imported by the daemon. **Not started until B0–B9 are done and the core system has been dogfooded.**

---

## Cross-cutting

- Update `memory.md` at the end of every work session.
- Tests in the same change as the code.
- `ruff` + `mypy` clean before commit.
- No dependency added without approval.
- Nothing merged that violates `rules.md §0`.

---

*Related: [PRD.md](PRD.md) · [Architecture.md](Architecture.md) · [rules.md](rules.md) · [design.md](design.md) · [memory.md](memory.md)*
