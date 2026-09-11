# VISION_PHASES.md — the build plan for VISION.md

> Companion to [`VISION.md`](VISION.md) (v2). `VISION.md` says **what** NeuroPACA
> becomes and **why**; this file says **how**, phase by phase, down to modules,
> events, math, tests and exit gates. The B-series in [`phases.md`](phases.md) is
> how the mechanism was built; this is the A-series (*Aliveness*) and S-series
> (*Secretary*) that build on it.
>
> Written 2026-09-12. Section references like "§3.4" point into `VISION.md`.

---

## 0. How every phase runs

### 0.1 The four steps

1. **Spike** — a bounded pre-flight in `spikes/<phase>_*` whose only job is to
   surface blockers and dead ends before production code. Output: a list of
   blockers, ruled together (the `D-n` / `BL-n` pattern), and a go / no-go.
   *No phase starts its build with an open blocker.*
2. **Build** — production code, small commits, one concern each.
3. **Test** — unit + integration on every phase; a performance test wherever the
   phase touches the event loop, the graph lock or the model; a **dogfood window**
   wherever the value only shows over days.
4. **Exit** — the checklist at the end of the phase, every box ticked with evidence.

### 0.2 Definition of done (every phase)

- CI gate green: `ruff check .`, `ruff format --check .`, `mypy`, `pytest -m ""`,
  and the no-network job (`unshare --net`).
- **Zero egress still proven** — the daemon's unit keeps `PrivateNetwork=true`.
- A `RESEARCH_DOSSIER.md` chapter: what was seen, why, the approach, what was
  built, how it was proved, **what was rejected**, what is left — with numbers.
- Provenance stamped (`scripts/_provenance.py`).
- No new runtime dependency without an explicit approval (`rules.md §9`).
- Any new *action* ships dry-run first and goes live only by the user's decision.
- The running 7-day soak is never restarted without asking.

### 0.3 The load budget (every phase)

A living machine that makes the laptop feel slow is dead on arrival.

| Resource | Budget |
| --- | --- |
| Event-loop block per operation | < 5 ms (one lock cycle per mutation, yield between) |
| Idle-cycle work (DMN) | inside `dmn_cycle_wall_clock_seconds`, cancelled on activity |
| Model calls | never on the focus hot path; one inference at a time; yields to the user |
| Memory | every cache, queue and ledger bounded, the bound tested |
| Disk | episode store growth measured in the soak; retention enforced |
| Background CPU | new periodic work justified per tick, measured at the 10k-node fixture |

### 0.4 Sizes

**S** ≈ a few days · **M** ≈ one to two weeks · **L** ≈ several weeks. Sizes are
relative, for ordering — not promises.

---

## 1. The map

### 1.1 Order and dependencies

```
                ┌──────────── Level 3: PRESENT ────────────┐
A0 welcome-back ─┬─▶ A1 presence ─┐                         │
                 │                ├─▶ A3 guardian ──────────┤
S0 episodic ─────┼─▶ A2 curiosity ┘        │               │
   stream +      │     + mirror            │               │
   attention +   │                         ▼               │
   briefing ─────┴─▶ S1 mail ─▶ S2 projects ─▶ S3 media ───┴── Level 4: USEFUL
                                   │
                                   ▼
                        A4 anticipation ─▶ S4 plugin contract ── Level 5: ANTICIPATING
                                                  │
                                                  ▼
                              S5 judgment learns ─▶ A5 earned action ── Level 6: TRUSTED
                                                                │
                                          A6 voice (stretch)    ▼
                                                        S6 hardening + paper
```

