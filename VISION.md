# VISION.md — NeuroPACA: a living machine

> **v2 · rewritten 2026-09-12.** The north star, the cognitive architecture that
> gets us there, the mathematics behind each part, the local model stack, and the
> phased plan. Companion to [`phases.md`](phases.md) (how the mechanism was built),
> [`Architecture.md`](Architecture.md), [`rules.md`](rules.md) and
> [`RESEARCH_DOSSIER.md`](RESEARCH_DOSSIER.md) (every measurement, and the history
> of the twelve graph defects fixed in v1 — §21.8–21.12).
>
> Phase IDs: **A-series** (*Aliveness* — how it feels to live with) and
> **S-series** (*Secretary* — what it does for you). They never collide with the
> B-series in `phases.md`.

---

## 1. The north star

> *I close the lid at night. In the morning the laptop greets me: "Welcome back.
> Yesterday you left the scoring refactor half-done — the failing test is
> `test_bridge_value`. Maya replied to the thread you started on Tuesday. And
> while you were away I kept wondering why you always open your notes right after
> a long terminal session — should I have them ready next time?"*
>
> *I never asked it anything. It just knew me — and when I asked "how do you know
> that?", it showed me.*

**In plain words.** Not a chatbot bolted onto the desktop, and not a smarter
search box. A computer that feels **alive**: it notices what you do, remembers it
across days, thinks about it while you are away, speaks at the right moment and
stays silent at the wrong one, grows as you change, and slowly earns the right to
act for you. Everything it knows lives on your machine — and you can open its mind
and look inside.

### 1.1 Beyond Jarvis

Jarvis is the reference everyone has. It is the wrong target, because a film
assistant gets to cheat on everything that is hard.

| | Jarvis | NeuroPACA |
| --- | --- | --- |
| **Where it lives** | someone else's servers | your laptop — zero bytes leave it |
| **Its mind** | a black box you trust or don't | **a graph you can click, read, correct and delete** |
| **When it thinks** | when spoken to | **continuously** — it consolidates and wonders while you are away |
| **What it knows** | everything, by script | **only what it has seen**, with the evidence attached |
| **Who it is** | one butler for everyone | **grown from one person** |
| **Its authority** | total, on day one | **earned**, tier by tier, with a track record |
| **When unsure** | never is | **says so, or stays quiet** |

Jarvis knows everything and explains nothing. NeuroPACA knows only what it has
observed and can always say how. That is harder to build and far more worth owning.

### 1.2 The six properties of a living machine

| | Property | What it feels like | Today |
| --- | --- | --- | --- |
| 1 | **Notices** | it knows what you are doing right now | ✅ focus, idle, load, apps, sites |
| 2 | **Remembers** | today connects to yesterday | ✅ the behavioural graph |
| 3 | **Wonders** | it has an inner life while you are away | ✅ built, but invisible |
| 4 | **Speaks** | reaches out at the right moment — and only then | ⚠️ the pipe works; it almost never speaks |
| 5 | **Anticipates** | has ready what you are about to need | ❌ |
| 6 | **Acts** | does things for you, within limits it has earned | ⚠️ safety gate built; only safe writes live |

**The core insight.** A machine that thinks but never lets you see it feels dead.
A simpler one that shows its thinking at the right moment feels alive.
**Aliveness is a problem of memory, timing and restraint — not of model size.**
That is why it can be built on one CPU laptop, and it is a testable claim (§9).

### 1.3 The ladder of aliveness

| Level | Name | The machine… | Status |
| --- | --- | --- | --- |
| 0 | Dormant | runs programs | every computer |
| 1 | Aware | sees what you are doing | ✅ |
| 2 | Remembering | keeps a lasting, learned model of your habits | ✅ |
| 3 | **Present** | shows its thinking, greets you, guards your focus | **next** — A0–A3 |
| 4 | **Useful** | resumes your threads — mail, projects, media | S0–S3 |
| 5 | **Anticipating** | prepares what you need before you ask | A4, S4 |
| 6 | **Trusted** | acts for you within earned limits | A5, S5 |
| 7 | **Companion** | a presence you would miss | the horizon |

