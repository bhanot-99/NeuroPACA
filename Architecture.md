# Architecture — NeuroPACA v4

**Status:** Blueprint
**Authoritative source:** the v4 class diagram (`1000071408.png`), supported by `neuropaca-v4.html` (full concept) and `neuropaca-overview.html` (overview + tech-fix).
**Supersedes:** the X/Y/Z/W/V manager model — see §12.

---

## 1. The four critical invariants

These are on the blueprint. Violating any of them is a defect.

1. **Services are held, not inherited.** `EventBus`, `GraphMemory`, and `BitNetRuntime` are singletons. Modules hold *references* to them — they do **not** inherit from them.
2. **`GraphMemory` uses `asyncio.Lock`, not `threading.Lock`.** Mixing the two silently deadlocks.
3. **`BitNetRuntime.infer()` is blocking.** Always wrap it in `loop.run_in_executor()` (`infer_async()`) so the event loop never freezes.
4. **`SignalCorrelator` produces `SIGNAL_CORRELATED`; `PressureAccumulator` and `BitNetPlasticity` consume it — independently.** They never call each other directly. Everything routes through the `EventBus`.

**Derived principle:** modules communicate only by publishing and subscribing on the `EventBus`. No module imports another module. Intelligence is emergent — no single "brain" decides; behaviour emerges from which modules fire together.

---

## 2. Layer map

| Layer | Name | Legacy | Primary classes |
| --- | --- | --- | --- |
| **L1** | Core Infrastructure | — | `EventBus`, `GraphMemory`, `BitNetRuntime`, `Config`, `Node`/`Edge`, enums, `BaseModule` |
| **L2** | Sensing | X | `XMetricCollector`, `BaseCollector`, `SystemMetricCollector`, `FileSystemCollector`, `ActivityCollector`, `MetricSnapshot` |
| **L3** | Diagnosis | Y | `SignalCorrelator`, `BasePattern`, `HighLoadPattern`/`IdlePattern`/`FocusSessionPattern`/`DistractionPattern`, `Signal`, `Insight` |
| **L4** | Learning | Z | `BitNetPlasticity` |
| **L5** | Drive | W | `PressureAccumulator`, `PressureEntry` |
| **L6** | Idle Cognition | — | `DefaultModeNetwork` |
| **L7** | Action | W | `BaseAction`, `NotificationAction`, `FileWriteAction`, `RunCommandAction`, `ApiCallAction`, `MemoryWriteAction` *(reconstructed — §11)* |
| **L8** | Agents | — | `AgentSupervisor` *(reconstructed — §11)* |
| **L9** | Interface | V | `InterfaceLayer`, `Message`, `InterfaceChannel` |
| **L10** | Orchestration | — | `NeuroPACAOrchestrator`, `BaseModule` |

`BaseModule` is the abstract base shared by **all 8 modules** (L2–L9). `NeuroPACAOrchestrator` manages `modules: List[BaseModule]`. The lifecycle contract is *not detailed* in the blueprint — §3.7 defines the binding version.

---

## 3. L1 · Core Infrastructure

### 3.1 `EventBus` «singleton»

```
- _instance      : EventBus                      (class-level)
- subscribers    : Dict[EventType, List[Callable]]
- event_queue    : asyncio.Queue(maxsize=1000)   (B1 decision — bounded)
- loop           : asyncio.AbstractEventLoop
- is_running     : bool
- _dropped_count : int                           (surfaced in SystemHealth)

+ get_instance()                              : EventBus  (static)
+ subscribe(event_type: EventType, callback: Callable)   : None
+ unsubscribe(event_type: EventType, callback: Callable) : None
+ publish(event: Event)                       : None
+ start() / stop()                            : None
- _dispatch_loop()                            : None  «async»
```

