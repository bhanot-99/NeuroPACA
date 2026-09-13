# S2 · Project Collector Spike (Open Question 3)

Canonical spike artifact for **S2 (Projects)**, resolving `VISION_PHASES.md` Open Question 3:
> "An explicit `.neuropaca/next` line per repo vs inferred next steps. Measure how often inference would be wrong on the user's own repos."

## Decision: Explicit `.neuropaca/next` marker only (No heuristic inference)

### Why
1. **Load Budget & Architecture (§0.3, rules.md):** "Zero model calls in the core". An LLM-based guesser for "next step" is forbidden in the core daemon loop, so an LLM path was never a candidate for the core; it was only ever considered as an out-of-loop possibility and rejected on principle, not benchmarked — see "What was rejected" below.
2. **Failure Mode of Heuristics:** A rule-based guesser (e.g. searching for the last-edited `TODO` comment, or picking the last failing test file as "next work") is brittle and confidently wrong. A wrong claim is worse than silence: "lively + wrong = creepy" (`VISION_PHASES.md §4`).
3. **Deterministic & Gradeable:** An explicit opt-in file (`.neuropaca/next`) costs a single `Path.read_text()` check on the local filesystem.
4. **Clean Omission:** When `.neuropaca/next` is absent, the "your note says: ..." clause is simply omitted from the briefing item. It is never fabricated or guessed.

## Empirical findings on real repos

`run_spike.py` was run read-only against 5 real local repositories spanning several
languages and project shapes (a Python daemon, a Python trading/validation
codebase, a TypeScript/Next.js web app, a C project, and a TypeScript desktop
app) to check the collector's logic holds up on genuinely different repo
shapes, not just the fixture states in the unit tests.

**Real repo paths, names, branch names and file/test names are not published
here** — this dossier lands in a public repository, and those identify other,
private projects on the operator's machine. Only the anonymized aggregate
(shape and counts, no identity) is recorded below; the run itself is
reproducible by anyone against their own repos via:

```
python spikes/s2_project_collector/run_spike.py <repo-path> [<repo-path> ...]
```

| Metric | Result |
| --- | --- |
| Repos checked | 5, across 4 different primary languages |
| `.neuropaca/next` present | 0 / 5 (0%) |
| Dirty file count range | 0 – 149 |
| Repos with failing-test cache present | 2 / 5 |
| Read-only verified (`git status`/`HEAD` byte-identical before/after) | 5 / 5 |

### Verification Highlights
- **Zero repo mutation:** `git status --porcelain` and `git rev-parse HEAD` were confirmed identical before and after collection on every repository.
- **`.neuropaca/next` presence:** Present on 0 of 5 real repos. Clean clause omission is verified as the mandatory behaviour — this is the actual, tested finding the decision above rests on.
- **Failing test extraction:** `.pytest_cache/v/cache/lastfailed` correctly surfaced failing test names on Python projects, while safely returning an empty list for non-pytest repos.

### What was rejected, honestly

The alternative — inferring a "next step" from a heuristic or a small local
model reading the dirty diff / recent commit log — was **not benchmarked**;
it was rejected on the architectural principle above (zero model calls in the
core, and "confidently wrong" being worse than silent) before it was ever
built. If a future phase wants to revisit this, that would need its own
spike with a real inference run and a labeled set of "was this next-step
guess actually right" judgments — not asserted from this one.

## Verdict: GO

Proceed with the build according to `VISION_PHASES.md`:
- `ProjectIngest` implements local-only async git inspection and `.pytest_cache` / `.neuropaca/next` parsing.
- If `.neuropaca/next` is present, read first line up to `project_next_max_chars` (default 200).
- If absent, omit the clause entirely.