### 1.4 The moments it lives in

Aliveness is felt in moments, not features.

- **The welcome-back.** You return from idle; one line: what it wondered while you
  were gone, and what you were in the middle of.
- **The morning.** The first unlock of a day: what is waiting, what changed, where
  you left off — two to five items, ranked, every one with its evidence.
- **The guardian.** Deep in focus, every nudge is held until you surface.
- **The mirror.** End of day: "what I learned about you today" — a new habit, two
  apps that became inseparable, a routine that broke.
- **The thread.** Open a project after a week away and its context is already
  there: last files, last error, the next step you noted.
- **The offer.** "You do this every Friday at five — want me to?" One click. After
  enough good calls, it stops asking.

---

## 2. The cognitive architecture

NeuroPACA is organised as a mind, not a pipeline. Each faculty maps to layers that
exist (L1–L10, `Architecture.md`) or to phases below. The design borrows the best
of current agent-memory research — each borrowing adapted to a single user, a
single laptop, and zero egress.

```
                 ┌──────────────────────────── the user ────────────────────────────┐
                 │                                                                   │
   SENSES ──▶ EPISODIC STREAM ──▶ SEMANTIC GRAPH ◀──▶ DREAMING (idle)               │
   L1/L2      bi-temporal events   Hebbian associations  consolidate · reflect ·     │
   focus,     "what happened,      "what goes with what" wonder · precompute         │
   idle,       when, for how long"        │                                          │
   load,                                  ▼                                          │
   sites      ATTENTION (retrieval) ──▶ PREDICTION ──▶ JUDGMENT ──▶ VOICE / HANDS ───┘
              PPR from "now"            next app, task,  speak? now?   notify, brief,
              + recency·importance      thread, time     act? which?   act (gated)
```

| Faculty | Question it answers | Borrowed from | NeuroPACA today → target |
| --- | --- | --- | --- |
| **Senses** | what is happening now? | — | ✅ L1/L2 sensing; + richer context via local plugins (S-series) |
| **Episodic stream** | what happened, and when was it true? | bi-temporal graphs — Zep / Graphiti | ❌ → every fact carries `t_valid`/`t_invalid`; superseded facts are invalidated, never overwritten |
| **Semantic graph** | what goes with what? | Hebbian learning; A-MEM's evolving links | ✅ star-shaped, saturating, decaying associations (V-1/V-2 rework) |
| **Attention** | what is relevant *right now*? | HippoRAG — Personalized PageRank; Generative Agents' recency·importance·relevance | ❌ → PPR seeded on current context |
| **Dreaming** | what should I consolidate, conclude, prepare? | Letta sleep-time compute; Generative Agents' reflection | ⚠️ L6 idle cycle exists → consolidation + reflection + precomputed briefing |
| **Curiosity** | what should I wonder about? | active inference — epistemic value / information gain | ⚠️ score-weighted sampling (V-4) → information-gain selection |
| **Prediction** | what will you do next, and when? | marked temporal point processes (Hawkes, ATPP) | ❌ |
| **Judgment** | should I speak — and now? | contextual bandits (Nurture); mixed-initiative principles | ❌ → Thompson-sampling gate with an interruption cost |
| **Voice** | how do I say it? | small local LLMs, grammar-constrained decoding | ✅ extractive templates; small model phrases grounded facts |
| **Hands** | how do I act? | Model Context Protocol (local servers) | ✅ L7 safety gate + tiers; plugins as local MCP servers |

**The rule that holds it together:** the *knowing* lives in the graph — learned
continuously, inspectable, editable, forgettable. Models are the **curiosity, the
judgment and the voice**, never the store of facts about you (§5).

---

## 3. The mathematics

