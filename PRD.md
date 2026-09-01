# PRD — NeuroPACA v4

| | |
| --- | --- |
| **Product** | NeuroPACA — Neuromorphic Personal Autonomous Computing Agent |
| **Status** | Research concept / prototype |
| **Owner** | Jatin Bhanot (Chitkara University, 2026) |
| **Derived from** | `neuropaca-v4.html`, `neuropaca-overview.html`, and the v4 class diagram (`1000071408.png`) |

---

## 1. One-line definition

> NeuroPACA is a local-first, always-on agent that watches how you work, builds a behavioural graph of your habits, and uses that graph to give grounded, personal answers from a local **BitNet b1.58 2B4T** model — with zero cloud dependency.
> *(A later phase adapts the model's weights to your domain too — deferred, see [`pruning.md`](pruning.md).)*

---

## 2. The problem

Most people adapt to their machine. **NeuroPACA flips it: your machine adapts to you.**

| Flat log storage | A behavioural graph |
| --- | --- |
| Grows forever | Raw data trains, then is purged |
| No relationships | Relationships are first-class |
| Standing privacy risk | Only extracted knowledge persists |
| Gets slower over time | Bounded by routing + score decay + pruning |
| Same generic answer for everyone | Queries traverse *your* meaning |

---

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

```mermaid
flowchart TD
    F["frequency ×3.0"] --> S
    D["decay(last_seen) ×3.0"] --> S
    C["log(connections) ×2.0"] --> S
    B["bridge_value ×2.0"] --> S
    S(("relevance_score<br/>0–10"))
    S --> J1["1 · Retention<br/>low, long-untouched nodes<br/>are pruned from the graph"]
    S --> J2["2 · Memory replay<br/>DMN replays high-score nodes often,<br/>low-score rarely"]
    S --> J3["3 · Retrieval ranking<br/>candidate nodes for a $ query<br/>ranked before truncation"]
    S -.->|DEFERRED| J4["4 · Model weight pruning<br/>same score cuts attention heads<br/>→ sparse personal model"]
```

**The deferred fourth job — model weight pruning.** The project's original and most ambitious idea is that the *same* score also decides which attention heads of the local model get cut, producing a sparse model shaped around your actual work. This is the riskiest part of the project and BitNet 2B4T already fits a laptop without it, so it is **deferred to the end of the roadmap**. Full design, rationale, and open questions: [`pruning.md`](pruning.md).

---

## 4. Scope

NeuroPACA is a Python daemon organised into **10 layers / 8 modules** that communicate **only** through an async `EventBus` — nothing calls anything else directly.

| Layer | Name | Legacy name |
| --- | --- | --- |
| L1 | Core | — |
| L2 | Sensing | X · Sensing |
| L3 | Diagnosis | Y · Diagnosis |
| L4 | Learning | Z · Learning |
| L5 | Drive | W · Action |
| L6 | Idle Cognition | — |
| L7 | Action | W · Action |
| L8 | Agents | — |
| L9 | Interface | V · Comms |
| L10 | Orchestration | — |

Single user, single machine, single graph. CPU-only inference. No GPU.

---

## 5. Target user

A developer who works long hours at a personal machine, lives in the terminal, and does not want their working context sent to a third-party API. NeuroPACA answers *about their machine and their work* — **it is not a general chatbot.**

---

## 6. Core features

```mermaid
flowchart LR
    subgraph L2["L2 · Sensing"]
        F1["F1 · Passive OS sensing"]
    end
    subgraph L3["L3 · Diagnosis"]
        F2["F2 · Behavioural pattern correlation"]
    end
    subgraph L1["L1 · Core"]
        F3["F3 · Unified graph memory"]
        F4["F4 · Local BitNet inference"]
    end
    subgraph L6["L6 · Idle Cognition"]
        F5["F5 · Default Mode Network"]
        F10["F10 · Scheduled graph maintenance"]
    end
    subgraph L5L7["L5 Drive · L7 Action"]
        F6["F6 · Action gradients"]
        F7["F7 · Safety-gated execution"]
    end
    subgraph L8["L8 · Agents"]
        F8["F8 · Structural plasticity"]
    end
    subgraph L9["L9 · Interface"]
        F9["F9 · Terminal-native interface"]
    end
    F1 --> F2 --> F3
    F3 --> F4
    F3 --> F5 --> F6 --> F7
    F3 --> F9
```

### F1 · Passive OS sensing
Monitors CPU, RAM, disk, temperature, processes, network, and system logs every 60 s via `psutil` / `/proc` / shell hooks / editor extension APIs. **No inference in this layer** — just reads and publishes `MetricSnapshot`s to the EventBus. Does not touch screen contents or keystrokes.

### F2 · Behavioural pattern correlation
`SignalCorrelator` matches recent snapshots against a registry of `BasePattern`s and emits typed `Signal`s. Rule-based; the local LLM is only consulted for complex cases.

| Signal | Trigger |
| --- | --- |
| `HIGH_LOAD` | CPU > 90 % for 5 min |
| `FOCUS_SESSION` | high CPU + coding app > 20 min |
| `DISTRACTION` | app switching > 5× in 2 min |
| `IDLE` | no input > threshold |

### F3 · Unified graph memory
`GraphMemory` wraps a `networkx.MultiDiGraph` (B1 decision — parallel edges carry distinct `RelationType`s between the same pair; see `Architecture.md §3.2`).

```mermaid
flowchart TD
    YOU((YOU<br/>routing hub))
    YOU --- ENG[domain:engineering]
    YOU --- RES[domain:research]
    YOU --- TOOLS[domain:tools]
    YOU --- SYS[domain:system]
    YOU --- MORE["…7 more domain hubs<br/>habits · projects · meetings · comms ·<br/>mental_models · learning"]
    ENG --> N1["app:code"]
    ENG --> N2["file:/abs/path"]
    RES --> N3["concept nodes"]
```

- **10 master domain nodes** (Engineering, Research, Tools, System, Habits, Projects, Meetings, Comms, Mental Models, Learning) plus a central `YOU` routing hub.
- New data is classified into a domain first, then placed in the right cluster — the routing layer collapses an O(n) comparison.
- Edges strengthen by **Hebbian co-occurrence** (`weight += ~0.01` when events recur together).
- `relevance_score` decays when unused, spikes on heavy use.

### F4 · Local BitNet inference
`BitNetRuntime` runs **BitNet b1.58 2B4T** in-process via **llama.cpp** — weights ∈ {−1, 0, +1}, 1.58 bits/param, CPU-native, no GPU. ~0.4 GB of weights, **~1.4 GB resident** with runtime overhead (B0 spike; §9), **one inference at a time system-wide**. Used by L4 (extractive insight classification — D-11), L6 (idle thoughts), and L9 (`$` responses). **L3 does zero inference** (B3 decision). Personal weight pruning is deferred — see [`pruning.md`](pruning.md).

### F5 · Default Mode Network (idle cognition)
When CPU drops below ~5 % (you walked away), the DMN pulls the top-K graph nodes and uses the local model to autonomously generate "idle thoughts" — candidate fixes, alternative approaches, follow-up queries — cached to `idle_cache.db`. On return, the system surfaces them once. **Idle thoughts expire after 48 h.**

### F6 · Action gradients (pressure-based autonomy)
`PressureAccumulator` replaces manual approval with activation thresholds.

| Threshold | What crosses it | Result |
| --- | --- | --- |
| **Low** | 1 spike from Diagnosis | Safe action fires silently (e.g. clear a stale cache) |
| **High** | Synchronised spikes from Sensing + Diagnosis + Learning *simultaneously* | Dangerous action; if corroboration isn't met → terminal prompt instead |

Pressure decays ~50 %/min when signals stop arriving.

### F7 · Safety-gated action execution
Every effect runs behind a safety-check gate: **sandboxed execution, backup before any write, rollback available.** Dangerous actions (killing processes, running commands) always require terminal confirmation — no threshold removes that gate.

### F8 · Structural plasticity
The EventBus can `spawn_node()` and `kill_node()` dynamically. A CPU spike spawns a temporary cluster of sub-sensing nodes (e.g. thermal watchers); when they haven't fired for 14 days they undergo **apoptosis**. The architecture shapes itself around active projects rather than a fixed blueprint.

### F9 · Terminal-native interface
The only surface that talks to the human. Shell prefix grammar:

| Prefix | Meaning |
| --- | --- |
| `$` | **Ask** — natural language, graph context injected |
| `$?` | **Diagnose** — question using current project context + live snapshot |
| `$!` | **Emergency** — immediate autonomous action, skips L3 + L4 |
| `$$` | **Safe** — full backup + verify before any action, never during tests |

### F10 · Scheduled graph maintenance
During idle/sleep the DMN consolidates duplicate nodes, re-links orphans, recomputes `relevance_score`s, and purges raw sensor buffers past their TTL. **This is graph housekeeping, not model training** — the model is used as-is. Any weekly model adaptation belongs to the deferred pruning work ([`pruning.md`](pruning.md)).

---

## 7. Non-goals

| Not building | Why |
| --- | --- |
| Cloud sync, accounts, telemetry | Privacy is the product |
| GPU inference | CPU viability via BitNet is the premise |
| A general chatbot | Answers are grounded in your graph |
| Multi-user / fleet | Single user, single machine, single graph |
| Recording screen or keystrokes | Only cold system numbers |
| Runtime model training / fine-tuning | Out of scope for the core system; see `pruning.md` |

---

## 8. Privacy

| # | Guarantee |
| --- | --- |
| 1 | Zero cloud calls. Nothing leaves the machine. |
| 2 | No screen capture, no keystroke logging. |
| 3 | Raw sensor data is buffered briefly, then purged — only extracted graph knowledge persists. |
| 4 | Idle thoughts expire after 48 h. |
| 5 | Conversation history is RAM-only. |

---

## 9. Model footprint

Two models, each **lazy-loaded** and independently self-disabling (D-12):

| Model | Role | Quant | Resident | Source |
| --- | --- | --- | --- | --- |
| **BitNet b1.58 2B4T** | always-on loop — L4 extractive insight, L6 idle thoughts | GGUF `tq2_0` | ~1.39 GB after load, ~1.55 GB after 30 min | B0 spike, 2026-08-30 (D-11) |
| **Qwen2.5-3B-Instruct** | interactive only — L9 `$` / `$?` grounded sentence | GGUF Q4_K_M | ~2.0 GB after load *(estimate; B5 target-box measurement pending)* | B5 (D-12) |

| Metric (2B4T) | Value | Source |
| --- | --- | --- |
| Of which weights | ~0.4 GB | — |
| Throughput | ~17 tok/s on 16 CPU threads | B0 spike |
| Package temp | 66–72 °C (no thermal throttle) | B0 spike |
| A conventional 2B model in float32 | ≈ 8 GB | for comparison |

**Concurrent peak ≈ 3.4 GB** — both models resident once a `$?` has been asked in a session that has also produced an L4 insight. A single `_inference_lock` still serialises every call system-wide: the two models never *run* at once, they only *reside* at once. On the 16 GB target machine this leaves ample room for the daemon and a normal dev session. The 2B4T model is not loaded until a signal passes L4 gating; the Qwen model is not loaded until the first `$` / `$?`; an idle session pays neither tax. If `interactive_model_path` is unset the `$?` path falls back to the extractive template and only the 2B4T footprint applies. Further shrinking via personal pruning is deferred ([`pruning.md`](pruning.md)).

---

## 10. Competitive position

NeuroPACA isn't a competing agent runtime (Hermes Agent, OpenClaw). It's a **behavioural data layer** — passive OS-level sensing plus a personal graph — that agent frameworks could plug into. Its moat is starting from OS signals and running on a laptop CPU with complete privacy.

---

## 11. Risks

| # | Risk | Mitigation |
| --- | --- | --- |
| **R1** | Does BitNet b1.58 2B4T actually run within the RAM/latency budget on the target machine, with coherent output? | The B0 de-risking spike measures it before any module code is written. Everything above L2 depends on the answer. |
| **R2** | An autonomous action damages user data | Safety gate: sandbox, backup, rollback; dangerous actions need terminal confirmation regardless of pressure |
| **R3** | DMN competes for CPU with real work | Idle-only trigger (CPU < 5 %); aborts on activity |
| **R4** | Graph grows unbounded | Routing layer + score decay + `prune_low_score` + raw-buffer purge |
| **R5** | Domain classification of raw activity is brittle | Ship an editable `app_map` (process → domain); the graph self-corrects via co-occurrence; never classify in the polling loop |
| **R6** | The reconstructed L7/L8 shapes are wrong | Re-export the class diagram at full width before building them; build safe actions first |
| — | *Pruning-specific risks live in `pruning.md` §5, not here — that work is deferred.* | |

---

*Related: [Architecture.md](Architecture.md) · [rules.md](rules.md) · [phases.md](phases.md) · [design.md](design.md) · [memory.md](memory.md)*
