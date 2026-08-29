# PRD — NeuroPACA v4

**Product:** NeuroPACA — Neuromorphic Personal Autonomous Computing Agent
**Status:** Research concept / prototype
**Owner:** Jatin Bhanot (Chitkara University, 2026)

Derived from `neuropaca-v4.html`, `neuropaca-overview.html`, and the v4 class diagram (`1000071408.png`).

---

## 1. One-line definition

> NeuroPACA is a local-first, always-on agent that watches how you work, builds a behavioural graph of your habits, and uses that graph to give grounded, personal answers from a local BitNet b1.58 2B4T model — with zero cloud dependency. *(A later phase adapts the model's weights to your domain too — deferred, see [`pruning.md`](pruning.md).)*

## 2. The problem

Most people adapt to their machine. NeuroPACA flips it: your machine adapts to you. Flat log storage grows forever, has no relationships, is a standing privacy risk, gets slower over time, and gives everyone the same generic answer. A graph fixes all of that — relationships are first-class, raw data trains then gets purged, and queries traverse meaning instead of scanning rows.

## 3. The thesis — one score, several jobs

Every node in the graph carries a `relevance_score` (0–10), a normalised composite of four usage signals:

```
score = normalize(
      frequency          * 3.0    # how often you use it
    + decay(last_seen)   * 3.0    # how recently
    + log(connections)   * 2.0    # how connected in the graph
    + bridge_value       * 2.0    # does it link different domains
)
```

That one score governs **three things**:

1. **Retention** — low-score, long-untouched nodes are pruned from the graph.
2. **Memory replay** — the Default Mode Network replays high-score nodes often, low-score nodes rarely.
3. **Retrieval ranking** — when the interface builds context for a `$` query, candidate nodes are ranked by score before truncation.

**The deferred fourth job — model weight pruning.** The project's original and most ambitious idea is that the *same* score also decides which attention heads of the local model get cut, producing a sparse model shaped around your actual work. This is the riskiest part of the project and BitNet 2B4T already fits a laptop without it, so it is **deferred to the end of the roadmap**. Full design, rationale, and open questions: [`pruning.md`](pruning.md).

## 4. Scope

NeuroPACA is a Python daemon organised into **10 layers / 8 modules** that communicate only through an async `EventBus` — nothing calls anything else directly. Layers: L1 Core, L2 Sensing, L3 Diagnosis, L4 Learning, L5 Drive, L6 Idle Cognition, L7 Action, L8 Agents, L9 Interface, L10 Orchestration. (The older concept called the module layer `X · Sensing`, `Y · Diagnosis`, `Z · Learning`, `W · Action`, `V · Comms`.)

Single user, single machine, single graph. CPU-only inference. No GPU.

## 5. Target user

A developer who works long hours at a personal machine, lives in the terminal, and does not want their working context sent to a third-party API. NeuroPACA answers *about their machine and their work* — it is not a general chatbot.

## 6. Core features

### F1 · Passive OS sensing
Monitors CPU, RAM, disk, temperature, processes, network, and system logs every 60 s via `psutil` / `/proc` / shell hooks / editor extension APIs. No inference in this layer — just reads and publishes `MetricSnapshot`s to the EventBus. Does not touch screen contents or keystrokes.

### F2 · Behavioural pattern correlation
`SignalCorrelator` matches recent snapshots against a registry of `BasePattern`s and emits typed `Signal`s: `HIGH_LOAD` (CPU > 90 % for 5 min), `FOCUS_SESSION` (high CPU + coding app > 20 min), `DISTRACTION` (app switching > 5× in 2 min), `IDLE` (no input > threshold). Rule-based; the local LLM is only consulted for complex cases.

### F3 · Unified graph memory
`GraphMemory` wraps a `networkx.MultiDiGraph` (B1 decision — parallel edges carry
distinct `RelationType`s between the same pair; see `Architecture.md §3.2`). 10 master domain nodes (Engineering, Research, Tools, System, Habits, Projects, Meetings, Comms, Mental Models, Learning) plus a central `YOU` routing hub. New data is classified into a domain first, then placed in the right cluster — the routing layer collapses an O(n) comparison. Edges strengthen by Hebbian co-occurrence (`weight += ~0.01` when events recur together). `relevance_score` decays when unused, spikes on heavy use.

### F4 · Local BitNet inference
`BitNetRuntime` runs **BitNet b1.58 2B4T** in-process via **llama.cpp** — weights ∈ {−1, 0, +1}, 1.58 bits/param, CPU-native, no GPU. ~0.4 GB of weights, ~1.1 GB resident with runtime overhead, one inference at a time system-wide. Used by L3 (complex cases only), L4 (insight generation), L6 (idle thoughts), and L9 (`$` responses). Personal weight pruning is deferred — see [`pruning.md`](pruning.md).

### F5 · Default Mode Network (idle cognition)
When CPU drops below ~5 % (you walked away), the DMN pulls the top-K graph nodes and uses the local model to autonomously generate "idle thoughts" — candidate fixes, alternative approaches, follow-up queries — cached to `idle_cache.db`. On return, the system surfaces them once. Idle thoughts expire after 48 h.

### F6 · Action gradients (pressure-based autonomy)
`PressureAccumulator` replaces manual approval with activation thresholds. A **low threshold** (1 spike from Diagnosis) fires safe actions silently — e.g. clearing a stale cache. A **high threshold** requires synchronised spikes from Sensing + Diagnosis + Learning simultaneously; if that corroboration isn't met, the system prompts you in the terminal instead of acting. Pressure decays ~50 %/min when signals stop arriving.

### F7 · Safety-gated action execution
The Action layer runs every effect behind a safety-check gate: sandboxed execution, backup before any write, rollback available. Dangerous actions (killing processes, running commands) always require terminal confirmation — no threshold removes that gate.

### F8 · Structural plasticity
The EventBus can `spawn_node()` and `kill_node()` dynamically. A CPU spike spawns a temporary cluster of sub-sensing nodes (e.g. thermal watchers); when they haven't fired for 14 days they undergo apoptosis. The architecture shapes itself around active projects rather than a fixed blueprint.

### F9 · Terminal-native interface
The only surface that talks to the human. Shell prefix grammar:

| Prefix | Meaning |
| --- | --- |
| `$` | Ask — natural language, graph context injected |
| `$?` | Diagnose — question using current project context + live snapshot |
| `$!` | Emergency — immediate autonomous action, skips Y + Z |
| `$$` | Safe — full backup + verify before any action, never during tests |

### F10 · Scheduled graph maintenance
During idle/sleep the DMN consolidates duplicate nodes, re-links orphans, recomputes `relevance_score`s, and purges raw sensor buffers past their TTL. This is graph housekeeping, not model training — the model is used as-is. Any weekly model adaptation belongs to the deferred pruning work ([`pruning.md`](pruning.md)).

## 7. Non-goals

| Not building | Why |
| --- | --- |
| Cloud sync, accounts, telemetry | Privacy is the product |
| GPU inference | CPU viability via BitNet is the premise |
| A general chatbot | Answers are grounded in your graph |
| Multi-user / fleet | Single user, single machine, single graph |
| Recording screen or keystrokes | Only cold system numbers |
| Runtime model training / fine-tuning | Out of scope for the core system; see `pruning.md` |

## 8. Privacy

1. Zero cloud calls. Nothing leaves the machine.
2. No screen capture, no keystroke logging.
3. Raw sensor data is buffered briefly, then purged — only extracted graph knowledge persists.
4. Idle thoughts expire after 48 h.
5. Conversation history is RAM-only.

## 9. Model footprint

BitNet b1.58 2B4T is ~1.1 GB resident (~0.4 GB weights) — fits comfortably on the target machine with room for the daemon and a normal dev session. A conventional 2B model in float32 would be ≈ 8 GB. Further shrinking via personal pruning is deferred ([`pruning.md`](pruning.md)).

## 10. Competitive position

NeuroPACA isn't a competing agent runtime (Hermes Agent, OpenClaw). It's a **behavioural data layer** — passive OS-level sensing plus a personal graph — that agent frameworks could plug into. Its moat is starting from OS signals and running on a laptop CPU with complete privacy.

## 11. Risks

| # | Risk | Mitigation |
| --- | --- | --- |
| R1 | Does BitNet b1.58 2B4T actually run within the RAM/latency budget on the target machine, with coherent output? | The B0 de-risking spike measures it before any module code is written. Everything above L2 depends on the answer. |
| R2 | An autonomous action damages user data | Safety gate: sandbox, backup, rollback; dangerous actions need terminal confirmation regardless of pressure |
| R3 | DMN competes for CPU with real work | Idle-only trigger (CPU < 5 %); aborts on activity |
| R4 | Graph grows unbounded | Routing layer + score decay + `prune_low_score` + raw-buffer purge |
| R5 | Domain classification of raw activity is brittle | Ship an editable `app_map.yaml` (process → domain); the graph self-corrects via co-occurrence; never classify in the polling loop |
| R6 | The reconstructed L7/L8 shapes are wrong | Re-export the class diagram at full width before building them; build safe actions first |
| — | *Pruning-specific risks live in `pruning.md` §5, not here — that work is deferred.* | |

---

*Related: [Architecture.md](Architecture.md) · [rules.md](rules.md) · [phases.md](phases.md) · [design.md](design.md) · [memory.md](memory.md)*