Every faculty has a precise definition. Each block says what it computes, then the
plain-words version. Symbols: $w$ edge weight, $t$ time, $v$ node, $\sigma$ the
logistic function.

### 3.1 Association (built) — saturating Hebbian learning with a half-life

When two things are used together, their link strengthens, but never past 1; unused
links fade with a half-life $T_{1/2}$ of daemon uptime:

$$
w \leftarrow w + \eta\,c\,(1 - w), \qquad
w(t + \Delta t) = w(t)\, 2^{-\Delta t / T_{1/2}}
$$

$c \in [0,1]$ is time-proximity credit (the app you switched straight from counts
fully). *In plain words: "fire together, wire together — use it or lose it."*

### 3.2 Importance (built) — the relevance score

$$
\text{score}(v) = 6\,\hat a(v) + 2\,\hat s(v) + 2\,b(v) \in [0, 10]
$$

$\hat a$: log-normalised decaying activity; $\hat s$: log-normalised association
strength; $b$: how many distinct domains the node bridges. *How much this matters
to you, recently, and how central it is.*

### 3.3 Episodic truth (S0) — bi-temporal facts

Every fact $f$ carries a validity interval and an ingestion time:

$$
f = (\text{subject}, \text{relation}, \text{object}, [t_{\text{valid}}, t_{\text{invalid}}), t_{\text{seen}})
$$

A new fact that contradicts an old one closes the old interval instead of deleting
it. *It can answer "what was true last Tuesday?" and never mixes stale and current.*

### 3.4 Attention (S0) — Personalized PageRank from "now"

Seed a restart vector $e$ on what you are doing now (focused app, open project,
time-of-day node); relevance spreads through the graph:

$$
\pi = (1 - d)\, e + d\, P^{\top} \pi, \qquad d \approx 0.85
$$

$P$ is the row-normalised weighted adjacency. Final retrieval blends it with
recency and importance (the Generative Agents triple):

$$
r(v) = \alpha\, \pi(v) + \beta\, e^{-\lambda (t_{\text{now}} - t_{\text{last}}(v))} + \gamma\, \text{score}(v)/10
$$

*Starting from what you are doing, what else in your life is lit up?* Power
iteration converges in a few dozen sparse matrix-vector products — milliseconds on
a CPU at this graph's size.

### 3.5 Prediction (A4) — what you will do next, and when

Model app switches as a multivariate Hawkes process: each event makes related
events more likely for a while.

$$
\lambda_k(t) = \mu_k + \sum_{j} \sum_{t_i^{(j)} < t} \alpha_{jk}\, \beta\, e^{-\beta (t - t_i^{(j)})}
$$

$\mu_k$ is app $k$'s baseline rate; $\alpha_{jk}$ how much using $j$ excites $k$.
The next app is $\arg\max_k \lambda_k(t)$, and the expected wait follows from the
total intensity. Parameters are fit by maximum likelihood, online, on the event
log — and $\alpha$ is the directed, time-aware cousin of the Hebbian weight.
*"After a long terminal session, you open your notes within ten minutes."*

### 3.6 Judgment (A3) — should it speak, and now?

Each kind of moment $m$ (welcome-back, nudge, offer) in context $x$ (focus state,
time of day, recent dismissals) has an unknown chance of being welcome. Thompson
sampling keeps a posterior per arm and speaks only when the sampled benefit beats
the cost of interrupting:

$$
\tilde p \sim \text{Beta}(a_{m,x}, b_{m,x}), \qquad
\text{speak} \iff \tilde p \cdot V(m) > C_{\text{interrupt}}(x)
$$

Accepted → $a \mathrel{+}= 1$; dismissed → $b \mathrel{+}= 1$; ignored → a small
$b$ increment. $C_{\text{interrupt}}$ is high during a focus session and low right
after one ends. A daily budget $B$ caps the total. *It learns your tolerance from
your reactions — and stops doing what you wave away.*