| # | Phase | Size | Needs | Delivers | Level |
| --- | --- | --- | --- | --- | --- |
| 1 | **A0** Welcome-back moment | S | today's code | the first felt moment; the *moment* seam | 3 |
| 2 | **S0** Episodic stream + attention + briefing core | L | A0 seam | memory of *when*; relevance to *now*; the briefing | 3→4 |
| 3 | **A1** Living presence (tray mind) | M | A0 | a visible, glanceable presence | 3 |
| 4 | **A2** Curiosity + the mirror | M | S0 | useful idle thoughts; "what I learned today" | 3 |
| 5 | **A3** The guardian | M | A0, A1 | speaks only when welcome; attention budget | 3 ✔ |
| 6 | **S1** Correspondence (mail) | L | S0, A3 | "Maya replied…" | 4 |
| 7 | **S2** Projects | M | S0 | "you left the refactor at…" | 4 |
| 8 | **S3** Media & continuity | M | S0 | "episode 7, season 2" | 4 ✔ |
| 9 | **A4** Anticipation | M/L | S0 | next app / task / time | 5 |
| 10 | **S4** Plugin contract | L | S1–S3 | any domain is just a plugin | 5 ✔ |
| 11 | **S5** Judgment learns | M/L | A3, S0 | a ranker trained on your reactions | 6 |
| 12 | **A5** Earned action | L | A3, A4, S5 | offer → one-click → autonomous | 6 ✔ |
| 13 | **A6** Voice | L | A3 | local speech in and out (stretch) | — |
| 14 | **S6** Hardening + the paper | L | all | retention, 30-day evaluation, paper | — |

### 1.2 Milestones

| Milestone | Phases | You can feel it when… |
| --- | --- | --- |
| **M1 · Present** | A0, A1, A3 (+ S0 core) | you come back to your desk and it greets you; it never interrupts your focus |
| **M2 · Useful** | S0, S1, S2, S3 | the morning briefing tells you where you left off in mail, code and shows |
| **M3 · Anticipating** | A2, A4, S4 | what you reach for next is already there |
| **M4 · Trusted** | S5, A5 | it quietly does the Friday five-o'clock thing, and you are glad |
| **M5 · Proven** | S6 | the 30-day evaluation and the paper |

---

## 2. Cross-cutting foundations

Three pieces are used by many phases. Each is introduced by the first phase that
needs it and generalised later — never built speculatively.

### F1 · The moment

Every thing the machine *says* is a **moment**: a typed, grounded, evidence-carrying
proposal to speak.

```python
@dataclass(frozen=True, slots=True)
class Moment:
    kind: str                 # "welcome_back" | "briefing" | "mirror" | "offer" | "nudge"
    text: str                 # rendered, deterministic, grounded
    evidence: tuple[str, ...] # graph node ids / episode ids every claim rests on
    value: float              # V(m) in §3.6 — how much it is worth saying
    context: dict[str, Any]   # focus state, hour bucket, recent dismissals …
    expires_at: datetime      # a held moment is dropped after this
```

- New events: `MOMENT_PROPOSED`, `MOMENT_DELIVERED`, `MOMENT_FEEDBACK`.
- Introduced in **A0** (published straight through to delivery); gated by **A3**
  (the guardian decides deliver / hold / drop); fed to **S5** (feedback trains the
  ranker).
- **Grounding check** on every moment before it leaves: each entity named in
  `text` must resolve to an id in `evidence`, and each id must exist. A moment that
  fails is dropped and logged — never delivered.
- Delivery stays L7 → L9: a moment becomes a `notification` action through the
  existing `SafetyGate` (audited, dry-run aware), then `interface/desktop.py`.

### F2 · Feedback

The judge (A3) and the ranker (S5) learn only from reactions, so reactions must be
captured.

- **Spike (in A3):** does `cosmic-notifications` support notification *actions*
  (`notify-send --action=Keep --action=Dismiss --wait`)? If yes: buttons on the
  popup. If no: the tray menu (A1) and `neuropaca feedback` carry it.
- Three outcomes per delivered moment: **accepted** (acted / kept),
  **dismissed**, **ignored** (expired untouched).
- Stored locally in the episode store (S0), forgettable, never leaves the machine.

### F3 · The evaluation harness

Every hypothesis in `VISION.md §9` needs data and a replayable test.

- `data/eval/` — hand-labelled sets (briefing items, thread states, project
  states), local only, gitignored.
- `scripts/eval_*.py` — offline replays against the episode store, reporting the
  metric each exit gate names.
