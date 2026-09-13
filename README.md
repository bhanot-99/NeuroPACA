# NeuroPACA

### Neuromorphic Personal Autonomous Computing Agent

> A local-first, always-on agent that watches how you work, builds a personal **behavioural
> graph**, thinks about it while you're away, and — carefully — starts acting like a personal
> secretary. Zero cloud calls.

| | |
| --- | --- |
| **Status** | Level 2 ("Remembering") reached. B0–B18, A0–A3, S0–S4 merged to `main`. |
| **Runs on** | One Linux laptop, CPU-only, single user. No GPU, no accounts, no telemetry. |
| **License** | [AGPL-3.0-only](LICENSE) |

---

## What it is

Most software makes you adapt to it. NeuroPACA flips that: it watches cold system signals
(focus, idle time, CPU/RAM, apps, sites, and — if you opt in — mail, calendar, projects, media)
and builds a private graph of how you actually work. While you're idle it consolidates that graph
and half-forms new thoughts about it. When something's worth saying — a welcome-back summary, an
end-of-day mirror, a briefing — a learned judgment gate decides *if* and *when* to actually
interrupt you. Every sentence it says is built from real data, never generated prose, so it's
never wrong about what it saw.

It doesn't predict what you'll do next yet, and it can't act autonomously — those are next.
**Privacy is the product**: nothing leaves the machine, and CI proves that by running tests with
network access blocked at the OS level.

## The body-map — what's built vs. planned

| Human body part | Job in the body | NeuroPaca equivalent | Status |
| --- | --- | --- | --- |
| Eyes / ears | notice what's happening now | Focus/idle/load sensors, app & site tracking, calendar + reading plugins | ✅ Built |
| Hippocampus | lay down episodic memory | Bi-temporal episodic store beside the graph | ✅ Built |
| Synapses | "fire together, wire together" | Hebbian graph edges, saturating weight + decay | ✅ Built |
| Sleep / dreaming | consolidate, forget noise, half-form ideas | Idle decay, relevance scoring, dead-hub pruning | ⚠️ Half built — mostly invisible unless it earns a moment |
| Prefrontal cortex | what's relevant right now? | Attention layer (Personalized PageRank) | ❌ Not built — briefing uses recency + overlap instead |
| Instinct / gut feeling | predict what you'll do next | Prediction faculty (Hawkes process) | ❌ Not built |
| Judgment / impulse control | should I speak, and now? | `Guardian` — Thompson-sampling gate, daily budget | ✅ Built and live |
| Mouth / vocal cords | say it out loud | Desktop notifications, extractive text only | ✅ Built, but still rare by design |
| Hands | act in the world | Tiered `SafetyGate`, sandboxed, path-confined | ⚠️ Built, restrained (safe tier only) |
| Autonomic nervous system | keep running unattended | systemd daemon, soak-hardened, self-healing sensors | ✅ Built |
| Immune system | verify identity, catch what's broken | SPDX provenance stamps, signed commits | ✅ Built |
| Object recognition | same thing, different views | `AppIdentity.resolve()` — collapses duplicate app nodes | ✅ Built |
| Correspondence memory | remember open threads with people | Mail plugin — thread state, overdue replies | ✅ Built, opt-in |
| "Where did I leave off" | resume a task exactly | Projects plugin — read-only git sensing | ✅ Built, opt-in |
| Interest tracking | remember what you read/watched | Media + reading-list plugins | ✅ Built, opt-in |
| Generalized nerve pathway | let new senses plug in easily | `PluginHost` contract — one interface for all domains | ✅ Built |
| Learning from reactions | judgment that improves over time | Reaction-trained ranker | ❌ Not built |
| Earned trust to act alone | autonomous action, tier by tier | Earned-action ladder | ❌ Not built |
| Speech | talk back, out loud | Local voice in/out | ❌ Not built (stretch) |

## How it works

Sensors → a rule-based diagnosis layer → the behavioural graph + episodic store. An idle-time
loop consolidates the graph and prunes it. Three composers (welcome-back, mirror, briefing)
propose moments from that graph; the `Guardian` gate learns per-situation whether to actually
notify you, inside a daily budget. Separately, accumulated "pressure" can cross a threshold and
trigger the tiered, audited action layer — dry-run by default, safe-tier only when live.

No module ever calls another directly — everything goes through one event bus. There is
currently **no chat/CLI surface**; a terminal was built and then deliberately withdrawn in favor
of a future voice interface. The only human-facing surfaces today are desktop notifications, a
read-only tray status icon, and an HTML graph viewer.

## Installing and running it

```bash
git clone https://github.com/bhanot-99/NeuroPACA.git
cd NeuroPACA
uv venv --python 3.12
uv pip install -e ".[dev]"
pre-commit install

uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
```

To run the daemon itself (needs a local model — see [`spikes/b0_bitnet/README.md`](spikes/b0_bitnet/README.md)):

```bash
uv pip install -e ".[dev,llama,activity]"
mkdir -p models && uv pip install huggingface-hub
huggingface-cli download microsoft/BitNet-b1.58-2B-4T-gguf --include "*.gguf" --local-dir models
# edit model_path / watch_paths in neuropaca.toml, then:
neuropacad
```

Run it automatically on login: `scripts/install-user-service.sh` (a systemd `--user` unit, no
root needed). Optional plugins (mail, projects, media, calendar, reading list) are off by default
— enable in `neuropaca.toml` with `<name>_enabled = true`; every one of them only reads local
files. Inspect the graph any time with `python scripts/neuropaca_graph.py`.

## Contributing

Read `rules.md` first — four invariants are binding: services are held not inherited, use
`asyncio.Lock` not `threading.Lock` in loop code, never call `BitNetRuntime.infer()` from a
coroutine, and no module imports another (everything is an event). Run the full local gate
(`ruff check` → `ruff format --check` → `mypy` → `pytest -q`) before pushing. Highest-value gap:
the evaluation harness (a synthetic activity generator, baselines, an ablation runner) — the
research paper can't be published without it.

---

*Local by construction. Zero cloud calls.*