### 3.7 Curiosity (A2) — what to wonder about

Treat each uncertain association as a Beta posterior over "these belong together".
While idle, wonder about the pair with the highest **expected information gain**,
the epistemic term of active inference's expected free energy:

$$
\text{IG}(u, v) = \mathbb{H}\!\left[\,p(w_{uv})\,\right] - \mathbb{E}_{y}\,\mathbb{H}\!\left[\,p(w_{uv} \mid y)\,\right]
$$

*It is curious about exactly what it doesn't yet understand about you* — not about
what it already knows (the old echo chamber), nor about noise.

### 3.8 The mirror (A2) — what changed today

Compare today's routine distribution $Q$ (time in each app, each hour) with the
learned baseline $P$; report only surprising shifts:

$$
\text{surprise} = D_{\mathrm{KL}}(Q \,\|\, P) = \sum_i Q_i \log \frac{Q_i}{P_i}
$$

and the top contributors $Q_i \log(Q_i / P_i)$ become the day's sentences. *Only
what actually changed about you, not a log dump.*

### 3.9 The briefing (S0) — choosing what fits

Pick at most $k$ items to maximise value while avoiding repetition — a monotone
submodular objective, so greedy selection is within $(1 - 1/e)$ of optimal:

$$
\max_{|S| \le k}\; \sum_{i \in S} r(i) \;-\; \mu \sum_{i \ne j \in S} \text{sim}(i, j)
$$

*Two to five items, the most important ones, not five versions of the same one.*

---

## 4. The local stack

All of it runs on one laptop, CPU only, zero egress. Choices marked *candidate*
are settled by a benchmark spike before adoption, never by a blog post.

| Role | Today | Target / candidates |
| --- | --- | --- |
| Inference runtime | llama.cpp (in-process) | llama.cpp — GBNF grammar-constrained decoding for every structured call |
| Always-on loop model | BitNet b1.58 2B4T | *candidate:* current 1–4B small models (Qwen3 series, Gemma family, Phi-4-mini) — judged on grammar-bound extraction accuracy, RAM and joules, not chat quality |
| Voice / phrasing model | Qwen2.5-3B Q4 | *candidate:* a newer ~3–4B instruct model; still phrases only grounded facts |
| Semantic similarity | none | *candidate:* EmbeddingGemma-300M (under 200 MB RAM quantised, Matryoshka dimensions) for titles, notes, thread matching |
| Graph + math | networkx, own code | + sparse PPR (scipy-free power iteration), online Hawkes MLE, Beta-Bernoulli bandit — all dependency-light |
| Plugins (senses & hands) | built-in collectors | **local MCP servers over stdio** — mail, calendar, projects, media as plugins behind one contract; no remote servers, ever |
| Desktop surface | `notify-send` → org.freedesktop.Notifications | + tray presence (A1), briefing view |
| Speech (A6, stretch) | none | *candidate:* whisper.cpp / Moonshine in; Kokoro-82M out |

New runtime dependencies still need approval, one at a time (`rules.md §9`).

---

## 5. Where learning happens — and where it must not

| What is learned | Where it lives | How it learns |
| --- | --- | --- |
| Facts about you (what, when, with what) | the graph | continuously, from observation |
| What goes with what | edge weights | Hebbian (§3.1) |
| What you'll do next | Hawkes parameters | online maximum likelihood (§3.5) |
| When you welcome interruption | bandit posteriors | your reactions (§3.6) |
| What matters to you | a small learned ranker over retrieval features | accept / dismiss / ignore (S5) |
| How you like things phrased | *optional, later:* a style LoRA | never facts — style only |

**Facts about you are never baked into model weights.** Weights cannot be inspected,
edited, or made to forget — and "you can always see and wipe what is stored" is
non-negotiable (§8). A week of one person's life is also far too little data to
fine-tune a language model without it memorising noise. The graph *is* the
learned model of you; the models reason over it.

---

## 6. The principles