- `publish()` enqueues and returns immediately — publishers never wait on subscribers.
- **Bounded queue, drop-on-full** (B1 decision, approved 2026-08-29). `publish()`
  is `put_nowait()` wrapped in `try/except asyncio.QueueFull`: on full it drops
  the incoming event, increments `_dropped_count`, and logs one `ERROR`
  ("EventBus queue full — dropped {event_type}"). It does **not** enqueue a
  `SYSTEM_ERROR` *event* (the queue is full — that would drop too); the drop is a
  logged SYSTEM_ERROR-severity condition and `_dropped_count` shows in
  `SystemHealth`. A full queue means the dispatch loop is wedged — that is the
  real bug to fix, not the drop.
- Per-subscriber isolation: `_dispatch_loop` wraps each callback; a handler that
  raises is caught and reported as a `SYSTEM_ERROR` event, siblings still run. A
  failure inside a `SYSTEM_ERROR` handler is logged only — never re-published.
- Signals are published async; all modules subscribe; no direct coupling.
- No persistence across restarts. Anything needing durability writes to the graph.

### 3.2 `GraphMemory` «singleton»

```
- graph            : networkx.MultiDiGraph          (B1 decision — see below)
- persistence_path : str
- last_save        : datetime
- dirty            : bool
- _lock            : asyncio.Lock

+ get_instance()                              : GraphMemory  (static)
+ add_node(node_id: str, node_type: NodeType, attributes) : Node
+ add_edge(source: str, target: str, relation: RelationType, weight) : Edge
+ get_node(node_id: str)                      : Optional[Node]
+ query(node_type: NodeType, filters)         : List[Node]
+ find_related(node_id: str, depth, *, traverse_hubs=False) : List[Node]
+ get_edges(node_id: str)                     : List[Edge]
+ update_node(node_id: str, attributes)       : None
+ delete_node(node_id: str)                   : None
+ recalculate_importance()                    : None
+ consolidate()                               : None
+ prune(older_than, min_importance)           : int
+ save() / load()                             : None
```

Score-management surface (from the concept): `get_top_k(domain, k, filters)`, `decay_scores(factor, min_val)`, `prune_low_score(threshold, domain)`, `export_subgraph(domain)`.

- `graph` is private. Nothing outside `GraphMemory` touches the `networkx` object.
- **`networkx.MultiDiGraph`, not `DiGraph`** (B1 decision, approved 2026-08-29). A
  plain `DiGraph` collapses every relation between the same ordered pair into one
  edge — `pytest CAUSED_BY crash` and `pytest FOLLOWED_BY commit` between the same
  two nodes cannot coexist. Edge identity is `(source, target, relation)`; the
  `RelationType` is the networkx edge **key**. `get_edges()` returns every
  parallel edge. On-disk form (`nx.node_link_data`, `multigraph: true`) gains a
  `key` per link.
- **Node / Edge IDs are `str` everywhere** (B1 decision). `file:/abs/path`,
  `app:code`, `domain:engineering`, `YOU` — stable and deterministic (rules.md
  §3). No `UUID` for anything that identifies or references a node; every
  node-reference field (`Message.related_node_ids`, `Insight.context_nodes`, …)
  is `List[str]`. `Event.id` stays a `UUID` — it identifies an event, not a node.
- **Routing skeleton is a hard-coded protected set** (B1 decision): the 11 hub
  IDs — `YOU` plus `domain:{engineering, research, tools, system, habits,
  projects, meetings, comms, mental_models, learning}` (PRD §F3). `prune()` and
  `prune_low_score()` skip any ID in this set regardless of age or score —
  low-score cleanup must never collapse the routing layer. Created once at
  `load()` when the store is empty.
- **`find_related()` does not traverse through hubs** (B1 decision). The `YOU`
  hub and the 10 domain hubs connect to nearly everything; a depth-2 BFS through
  one of them fans out to the whole graph and blows the < 50 ms target. BFS never
  expands a hub node's neighbours (`traverse_hubs=False` default); a hub still
  appears in the result if it is directly adjacent to the seed.
- All writes serialise through the single `_lock`. Public methods acquire the
  lock and call **lock-free `_*_locked` workers**; compound operations
  (`consolidate`, `prune`) take the lock **once** and call several workers.
  `asyncio.Lock` is not reentrant — a public method calling another public method
  while holding `_lock` deadlocks the loop forever (problems.md 1.10).
