# S2 · Project Collector Spike (Open Question 3)

Canonical spike artifact for **S2 (Projects)**, resolving `VISION_PHASES.md` Open Question 3:
> "An explicit `.neuropaca/next` line per repo vs inferred next steps. Measure how often inference would be wrong on the user's own repos."

## Decision: Explicit `.neuropaca/next` marker only (No heuristic inference)

### Why
1. **Load Budget & Architecture (§0.3, rules.md):** "Zero model calls in the core". An LLM-based guesser for "next step" is forbidden in the core daemon loop.
2. **Failure Mode of Heuristics:** A rule-based guesser (e.g. searching for the last-edited `TODO` comment, or picking the last failing test file as "next work") is brittle and confidently wrong. A wrong claim is worse than silence: "lively + wrong = creepy" (`VISION_PHASES.md §4`).
3. **Deterministic & Gradeable:** An explicit opt-in file (`.neuropaca/next`) costs a single `Path.read_text()` check on the local filesystem.
4. **Clean Omission:** When `.neuropaca/next` is absent (as was the case on 100% of tested real repos), the "your note says: ..." clause is simply omitted from the briefing item. It is never fabricated or guessed.

## Empirical Findings on Real Repos

The collector logic was executed read-only against 5 real repositories on this machine (`/home/bhanot/...`).

| Repo | Branch | Dirty Files | Last Commit | Last Failing Tests | `.neuropaca/next` | Read-only Verified |
| --- | --- | --- | --- | --- | --- | --- |
| `NeuroPaca` | `s2-projects` | 1 | 2026-09-12 | 8 failing | None | Yes |
| `MyBotTrader` | `phase-2-risk-and-validation-gate` | 0 | 2026-08-18 | 2 failing | None | Yes |
| `SYSKON` | `redesign` | 149 | 2026-07-31 | 0 | None | Yes |
| `keyd` | `master` | 0 | 2026-06-01 | 0 | None | Yes |
| `hermes-agent` | `main` | 0 | 2026-07-24 | 0 | None | Yes |

Full JSON results are committed in `summary.json`.

### Verification Highlights
- **Zero repo mutation:** `git status --porcelain` and `git rev-parse HEAD` were confirmed identical before and after collection on every repository.
- **`.neuropaca/next` presence:** Present on 0 of 5 real repos. Clean clause omission is verified as the mandatory behaviour.
- **Failing test extraction:** `.pytest_cache/v/cache/lastfailed` correctly surfaced failing test names on Python projects (`NeuroPaca`, `MyBotTrader`) while safely returning an empty list for non-pytest repos.

## Verdict: GO

Proceed with the build according to `VISION_PHASES.md`:
- `ProjectIngest` implements local-only async git inspection and `.pytest_cache` / `.neuropaca/next` parsing.
- If `.neuropaca/next` is present, read first line up to `project_next_max_chars` (default 200).
- If absent, omit the clause entirely.