1. **Grounded.** Every sentence it says points at graph evidence. Extractive
   before generative; a machine that is confidently wrong is not lively, it is
   unsettling.
2. **Local.** Its mind is on your disk. Zero egress, proven in CI, with one
   opt-in exception per plugin the user explicitly configures (a mail provider).
3. **A visible mind.** Any node can be clicked and read — its numbers, its links,
   and in plain English what the system is learning from it.
4. **Earned autonomy.** Observe → suggest → one-click act → autonomous, tier by
   tier. Each step up needs a track record; dangerous actions always ask.
5. **Attention is sacred.** A daily budget, silence during focus, and every
   dismissal teaches restraint.
6. **It can forget.** Per-domain retention, `forget <entity>`, and `panic`.
7. **One inference at a time.** Everything shares one inference lock and yields
   to the user.

---

## 7. Where we are (2026-09-12)

**Level 2 — Remembering — reached.**

- Sensing: focus, idle, CPU/RAM census, web-app attribution (Wayland, COSMIC).
- Memory: the behavioural graph (schema v8) — Hebbian associations, a real
  relevance score, sighting times, provenance (probes cite their insights).
- Idle cognition: the DMN consolidates and asks itself grounded questions.
- Action: a tiered safety gate, audited; live on this machine for the safe tier;
  notifications reach the desktop.
- Visibility: the graph view with a per-node panel explaining what the system
  learns from each node; `neuropaca doctor`, `tell`, `overview`.
- Reliability: the daemon survives compositor outages and session restarts; the
  7-day soak restarted on this build on 2026-09-11.

All twelve graph defects identified in v1 of this document are fixed and on
`main`; their diagnoses and measurements live in `RESEARCH_DOSSIER.md` §21.8–21.12.

**Open:**
- The notification path has never fired on real use — high-tier pressure needs two
  corroborating sources, and nothing yet produces moments worth saying (A0 fixes the
  second half).
- The L9 socket file once vanished from a running daemon (2026-09-11); cause unknown.
- The episodic stream (§3.3) does not exist yet — everything above Level 3 needs it.

---

## 8. What must not change

- **Zero egress** except explicitly configured, opt-in plugin backends; CI-proven.
- **No module imports another module** — layers talk over the event bus; plugins
  register as data (or as local MCP servers).
- **The user can always see and wipe what is stored** — `tell`, `forget`, `panic`,
  `doctor`, the graph view.
- **Extractive before generative** — a wrong factual claim is a release blocker.
- **One inference at a time**, yielding to the user.

---

## 9. How we will know it is alive

**The product test (30-day dogfood):**
- **It stays on.** Nudges not disabled after 30 days.
- **It is welcome.** Accepted + kept moments outnumber dismissals, and the
  dismissal rate falls across the month (the bandit is learning).
- **It is never wrong out loud.** Zero ungrounded claims.
- **It guards attention.** Within budget; none land mid-focus.
- **It anticipates.** Next-app top-3 accuracy beats the "most frequent app" baseline.
- **It would be missed.** Final question: *would you want it back if it were gone?*

**The research claims (the paper):**

| # | Hypothesis | Test |
| --- | --- | --- |
| H1 | A graph + retrieval memory beats a fine-tuned small model for knowing one user | briefing precision/recall vs a hand-labelled set, both variants, same 30 days |
| H2 | A Thompson-sampling judge lowers dismissals without lowering acceptance | A/B across weeks against a fixed-rule gate |
| H3 | Hawkes prediction beats frequency and Markov baselines for next app and timing | log-likelihood + top-k accuracy on held-out days |
| H4 | Information-gain curiosity produces more *useful* idle thoughts than score sampling | user rating of surfaced thoughts |
| H5 | Aliveness comes from timing and memory, not model size | swap the voice model 1B ↔ 4B; aliveness measures should barely move |

---

## 10. The plan