- `save()` is atomic: temp file → `fsync` → `os.replace` → `fsync` the directory.

**`relevance_score`** (0–10 composite, recomputed on a schedule, never per-event):

```
score = normalize(
      frequency        * 3.0
    + decay(last_seen) * 3.0
    + log(connections) * 2.0
    + bridge_value     * 2.0
)
```

### 3.3 `BitNetRuntime` «singleton»

```
- model             : Any
- tokenizer         : Any
- model_path        : str
- max_context_tokens: int
- is_loaded         : bool
- _inference_lock   : asyncio.Lock
- temperature       : float
- is_busy           : bool

+ get_instance()                                          : BitNetRuntime  (static)
+ load_model() / unload_model()                           : None
+ infer(prompt, max_tokens, temperature, grammar=None)    : str        ← BLOCKING
+ infer_async(prompt, max_tokens, grammar=None)           : Awaitable[str]  «async»
+ build_context_from_nodes(nodes: List[Node])             : str
+ get_ram_usage_mb()                                      : float
```

- Owns `model` + `tokenizer` **in-process via llama.cpp** — not an HTTP call to Ollama. See §10.
- `infer_async()` acquires `_inference_lock`, then `run_in_executor`. One inference at a time, system-wide.
- Model can be unloaded and lazily reloaded.
- **`grammar: Optional[str] = None`** (B1 decision, approved 2026-08-29) — a GBNF
  string, assembled by the caller *before* the lock (rules.md §4.1). Present on
  both `infer` / `infer_async` **and** the `InferenceBackend` protocol from B1,
  so B4's constrained-generation path needs no signature change. `None` = free
  decode (used only by tests and the `$` interactive path).
- **`InferenceBackend` protocol** (B1): `load()`, `unload()`, `infer(prompt, max_tokens, temperature, grammar=None) -> str`, `get_ram_usage_mb() -> float`.
  Two implementations in B1: `LlamaCppBackend` (skeleton) and
  `FakeInferenceBackend` (deterministic — rules.md §8). Selected by
  `Config.inference_backend`. Module code never imports a backend (rules.md §4).

### 3.4 `Config` «dataclass»

```
model_path                  : str
graph_db_path               : str
action_log_path             : str
idle_threshold_seconds      : int
pressure_threshold          : float
max_concurrent_agents       : int
log_level                   : str
poll_intervals              : Dict[str, float]
graph_save_interval_seconds : int
bitnet_max_tokens           : int
inference_backend           : str = "llama"    (B1 — "fake" in tests)
```

Concept variant also carries `n_threads`, `max_failures = 3`, `max_file_tokens = 4096`. Loaded once at startup, immutable thereafter. `from_file(path) -> Config`.

**`inference_backend`** (B1 decision, approved 2026-08-29): `"llama"` selects
`LlamaCppBackend`, `"fake"` selects `FakeInferenceBackend`. Validation is
backend-aware — `model_path` must exist when `"llama"`, is ignored when `"fake"`.

### 3.5 Core data model

```
Event «dataclass»            Node «dataclass»                Edge «dataclass»
  id        : UUID             id              : str          source_id : str
  event_type: EventType        node_type       : NodeType      target_id : str
  payload   : Dict[str, Any]   name / label    : str           relation  : RelationType
  timestamp : datetime         created_at      : datetime      weight    : float
  source    : str              last_accessed   : datetime      created_at: datetime
  priority  : int              access_count    : int
                               relevance_score : float
                               priority        : int
```

**Node references are `str`, not `UUID`** (B1 decision, approved 2026-08-29).
`Node.id` / `Edge.source_id` / `Edge.target_id` are already `str`. Every field
anywhere in the system that *holds* a node id is `List[str]` / `str` too —
`Message.related_node_ids` (§9), `Insight.context_nodes` / `related_signal` (§5),
event payloads. The blueprint's `List[UUID]` on `Message` is superseded. Only
`Event.id` stays a `UUID` — it identifies an event, never a node.