- Dogfood diary: one line a day from the user during a dogfood window ("useful /
  noise / wrong"), the ground truth the numbers are checked against.

---

## 3. The phases

### A0 · The welcome-back moment — *size S*

**In plain words.** You come back to your laptop; one line greets you — what it
wondered about while you were away, and what you were in the middle of. This is the
cheapest possible proof that the machine can feel alive, and everything it needs
already exists.

**Needs:** today's code (L6 idle thoughts with stored text — V-7; desktop delivery
— V-12). **Delivers:** the first felt moment and the `Moment` seam (F1).

**Spike — questions to answer first**
1. Can `ACTION_PROPOSAL` be published by a module other than L8, and does L7 accept a
   `notification` proposal from it unchanged? (Expected yes — the executor builds
   from `_PROPOSABLE` by `action_type`.)
2. How long are real idle spells on this machine? Pull the distribution from
   `data/neuropaca.log` to set the minimum spell worth greeting.
3. How many idle thoughts does a typical spell produce? (If usually zero, the line
   must stand on the "where you left off" half alone.)

**Design**
- New `interface/moments.py` — `MomentComposer(BaseModule)`:
  - subscribes `IDLE_DETECTED` (open a spell, remember the last focused node from
    `APP_SWITCH`), `INSIGHT_GENERATED` with `source="idle"` (collect the spell's
    thoughts), `ACTIVITY_DETECTED` (close the spell and compose);
  - composes with fixed templates only:
    *"Welcome back. While you were away I wondered: “{question}” You were in
    {app} ({topic})."* — each half dropped when it has nothing grounded to say;
  - picks the spell's thought with the highest relevance; ties → newest;
  - publishes `MOMENT_PROPOSED`, and (until A3 exists) an `ACTION_PROPOSAL`
    `notification` straight away.
- Config: `welcome_min_idle_minutes = 20`, `welcome_daily_cap = 6`,
  `welcome_enabled = true`.
- Registered in `orchestration/modules.py`; no module imports another.

**Math.** None beyond ranking by the existing relevance score (§3.2).

**Load budget.** Pure bookkeeping on events already flowing; one proposal per
returned spell; zero model calls.

**Tests**
- Synthetic spell (idle → thought → activity) produces exactly one proposal; a
  spell shorter than the minimum produces none; the daily cap holds.
- A spell with no thought still yields the "you were in…" line; with neither, silence.
- Grounding: every name in the text resolves to an id in the moment's evidence.
- Dry-run config → audited, no popup (reuses the V-12 guarantee).
- End to end on a copy of the live graph: real `DefaultModeNetwork` + composer + L7
  + L9 (the V-12 harness).

**Exit**
- [ ] Fires on real returns during a one-week dogfood; never more than the cap.
- [ ] Zero ungrounded lines (diary + grounding log).
- [ ] The user keeps it on for the week.

**Risks.** Repetitive text → rotate templates and prefer thoughts not yet surfaced
(L9 already remembers surfaced ids). Thoughts that are dull → A2 fixes the source.

---

### S0 · Episodic stream, attention, and the briefing core — *size L*

**In plain words.** Today the graph knows *what goes with what*, but not *what
happened when*. S0 gives it a memory of time — what you did, when, for how long,
and what was true at that moment — plus the ability to tell what is relevant
**right now**, and the first morning briefing built on both.

**Needs:** A0 (the moment seam). **Delivers:** Level 4's foundation; everything
above Level 3 depends on it.

**Spike**
1. **Store.** `sqlite3` (stdlib — no new dependency) in WAL mode vs append-only
   JSONL. Measure: write throughput under a 20k-switch storm, query latency for
   "what was I doing Tuesday 15:00", growth per day. Expected: SQLite.
2. **Volume.** Real events per day from the gate log (~39 switches/hour observed) →
   projected size per month; set retention (default 90 days).