> The detailed, phase-by-phase build plan — spikes, design down to modules and
> events, math, load budgets, tests and exit gates — is in
> [`VISION_PHASES.md`](VISION_PHASES.md). This section is the summary.

Every phase follows the same four steps: **spike** (surface blockers before
production code, ruled go / no-go) → **build** → **test** (unit + integration, plus a
dogfood window where the value only shows over days) → **exit** (an explicit
checklist).

```
A0  Welcome-back moment            ← first: everything it needs exists today
S0  Episodic stream + attention + briefing core   (§3.3, §3.4, §3.9)
A1  Living presence — the tray mind
A2  Curiosity + the mirror          (§3.7, §3.8)
A3  The guardian — bandit judgment + attention budget   (§3.6)
S1  Correspondence plugin (mail, local MCP server, the one opt-in backend)
S2  Projects plugin (repos, files, next steps)
S3  Media & continuity plugin
A4  Anticipation — Hawkes prediction, things ready before you ask   (§3.5)
S4  The plugin contract, generalised (calendar, reading, bills, habits…)
S5  Judgment learns — the reaction-trained ranker
A5  Earned action — suggest → one-click → autonomous, tier by tier
A6  Voice (stretch) — local speech in and out
S6  Hardening, retention, and the evaluation paper
```

### A0 · The welcome-back moment
**Goal.** Returning from idle produces one grounded line: what it wondered while
you were away, and what you were in the middle of.
**Build.** On `ACTIVITY_DETECTED` after an idle spell: pick the best idle thought
of the spell (stored question text) and the last focused thread; send through the
existing desktop path; rate-limited; silent if the spell was short or nothing new.
**Exit.** Fires on real returns; zero ungrounded lines; user keeps it on for a week.

### S0 · Episodic stream, attention, briefing core
**Goal.** Level 4's foundation: "what happened, when, and what matters now".
**Build.** Bi-temporal event store beside the graph (§3.3); PPR attention seeded
from the current context (§3.4); submodular item selection (§3.9); the briefing
surface (first unlock of the day, and on demand).
**Exit.** "What was I doing Tuesday afternoon?" answers correctly from the store;
the briefing picks 2–5 items with evidence; retrieval under 50 ms.

### A1 · Living presence
**Goal.** You can glance at it and see it is there. A tray mind showing state
(focused / idle / thinking / noticed something) and today's thoughts; one click
opens the graph view on what it is thinking about.
**Exit.** State is always true to the daemon; zero cost when hidden.

### A2 · Curiosity and the mirror
**Goal.** Idle thoughts chosen by information gain (§3.7); an end-of-day "what I
learned about you" built from KL surprise (§3.8).
**Exit.** H4 measured; the mirror only reports real changes (no change → no message).

### A3 · The guardian
**Goal.** It speaks only when welcome. Thompson-sampling gate over moment types
and contexts, interruption cost from the focus detector, a daily budget (§3.6).
**Exit.** Nothing mid-focus; dismissal rate falls over two weeks (H2).