### 3.6 Enumerations

```
EventType                      NodeType        RelationType
  METRIC_COLLECTED               TASK            RELATED_TO
  SIGNAL_CORRELATED              PERSON          CAUSED_BY
  PATTERN_DETECTED               CONCEPT         PART_OF
  ACTION_TRIGGERED               FILE            DEPENDS_ON
  MEMORY_UPDATED                 EVENT_LOG       CREATED
  PRESSURE_THRESHOLD_REACHED     METRIC          MODIFIED
  IDLE_DETECTED                  INSIGHT         FOLLOWED_BY
  ACTIVITY_DETECTED              APP             CONTRADICTS
  INSIGHT_GENERATED              SESSION
  USER_MESSAGE                   GOAL          SignalType
  AGENT_SPAWNED                                  FOCUS_SESSION   FILE_ACTIVITY
  AGENT_COMPLETED                                DISTRACTION     APP_SWITCH
  SYSTEM_ERROR                                   HIGH_LOAD       USER_RETURN
                                                 IDLE
InterfaceChannel
  CLI · WEB_SOCKET · NOTIFICATION_ONLY
```

A string literal where an enum belongs is a defect.

### 3.7 `BaseModule` «abstract»

The lifecycle contract shared by all 8 modules (not detailed in the blueprint — this is the binding version):

```python
class BaseModule(ABC):
    name: str
    is_running: bool
    event_bus: EventBus
    config: Config

    @abstractmethod
    async def initialize(self) -> None: ...   # subscribe, allocate, validate
    @abstractmethod
    async def start(self) -> None: ...        # begin work (idempotent)
    @abstractmethod
    async def stop(self) -> None: ...         # unsubscribe, cancel tasks, flush
    @abstractmethod
    def health(self) -> ModuleHealth: ...     # never raises, never blocks
```

---

## 4. L2 · Sensing (X)

```
XMetricCollector «Module»                 BaseCollector «abstract»
  - event_bus       : EventBus              + name                 : str
  - collectors      : List[BaseCollector]   + poll_interval_seconds : float
  - is_running      : bool                  + last_poll            : datetime
  - snapshot_buffer : List[MetricSnapshot]  + is_enabled           : bool
  + start() / stop()                        + collect() : MetricSnapshot  «abstract»
  + register_collector(c: BaseCollector)    + should_poll() : bool
  - _poll_loop() «async»

SystemMetricCollector   FileSystemCollector           ActivityCollector
  CPU/RAM/disk/temp       watch_paths                   last_active_window
  via psutil              recent_changes                last_input_time
                          watchdog.Observer thread      get_idle_seconds()  ← platform-specific

MetricSnapshot «dataclass»
  collector_name : str · timestamp : datetime · data : Dict[str, Any] · anomaly_score : float
```

- Collects raw system data every 60 s. No intelligence — reads and publishes to the EventBus.
- `collect()` must be non-blocking (dispatch via `asyncio.to_thread`; `psutil.cpu_percent(interval=1)` alone costs a second).
- A collector that raises repeatedly disables itself and reports `SYSTEM_ERROR`; the others keep running.
- `snapshot_buffer` is a bounded ring buffer.
- Inputs: `psutil`, `/proc`, shell hooks (Bash/Zsh), editor extension APIs, system event logs.
- Emits `METRIC_COLLECTED`, and `IDLE_DETECTED` / `ACTIVITY_DETECTED` on idle-state transitions.

---

## 5. L3 · Diagnosis (Y)

```
SignalCorrelator «Module»                  BasePattern «abstract»
  - event_bus        : EventBus              + matches(snapshots) : bool
  - graph_memory     : GraphMemory           + create_signal(snapshots) : Signal
  - pattern_registry : List[BasePattern]
  - recent_snapshots : Deque[MetricSnapshot] HighLoadPattern · IdlePattern
  - correlation_window : timedelta           FocusSessionPattern · DistractionPattern
  + on_metric_event(event)  : None
  - _correlate()            : List[Signal]   Signal «dataclass»
  - _update_graph(signal)   : None             signal_type      : SignalType
  - _publish_signal(signal) : None             confidence       : float
                                               related_node_ids : List[str]
Insight «dataclass»                            source_snapshots : List[MetricSnapshot]
  id · confidence · related_signal             timestamp        : datetime
  context_nodes · timestamp
```