3. **Bi-temporal semantics.** Which facts can be *superseded* (an app's topic, a
   project's branch) vs which are only *episodes* (a focus span)?
4. **PPR at scale.** Pure-Python power iteration over the adjacency dicts at the
   10k-node fixture: target < 20 ms; if not, cache per scheduler tick.
5. **Briefing trigger.** First activity of a calendar day vs first after ≥ 6 h
   idle vs both (open question 4) — replay the log to see which fires sensibly.

**Design**
- New `core/episodes.py` — `EpisodeStore`:
  - `data/episodes.sqlite`, own `episodes_schema_version`, read and written off the
    event loop (`asyncio.to_thread`), one writer task draining a bounded queue in
    batches;
  - table `episode(id, kind, subject, object, t_start, t_end, t_valid, t_invalid,
    t_seen, source, attrs_json)` with indexes on `(subject, t_start)` and `(kind, t_start)`;
  - `record_span(kind, subject, start, end)`, `assert_fact(...)` — which **closes**
    any contradicting open fact (`t_invalid = now`) instead of deleting it (§3.3),
    `at(t)`, `between(t0, t1)`, `forget(entity)`.
- **Writers** (all through the bus, never imports):
  - focus spans: open on `APP_SWITCH`, close on the next switch / `IDLE_DETECTED`;
  - idle spans; insights and idle thoughts; delivered moments and their feedback (F2);
  - superseding facts: topic membership, later project state (S2).
- **Attention** — `GraphMemory.personalized_pagerank(seeds, d=0.85, tol=1e-6, max_iter=50)`:
  - seeds = current focus node + the last three focus nodes, weighted by recency;
  - sparse power iteration over `_succ` / `_pred` with Hebbian weights as transition
    weights, hubs damped so `YOU` does not soak up the mass;
  - retrieval score $r(v)$ = the §3.4 blend; weights `attention_alpha/beta/gamma` in config.
- **Briefing core** — `interface/briefing.py`:
  - candidates: open threads (last session per app/project without a clean end),
    new insights since the last briefing, plugin items later (S1–S3);
  - value = $r(i)$; similarity = neighbourhood Jaccard (no embeddings needed yet);
  - greedy submodular selection, $k \le 5$ (§3.9);
  - rendered with templates, each item carrying its evidence ids;
  - delivered as a moment (F1) and on demand: `neuropaca briefing`.
- `forget` and `panic` extended to the episode store.

**Math.** Bi-temporal intervals (§3.3); Personalized PageRank (§3.4); greedy
submodular selection with the $(1 - 1/e)$ guarantee (§3.9).

**Load budget.** Writes batched off-loop; PPR ≤ 20 ms at 10k nodes or cached per
tick; briefing composed once per trigger; zero model calls in the core (a model
may phrase, never decide).

**Tests**
- Span reconstruction from synthetic switch streams (overlaps, idle gaps, restarts).
- Supersede: asserting a new topic closes the old interval; `at(t)` returns what
  was true then.
- PPR matches a reference implementation on small graphs; converges; seeds matter.
- Submodular greedy: never picks near-duplicates; respects $k$.
- Performance: 20k-switch storm — no loop lag over 5 ms; PPR timing at 10k nodes.
- `panic` wipes the store; `forget <app>` removes its episodes.
- Egress: unchanged (the store is a local file).

**Exit**
- [ ] 20 hand-written "what was I doing when…" queries answered correctly from the store.
- [ ] Morning briefing on 7 dogfood days: 2–5 items, every one with evidence, zero wrong.
- [ ] Retrieval < 50 ms; DB growth bounded and matching the projection in the soak.

**Risks.** Store and graph drifting apart → the store is the log, the graph is the
learned summary; a nightly consistency check. Briefing that states the obvious →
S5's ranker and A3's feedback.

---

### A1 · Living presence — the tray mind — *size M*

**In plain words.** You can glance at the corner of your screen and see that it is
there and what it is doing — focused, idle, thinking, or "noticed something". One
click shows today's thoughts; another opens the graph at what it is thinking about.

**Needs:** A0. **Delivers:** a presence; the feedback channel if popups can't carry it (F2).

**Spike**
1. The existing `scripts/soak_tray.py` (AyatanaAppIndicator via system `gi`) proves
   the toolkit works on COSMIC; confirm menu refresh and icon swap without leaks
   over 24 h.
2. Poll vs push: L9 is request/response over the socket — is a 5 s poll invisible
   in CPU? (Expected yes.)

**Design**
- New L9 op `presence` → `{state, since, thoughts_today[], last_moment, paused_until}`.
  State machine in L9 from events it already sees:
  `thinking` (a DMN cycle is running) > `noticed` (an undelivered moment or new insight
  in the last 10 min) > `focused` (a focus session is active) > `idle` > `awake`.
- New `scripts/neuropaca_tray.py` — a separate process (like the graph viewer,
  importing nothing from the package): icon per state, menu with today's thoughts,
  "open graph view", "pause for 1 h", "what did you learn today" (A2), feedback on
  the last moment (F2).
- `scripts/systemd/neuropaca-tray.service` — enabling it is the user's call.

**Tests.** State derivation unit tests; the op's contract; menu model tested without
GTK (the soak-tray split); the tray survives the daemon restarting ("asleep" state).

**Exit**
- [ ] State matches the daemon within one poll, verified against the log over a day.
- [ ] Pause silences every moment until it expires.
- [ ] No measurable CPU when idle; no memory growth over 24 h.

---

### A2 · Curiosity and the mirror — *size M*

**In plain words.** Two things. Its idle thoughts become genuinely curious —
about what it *doesn't yet understand* about you, rather than what it already
knows. And at the end of a day it can tell you, in two or three sentences, what
actually changed about you today.

**Needs:** S0 (co-occurrence counts and daily distributions come from the episode store).

**Spike**
1. From the episode store, can each association get honest evidence counts —
   co-uses $s$ and "one without the other" $f$ within the co-activation window —
   without a graph schema change? (Expected yes: derived at idle time.)
2. The KL threshold $\tau$: replay two weeks of days; pick $\tau$ so an ordinary
   day produces no mirror.

**Design — curiosity**
- Each association $(u, v)$ gets a posterior $\text{Beta}(1 + s, 1 + f)$.
- Expected information gain of one more observation (§3.7), closed form via the
  Beta–Bernoulli predictive; computed for the top-$N$ candidate pairs from S0's
  attention, not the whole graph.
- The DMN's seed choice (V-4 sampling) becomes a mix: with probability
  $1 - \epsilon$ the highest-IG pairs, with $\epsilon$ the existing weighted sample
  (keeps exploration and a fallback).
- Questions stay from the closed template set — curiosity changes *what* it asks
  about, never lets the model invent the question.

**Design — the mirror**
- $Q$ = today's app × hour distribution; $P$ = an exponentially weighted baseline
  over the last 14 days (same weekday weighted up); Dirichlet smoothing ($+1$).
- If $D_{\mathrm{KL}}(Q \,\|\, P) > \tau$: take the top 2–3 contributors
  $Q_i \log(Q_i / P_i)$ (and the largest "missing" $P_i$) and render them:
  *"You spent 2 h more in VS Code than a usual Thursday." / "You didn't open
  Obsidian today — unusual for you."*
- Delivered as a `mirror` moment in the evening (first idle after 18:00, config),
  and on demand: `neuropaca mirror`.

**Math.** Beta–Bernoulli information gain (§3.7); KL divergence with smoothing (§3.8).

**Tests.** IG is higher for uncertain than for settled pairs; IG ranking stable
under relabelling; KL is zero for identical days and grows with shift; contributors
sum to the divergence; no mirror below $\tau$.

**Exit**
- [ ] The mirror stays silent on ordinary days (≤ 1 false alarm a week in dogfood).
- [ ] H4 protocol run: users rate IG-chosen thoughts vs score-sampled thoughts.

---

### A3 · The guardian — *size M*

**In plain words.** It learns when you want to hear from it. Nothing reaches you
mid-focus; there is a daily limit; and every time you wave something away, it gets
a little more careful about that kind of moment in that kind of situation.

**Needs:** A0 (moments), A1 (a feedback channel). **Delivers:** Level 3 complete.

**Spike**
1. Notification actions on `cosmic-notifications` (F2).
2. Context buckets: focus state {focused, normal, just-ended} × hour {night,
   morning, afternoon, evening} × recent dismissals {0, 1, 2+} = 36 buckets per
   moment kind — enough to learn, few enough to fill.
3. Priors: from open question 6 — start conservative ($\text{Beta}(1, 3)$).

**Design**
- New `drive/guardian.py` — `Guardian(BaseModule)`:
  - subscribes `MOMENT_PROPOSED`; decides **deliver / hold / drop**;
  - Thompson sampling per (kind, bucket) (§3.6): deliver iff
    $\tilde p \cdot V(m) > C_{\text{interrupt}}(x)$ and the daily budget $B$ allows;
  - `hold` during a focus session: queued (bounded, with `expires_at`) and
    re-evaluated when the session ends;
  - updates posteriors from `MOMENT_FEEDBACK`; posteriors decay daily
    ($\gamma = 0.98$) so it follows you as you change;
  - state persisted in the episode store.
- A0's direct path switches to going through the guardian.
- Config: `nudge_daily_budget`, `interrupt_cost_focus`, `interrupt_cost_normal`,
  `guardian_decay`.

**Math.** Beta–Bernoulli Thompson sampling with a context-dependent cost (§3.6).

**Tests**
- Simulated users with known acceptance rates: the gate converges to delivering the
  welcome kinds and suppressing the unwelcome ones; regret grows sublinearly.
- Nothing delivered while a focus session is active, whatever the sample.
- The budget is never exceeded; held moments expire rather than pile up.

**Exit**
- [ ] Zero moments mid-focus in a two-week dogfood.
- [ ] Dismissal rate falls across the two weeks (H2: alternate weeks against a
      fixed-rule gate).

---

### S1 · Correspondence (mail) — *size L*

**In plain words.** "Maya replied to the thread you started on Tuesday." "You
haven't answered Arjun in five days." It knows the state of your conversations —
without reading them to anyone but you.

**Needs:** S0, A3.

**A structural fact that shapes this phase.** The daemon runs with
`PrivateNetwork=true`: it *cannot* reach a mail server, and any child it spawns
cannot either. That is a feature. The mail fetcher must therefore be a **separate
local process** with its own tightly scoped unit, writing only to a local spool
the daemon reads. The daemon's zero-egress guarantee stays intact and CI-provable.

**Spike**
1. Backend (open question 1): local IMAP (headers only) vs provider API vs an
   existing on-disk maildir (e.g. from a mail client). Criteria: what leaves the box,
   setup effort, reliability.
2. Retention (open question 2): participants + subject + snippet, or full bodies.
3. Threading: `Message-ID` / `In-Reply-To` / `References` reliability on the user's
   real mail (fixture exported locally).
4. The fetcher unit's sandbox: `RestrictAddressFamilies`, no write outside its spool,
   one configured host.

**Design**
- `plugins/mail/` — the fetcher: its own unit (`neuropaca-mail.service`), off by
  default, writes header records to `data/plugins/mail/spool/*.jsonl`.
- Daemon side: a `MailIngest` reader turns spool records into episodes (message
  received / sent) and superseding facts (thread state: *awaiting you*,
  *awaiting them*, *resolved*).
- Briefing candidates: replies received to threads you started; threads awaiting
  you beyond N days; people you usually answer quickly, now overdue.
- `forget <person>` removes their episodes, facts and spool records.

**Tests.** Threading on a fixture maildir; state transitions; the egress job
extended: the daemon still has no route out; the fetcher reaches only its host.

**Exit**
- [ ] On a 50-thread hand-labelled set: "replied" / "awaiting" precision ≥ 0.9.
- [ ] CI proves the daemon's zero egress and the fetcher's single-host egress.

---

### S2 · Projects — *size M*

**In plain words.** "You left NeuroPACA on `graph-view` with two uncommitted files;
the last failing test was `test_bridge_value`; your note says: wire the tray."

**Needs:** S0.

**Spike.** Open question 3: an explicit `.neuropaca/next` line per repo vs inferred
next steps. Measure how often inference would be wrong on the user's own repos.

**Design**
- A read-only project collector over repos under `watch_paths`, on the scheduler:
  current branch, uncommitted file count, last commit time, most recently edited
  files (mtime), last failing tests from `.pytest_cache/v/cache/lastfailed`, and the
  `.neuropaca/next` line if present.
- Project sessions as episodes (focus spans whose working directory is in the repo);
  project state as superseding facts.
- Briefing item "where you left off" per recently active project; the thread
  moment (VISION §1.4) when you return to a repo after days away.

**Tests.** Temp git repos in every state; the collector never writes to a repo.

**Exit.** [ ] Correct "left off" summaries on 10 real repos, checked by the user.

---

### S3 · Media and continuity — *size M*

**In plain words.** "You were on episode 7 of season 2." It remembers where you
stopped in what you watch, read and listen to.

**Needs:** S0.

**Spike**
1. Sources without new dependencies: browser tab titles already captured by B14's
   web-app attribution; MPRIS players over the session bus (`busctl` / `gdbus`,
   AF_UNIX, like `notify-send`).
2. Title patterns per site (allowlisted, like `webapp_map`) — how reliably do they
   carry show, season and episode?

**Design.** A media extractor over titles and MPRIS metadata → episodes (watched /
listened spans) and facts (last position per series); only the derived fields
leave the collector, never raw titles beyond the allowlist.

**Exit.** [ ] Correct "where you stopped" on a fixture of real titles and three
real series over a week.

---

### A4 · Anticipation — *size M/L*

**In plain words.** It learns your rhythm — after a long terminal session you open
your notes; on Friday afternoons you check the same dashboard — and has those things
ready.

**Needs:** S0 (the event log). **Delivers:** Level 5.

**Spike**
1. Kernel timescale $\beta$: validate $\{1/60, 1/300, 1/1800\}\ \text{s}^{-1}$ on held-out days.
2. Fit cost: online EM vs gradient MLE for $K \approx 25$ apps over a week of
   events — target < 2 s nightly in the DMN.
3. Baselines: most-frequent, time-of-day frequency, first-order Markov.

**Design**
- New `learning/anticipation.py`: multivariate Hawkes process (§3.5) with an
  exponential kernel, parameters $\mu_k, \alpha_{jk}$ fit nightly on the episode
  store; log-likelihood computed recursively in $O(N \cdot K)$.
- Also detects **periodic routines** (same app, same weekday and hour, ≥ 3 weeks) —
  the raw material for A5's offers.
- Uses, all passive until A5: ordering the briefing and the tray's "likely next";
  pre-warming retrieval for the predicted context; `nudge` moments ("notes next?")
  through the guardian.
- L9 op `predict` → top-3 next apps with probabilities and expected time.

**Tests.** Recover known $\alpha$ from synthetic Hawkes data; the evaluation
harness comparing against the three baselines on held-out days.

**Exit.** [ ] H3: top-3 accuracy and log-likelihood beat all three baselines on
held-out days. [ ] A wrong prediction is invisible (pre-warming only).

---

### S4 · The plugin contract — *size L*

**In plain words.** Calendar, reading list, bills, habit streaks — each becomes
"just a plugin", added without touching the core.

**Needs:** S1–S3 (three real plugins to generalise from, not one imagined).

**Spike**
1. The contract shape: Model Context Protocol (the ecosystem standard) over stdio
   or a local socket — via its official SDK (a new dependency, needs approval) or a
   minimal MCP-compatible JSON-RPC subset written in-house.
2. Permissions: a manifest per plugin (paths it may read, the one host it may reach
   if any, retention); enforced by its systemd unit, checked by `doctor`.

**Design**
- Contract: `describe()`, `items(since)`, `entities()`, `forget(entity)`; records
  land in the episode store through one ingest path.
- S1–S3 refactored onto it; then two or three new plugins (calendar from local
  `.ics`, a reading list, habit streaks).
- `neuropaca plugins` lists them with their permissions.

**Exit.** [ ] A fourth domain touches only its own plugin directory.
[ ] `doctor` flags any plugin exceeding its manifest.

---

### S5 · Judgment learns — *size M/L*

**In plain words.** It learns what *matters* to you — not just when to speak (A3),
but which items deserve the briefing's top slot.

**Needs:** A3 (feedback), S0 (retrieval features).

**Spike.** Enough feedback? Minimum labelled reactions before a learned ranker beats
the fixed §3.4 weights on replay (expected a few hundred).

**Design**
- Features per candidate: PPR score, recency, importance, kind, age, domain, past
  reactions to the same entity, time of day.
- Model: logistic regression with online updates (pure Python at this size; no
  new dependency), or pairwise ranking if the spike favours it.
- **Safety net:** the learned ranker is used only while it beats the fixed-weight
  ranker on a rolling held-out window; otherwise it falls back automatically.

**Exit.** [ ] NDCG on held-out days improves week over week and never falls below
the fixed-weight baseline in use.

---

### A5 · Earned action — *size L*

**In plain words.** "You do this every Friday at five — want me to?" Yes, a few
times. Then one click. Then, once it has proven itself, it just does it — and
tells you it did.

**Needs:** A3, A4 (routines), S5. **Delivers:** Level 6.

**Design**
- A **trust ledger** per action type: offers accepted in a row, reverts, last
  failure.
- The ladder per action type: **offer** (a moment) → **one-click** (the moment
  carries the action) → **autonomous** (performed, then reported). Promotion needs
  $n$ consecutive acceptances and zero reverts; any revert or complaint demotes one
  rung.
- Only the **safe** tier may ever be autonomous; dangerous actions always require
  the recorded confirmation the L7 gate already enforces.
- First action types: open the notes app, restore a project workspace (open the
  repo in the editor), mark an episode watched. Every one reversible and audited.

**Exit.** [ ] Zero unwanted actions over 30 days. [ ] Every autonomous action was
reversible, audited, and reported to the user.

---

### A6 · Voice (stretch) — *size L*

**In plain words.** Talk to it; it answers aloud — without anything leaving the
laptop.

**Needs:** A3. Only if it stays local, grounded and interruptible.

**Spike.** whisper.cpp / Moonshine for speech in, Kokoro-82M for speech out, as
local binaries (new dependencies — approval). Latency on this CPU; RAM next to the
two language models.

**Design.** **Push-to-talk only** — no always-listening microphone. Spoken commands
map onto existing verbs (briefing, mirror, tell, pause); answers read grounded text
only. Transcripts kept only if the user turns that on.

**Exit.** [ ] Round trip under 2 s. [ ] Nothing recorded or stored by default.

---

### S6 · Hardening and the paper — *size L*

**In plain words.** Make it last, make it forget properly, and prove it works.

- **Retention** per domain; `forget` across graph, episode store and plugins;
  `panic` wipes everything; `doctor` reports what is stored, where, and for how long.
- **7-day soak** of the full system at each milestone.
- **The 30-day evaluation** (`VISION.md §9`): the product test plus H1–H5, including
  H1's comparison against a fine-tuned small model — feasibility of that fine-tune
  on this CPU settled by its own spike.
- **The paper**: the mechanism, the living-machine thesis, the numbers, and what
  was rejected along the way.

**Exit.** [ ] Every hypothesis measured and reported, including the ones that fail.

---

## 4. Risks that cut across every phase

| Risk | Why it matters | Guard |
| --- | --- | --- |
| **Annoyance** | the fastest way to be switched off | A3 before any high-volume moment; budget; silence in focus |
| **A wrong claim** | lively + wrong = creepy | F1 grounding check; extractive templates; release blocker |
| **Privacy through plugins** | the only place egress can creep in | separate units, manifests, CI egress proofs, `doctor` |
| **Laptop slowdown** | "alive" must never mean "sluggish" | §0.3 load budget; perf tests at 10k nodes |
| **Store/graph drift** | two memories that disagree | the store is the log, the graph its summary; nightly check |
| **Model temptation** | using the LLM to decide or invent | models phrase and pick from closed sets; facts only from the graph |
| **Scope creep** | a Jarvis that never ships | milestones M1–M5; each phase has an exit gate |
| **Soak interference** | a week of evidence thrown away | never restart the soak without asking |

---

## 5. What to build first

**A0 — the welcome-back moment.** It is small, it reuses the idle thoughts (V-7) and
desktop delivery (V-12) that already work, it introduces the `Moment` seam every
later phase builds on, and it is the first time the machine will *feel* different
the moment you sit back down. Then S0, because everything above Level 3 needs a
memory of time.