### S1 – S3 · The first three domains
Correspondence ("Maya replied to your Tuesday thread"), projects ("you left the
refactor at `test_bridge_value`"), media ("episode 7, season 2"). Each is a local
plugin feeding the episodic stream; mail is the single opt-in outbound path,
sandboxed and off by default.
**Exit (each).** The domain's briefing items are correct on a labelled set; adding
it touched no core code.

### A4 · Anticipation
**Goal.** Hawkes prediction of next app, task and timing (§3.5), used to have
things ready — the right notes open, the right thread on top.
**Exit.** H3 beats the baselines; pre-warming is invisible when wrong.

### S4 – S5 · Generalise, then learn judgment
The plugin contract (local MCP servers) so calendar, reading lists, bills and
habit streaks are just plugins; then the ranker that learns from your reactions.
**Exit.** A fourth domain touches only its plugin; ranking improves week over week.

### A5 · Earned action
Offers become one-click actions, and — only after a track record per action type —
autonomous ones within the safe tier. Dangerous actions always ask.
**Exit.** Zero unwanted actions; every autonomous action reversible and audited.

### A6 · Voice (stretch)
Local speech in and out, only if it stays local, grounded and interruptible.

### S6 · Hardening and the paper
Retention and forgetting per domain, the 30-day evaluation (§9), and the paper.

---

## 11. Open questions (settled in the spikes)

1. **Mail backend** — local IMAP vs a provider API (metadata leaves the box) vs an
   on-disk maildir. Governs the one egress exception.
2. **Content retention** — subjects + participants + snippet, or full bodies
   (local either way)?
3. **Projects** — an explicit `.neuropaca/next` line per repo, or inferred next
   steps that will sometimes be wrong?
4. **Briefing trigger** — first unlock of the day, first start after a gap, or both?
5. **Loop model** — does a newer 1–4B model beat BitNet on grammar-bound
   extraction at equal RAM (the stack spike in §4)?
6. **Nudge budget** — what daily budget $B$ and focus cost $C_{\text{interrupt}}$
   to start the bandit from?

---

## Sources

Research this vision draws on (retrieved 2026-09-12):

- Zep / Graphiti — temporal knowledge-graph memory: [arXiv 2501.13956](https://arxiv.org/abs/2501.13956), [Graphiti docs](https://help.getzep.com/graphiti/getting-started/overview)
- HippoRAG — Personalized PageRank memory: [NeurIPS 2024](https://proceedings.neurips.cc/paper_files/paper/2024/file/6ddc001d07ca4f319af96a3024f6dbd1-Paper-Conference.pdf); HippoRAG 2: [arXiv 2502.14802](https://arxiv.org/abs/2502.14802)
- Generative Agents — memory stream, recency·importance·relevance, reflection: [arXiv 2304.03442](https://ar5iv.labs.arxiv.org/html/2304.03442)
- A-MEM — agentic, evolving memory links: [arXiv 2502.12110](https://arxiv.org/abs/2502.12110)
- Letta — sleep-time compute: [blog](https://www.letta.com/blog/sleep-time-compute/), [docs](https://docs.letta.com/guides/agents/architectures/sleeptime/)
- Proactive agents: [Proactive Agent / ProactiveBench, arXiv 2410.12361](https://arxiv.org/pdf/2410.12361), [Beyond Reactivity, arXiv 2510.19771](https://arxiv.org/abs/2510.19771), [Anticipate and Learn — idle-time compute in proactive agents, arXiv 2605.25971](https://arxiv.org/pdf/2605.25971)
- App-usage prediction with temporal point processes: [ATPP, ACM TOSN](https://dl.acm.org/doi/abs/10.1145/3582555), [TGT, arXiv 2502.16957](https://arxiv.org/pdf/2502.16957)
- Notification timing with reinforcement learning: [Nurture](https://par.nsf.gov/servlets/purl/10111008)
- Active inference and epistemic value: [PMC9019474](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9019474/), [Curiosity is Knowledge, arXiv 2602.06029](https://arxiv.org/abs/2602.06029)
- On-device embeddings: [EmbeddingGemma](https://developers.googleblog.com/en/introducing-embeddinggemma/)
- Local models and speech: [CPU-only local LLMs 2026](https://www.popularai.org/p/best-cpu-only-local-llm-2026), [local voice models 2026](https://d-central.tech/local-voice-ai-models/)
- Model Context Protocol ecosystem: [state of MCP 2026](https://chatforest.com/guides/mcp-ecosystem-2026-state-of-the-standard/)
- Classic results used without new retrieval: Thompson sampling; Horvitz, *Principles of Mixed-Initiative User Interfaces* (CHI 1999); Nemhauser–Wolsey–Fisher (1978) greedy submodular bound; Carbonell & Goldstein (1998) maximal marginal relevance; Itti & Baldi, Bayesian surprise.