**Concrete pattern triggers (from the blueprint):**

| Pattern | Trigger |
| --- | --- |
| `FocusSessionPattern` | high CPU + coding app active > 20 min |
| `DistractionPattern` | app switching > 5× in 2 min |
| `HighLoadPattern` | CPU % > 90 for > 5 min |
| `IdlePattern` | no input > `idle_threshold` |

- Interprets X sensor data — correlate signals, filter noise, predict failures.
- Rule-based; the local LLM is only invoked for complex cases.
- Adding a pattern = a class + one registry line. `SignalCorrelator` never changes.
- `_update_graph()` writes the implicated nodes **before** publishing.

---

## 6. L4 · Learning (Z)

```
BitNetPlasticity «Module»
  - event_bus        : EventBus
  - graph_memory     : GraphMemory
  - bitnet_runtime   : BitNetRuntime
  - adaptation_buffer: List[Tuple[Signal, Insight]]
  + on_signal_event(event)        : None
  - _generate_insight(signal)     : Insight  «async»
  - _store_insight(insight)       : None
  - _publish_insight(insight)     : None
```

- Builds the behavioural fingerprint — updates `relevance_score`s in the graph every cycle.
- Hebbian: co-occurring events strengthen their edge weight (`weight += ~0.01`).
- Not every signal deserves inference — gate on confidence, novelty, and `is_busy`. Dropping a signal is correct.
- `adaptation_buffer` is bounded. It is a record of `(signal, insight)` pairs for later analysis — **not** an inference queue, and (in the core system) not a training set. Model weight adaptation is deferred (`pruning.md`).

---

## 7. L5 · Drive (W) — Action Gradients

```
PressureAccumulator «Module»               PressureEntry «dataclass»
  - event_bus  : EventBus                    node_id      : str
  - graph_memory : GraphMemory               pressure     : float
  - pressure_map : Dict[str, float]          reason       : str
  - threshold  : float                       created_at   : datetime
  - decay_rate : float                       last_updated : datetime
  + on_signal_event(event)  : None
  + on_insight_event(event) : None
  + add_pressure(node_id, amount, reason) : None
  + decay()                 : None
  + get_top_pressures(n)    : List[PressureEntry]
  - _publish_if_over_threshold() : None
```

Replaces manual approval with activation thresholds:

- **Low threshold** — a single spike from Diagnosis fires a safe action (clear a stale cache) instantly and silently.
- **High threshold** — a dangerous action (kill a frozen process) requires synchronised spikes from **Sensing + Diagnosis + Learning simultaneously**. If that triple corroboration isn't met, the system prompts you in the terminal instead of acting.
- Pressure decays ~50 %/min when signals stop arriving — last week's spike cannot combine with today's.
- Every `PressureEntry` carries a `reason`. An action that cannot explain itself must not fire.
- Emits `PRESSURE_THRESHOLD_REACHED`.

---

## 8. L6 · Idle Cognition — Default Mode Network

```
DefaultModeNetwork «Module»
  - event_bus      : EventBus
  - graph_memory   : GraphMemory
  - bitnet_runtime : BitNetRuntime
  - idle_threshold : timedelta
  - is_active      : bool
  - last_activity  : datetime
  - idle_task      : Optional[asyncio.Task]
  + on_idle_detected(event)          : None
  + on_activity_detected(event)      : None
  - _run_idle_cycle()                : None  «async»
  + consolidate_memory()             : None
  + prune_stale_nodes()              : int
  + link_orphan_nodes()              : None
  + generate_proactive_insights()    : List[Insight]
  + save_to_disk()                   : None
```

- Trigger: CPU drops below ~5 %. `on_activity_detected` **cancels** `idle_task` within one tick.
- Two jobs:
  1. **Reminiscence** — replay old graph nodes weighted by score, adjust `relevance_score`s, flag stale nodes. Score → 0 makes a node a candidate for graph cleanup (and, in the deferred pruning work, for head removal).
  2. **Imagination** — pull the top-K nodes, run local inference to generate candidate fixes / alternative approaches / follow-up queries, cache as "idle thoughts" (`idle_cache.db`). Surface once on return.
- Idle thoughts expire after 48 h.
- Do graph work in bounded transactions under the lock, one atomic call at a time, so a cancellation mid-cycle never leaves the graph half-mutated.

---

## 9. L9 · Interface (V)

```
InterfaceLayer «Module»                    Message «dataclass»
  - event_bus           : EventBus           role             : str
  - graph_memory        : GraphMemory        content          : str
  - bitnet_runtime      : BitNetRuntime      related_node_ids : List[str]  (B1 — not UUID)
  - conversation_history: List[Message]      timestamp        : datetime
  - max_history_length  : int
  - channel             : InterfaceChannel   InterfaceChannel
  + on_user_input(raw_text)              : None    CLI
  + on_insight_generated(event)          : None    WEB_SOCKET
  - _build_context(query)  : List[Node]           NOTIFICATION_ONLY
  - _generate_response(query, context)   : str  «async»
  + send_to_user(message)                : None
  - _store_message(role, content)        : None
```

- The only module that talks to the human. Filters noise, delivers smart notifications, formats `$` responses, drives a tray icon and a daily report.
- `conversation_history` is **RAM-only** — never persisted.
- `_build_context()` is retrieval: query → candidate nodes → `find_related()` → rank by `relevance_score` → truncate to `max_context_tokens`.
- Shell prefixes: `$` ask · `$?` diagnose (project context + live snapshot) · `$!` emergency (skips Y + Z, immediate action) · `$$` safe (backup + verify, never during tests).

---

## 10. L10 · Orchestration

```
NeuroPACAOrchestrator «entry point»
  - event_bus     : EventBus
  - graph_memory  : GraphMemory
  - bitnet_runtime: BitNetRuntime
  - modules       : List[BaseModule]
  - config        : Config
  - is_running    : bool
  + initialize()   : None
  + start()        : None
  + stop()         : None
  + health_check() : SystemHealth
```

**Startup:** load + validate `Config` → `GraphMemory.load()` → `BitNetRuntime.load_model()` (lazy option) → `EventBus.start()` → construct all 8 modules with injected references → `initialize()` each → `start()` in dependency order (L2 → L3 → L4 → L5 → L7 → L6 → L9) → start background timers (graph save, pressure decay, score recalculation).

**Shutdown (SIGTERM):** stop collectors → cancel `idle_task` → drain queue → `stop()` each module → `EventBus.stop()` → `GraphMemory.save()` → `unload_model()`.

---

## 11. Model stack — BitNet b1.58 2B4T + llama.cpp

**The model: BitNet b1.58 2B4T.** Every weight ∈ {−1, 0, +1}, 1.58 bits/param. Matrix multiplications become additions and subtractions — no floating point at inference. ~0.4 GB of weights, ~1.1 GB resident with runtime overhead; runs on a laptop CPU without thermal throttling. This is the only released, production-usable BitNet b1.58 checkpoint — larger BitNet sizes exist only as paper results.

**Serving: llama.cpp, in-process.** `BitNetRuntime` owns `model` + `tokenizer` directly and calls llama.cpp — **not** an HTTP call to Ollama. One inference at a time system-wide (`_inference_lock`); `infer_async()` offloads the blocking call to `run_in_executor`. This choice is partly historical: an earlier design served the model through Ollama, which cannot expose model internals — see `pruning.md §3.3` for why that mattered.

**Keeping a 2B model coherent.** A 2B / 1.58-bit model drifts on open-ended prompts over graph context. Every structured call runs against a task-specific **GBNF grammar** (a llama.cpp feature), with `cited_nodes` locked to an enum of the exact node IDs in the prompt, an `abstain` path in every schema, distilled input (≤ 5 nodes), greedy decoding, and a hard post-generation validation gate. Full rules: `rules.md §4.1`; the risk and the B0 test: `problems.md` 1.13.

> **Personal weight pruning — deferred.** The project's original thesis is that graph `relevance_score`s drive which attention heads of this model survive, via a static `domain_to_heads` table, producing a sparse per-user model. This is the riskiest part of the project and 2B4T already fits a laptop without it. It is **deferred to the end of the roadmap** — full design, the `load_gguf_with_mask` mechanism, and open questions in [`pruning.md`](pruning.md). Nothing in L1–L10 depends on it; only `BitNetRuntime.load_model()` would later gain an optional `mask` argument.

---

## 11b. L7 Action & L8 Agents *(reconstructed)*

The class diagram is cut off at the right edge; L7 and L8 have no class boxes. Their existence is confirmed by: the concrete-actions note (*Notification · FileWrite · RunCommand · ApiCall · Memory*), `EventType` members `ACTION_TRIGGERED` / `AGENT_SPAWNED` / `AGENT_COMPLETED`, and `Config` fields `action_log_path` / `max_concurrent_agents`.

**L7 · Action.** `BaseAction` contract (`validate()`, `dry_run()`, `execute()`, `rollback()`). Concrete actions: `NotificationAction`, `FileWriteAction`, `RunCommandAction`, `ApiCallAction`, `MemoryWriteAction`. Every effect runs behind a safety-check gate — sandboxed execution, backup before write, rollback available. Dangerous actions require terminal confirmation regardless of pressure. `ApiCallAction` is the only component permitted an outbound socket, disabled by default. Every attempt is appended to `action_log_path` as JSONL. Emits `ACTION_TRIGGERED`.

**L8 · Agents.** `AgentSupervisor` spawns bounded multi-step tasks capped by `Config.max_concurrent_agents`, each with a wall-clock and inference budget. Also the home of **structural plasticity** — `spawn_node()` / `kill_node()` on the EventBus, spawning temporary sub-clusters on load spikes and killing them (apoptosis) after `idle_ttl = 14d` of no firing. Emits `AGENT_SPAWNED` / `AGENT_COMPLETED`.

Confirm both against a full-width re-export of the diagram before building them.

---

## 12. Blueprint reconciliation

### v3/v2 → v4

| Concern | Earlier | **v4** |
| --- | --- | --- |
| Module naming | X/Y/Z/W/V managers with numbered sub-agents (X1–X5, …) | 8 `BaseModule`s across L2–L9 |
| Inference serving | Ollama HTTP at `localhost:11434` | In-process `BitNetRuntime` via llama.cpp (tech-fix, §11) |
| Concurrency | threads + `threading.Lock` | single event loop + `asyncio.Lock` + executor for blocking calls |
| Idle cognition | "Reminiscence Agent" (DB housekeeping only) | `DefaultModeNetwork` — housekeeping **and** imagination |
| Action layer | binary manual approval for everything | Action gradients — low/high pressure thresholds |
| Architecture shape | 5 hard-coded permanent managers | structural plasticity — nodes spawn and undergo apoptosis |
| Sparsity/pruning | inference-time, via Ollama | mask applied once at model load, via llama.cpp — **deferred** (`pruning.md`) |
| Weekly model training | catastrophic-forgetting mitigation | out of scope for the core system; folded into `pruning.md` |

### Known gaps in the diagram

| Gap | Action |
| --- | --- |
| L7 and L8 cut off at the right edge | §11b is reconstructed. Re-export at full width and reconcile before building them. |
| `BaseModule` lifecycle not detailed | Defined in §3.7. |
| `SystemHealth` / `ModuleHealth` shapes undefined | Define in the core-infra phase. |
| Node/Edge/enum drift between the diagram and the concept HTML's inline code | The diagram wins. Concept variants noted inline above. |
| Naming drift (`BitNetPlasticity` vs `BehaviourPlasticity`, `XMetricCollector` vs `XMetricsCollector`, `SignalCorrelator` vs `SignalCorrelation`) | Use the diagram's names. |

---

## 13. Event catalogue

| Event | Publisher | Subscribers | Payload |
| --- | --- | --- | --- |
| `METRIC_COLLECTED` | L2 | L3 | `MetricSnapshot` |
| `IDLE_DETECTED` | L2 | L6 | idle duration, last activity |
| `ACTIVITY_DETECTED` | L2 | L6 | activity source |
| `SIGNAL_CORRELATED` | L3 | **L4 and L5, independently** | `Signal` |
| `PATTERN_DETECTED` | L3 | L9 (optional) | pattern name, confidence |
| `INSIGHT_GENERATED` | L4 | L5, L9 | `Insight` |
| `PRESSURE_THRESHOLD_REACHED` | L5 | L7 | `PressureEntry` |
| `ACTION_TRIGGERED` | L7 | L9, L4 | action, result |
| `MEMORY_UPDATED` | the mutating module (L3 / L6 / L9) | L9 (optional) | `{node_ids: List[str], operation: str}` |
| `USER_MESSAGE` | L9 | L4 (optional) | text, timestamp |
| `AGENT_SPAWNED` / `AGENT_COMPLETED` | L8 | — | agent id, goal, outcome |
| `SYSTEM_ERROR` | any | L9, L10 | module, exception, severity |

---

## 14. Concurrency & load tiers

**One process. One event loop. One inference executor.**

| Work | Where it runs |
| --- | --- |
| Event dispatch, graph reads/writes | Event loop (graph under `asyncio.Lock`) |
| `collector.collect()` | `asyncio.to_thread` |
| `BitNetRuntime.infer()` | dedicated single-worker `run_in_executor` |
| Graph `save()` | `asyncio.to_thread` |
| DMN idle cycle | cancellable `asyncio.Task` |
| `watchdog.Observer` | its own thread; callbacks marshal to the loop |

**Load tiers:**

| Tier | When | Cost |
| --- | --- | --- |
| Sensing | always on | ≈ 0 % CPU — logging to buffer, no inference |
| Inference | on `$` command, or an insight/idle thought | small spike, BitNet is fast, stops immediately |
| Score recompute + graph consolidation/prune | idle / night | heavier — DMN, only while you're away |

*(A "weekly model training" tier existed in the concept; it belongs to the deferred pruning work — `pruning.md`.)*

---

## 15. Files (target)

```
neuropaca/
├── 1000071408.png · neuropaca-v4.html · neuropaca-overview.html   # the blueprint
├── PRD.md · Architecture.md · rules.md · phases.md · design.md · memory.md · problems.md · pruning.md
├── src/neuropaca/
│   ├── daemon.py
│   ├── core/         # L1 — event_bus, graph_memory, bitnet_runtime, config, base_module, enums
│   ├── sensing/      # L2 — collector_module, base_collector, collectors/
│   ├── diagnosis/    # L3 — correlator, base_pattern, patterns/, signal, insight
│   ├── learning/     # L4 — plasticity, prompts.py  (all prompt strings live here)
│   ├── drive/        # L5 — pressure_accumulator, pressure_entry
│   ├── idle/         # L6 — dmn, consolidation
│   ├── action/       # L7 — base_action, actions/, audit
│   ├── agents/       # L8 — supervisor, structural plasticity
│   ├── interface/    # L9 — layer, message, channels/, formatting (see design.md)
│   └── orchestration/# L10 — orchestrator, scheduler
├── tests/
├── experiments/      # DEFERRED — pruning/ spike lives here first (see pruning.md); not shipped
└── data/             # gitignored — graph.json, idle_cache.db, actions.jsonl, logs
```

Package directory names map 1:1 to layers. All prompt strings live in `learning/prompts.py`. The eventual `src/neuropaca/sparsity/` package is deferred (`pruning.md`) and is never imported by the running daemon.

---

*Related: [PRD.md](PRD.md) · [rules.md](rules.md) · [phases.md](phases.md) · [design.md](design.md) · [memory.md](memory.md)*
