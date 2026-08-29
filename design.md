# design.md — Visual Identity

**Status:** Derived from `neuropaca-v4.html` (product) and `neuropaca-overview.html` (overview site).

Two related looks: a **dark, terminal-native** identity for the product itself, and a **lighter** identity for the overview/marketing site. They share the accent language.

---

## 1. Principles

1. **The terminal is the product.** Most user contact is `rich` output in a terminal. Anything that can't survive at monospace on a dark background isn't part of the system.
2. **Instrument, not dashboard.** Dense, precise, calm. No big hero numbers, no gradients-as-decoration.
3. **Silence is the default.** Good design here is measured by how rarely the user is interrupted.
4. **Colour carries meaning.** Every hue maps to a semantic. `relevance_score` and pressure are always shown, never a bare assertion.
5. **Dark-first.** The user works in a dark terminal at night.

---

## 2. Typography

| Role | Family | Where |
| --- | --- | --- |
| Display | **Syne** (700, 800) | Page titles, section headers |
| Mono / body | **JetBrains Mono** (300–500) | The primary face — terminal, code, data, all CLI rendering |
| Editorial | **Instrument Serif** (italic) | Hero statements and pull quotes only |
| Overview site sans | Inter · Space Grotesk | The lighter overview/marketing pages only |

Terminal alignment comes from column arithmetic, never glyph widths. Box-drawing characters are safe; ligature-dependent layouts are not.

---

## 3. Colour

### 3.1 Product (dark) — from `neuropaca-v4.html`

| Token | Hex | Use |
| --- | --- | --- |
| `bg` | `#0f1117` | base plane |
| `bg2` | `#161b27` | panels |
| `bg3` | `#1e2535` | nested panels, table headers |
| `bg4` | `#252d3d` | hover, selection |
| `border` | `rgba(255,255,255,0.07)` | default dividers |
| `border2` | `rgba(255,255,255,0.13)` | emphasised edges |
| `text` | `#e8eaf0` | body, headings |
| `muted` | `#7a8099` | labels, metadata, captions |

### 3.2 Semantic accents

| Token | Hex | Means |
| --- | --- | --- |
| `accent` (blue) | `#6c8fff` | primary action, links, system identity |
| `accent2` (violet) | `#a78bfa` | insight, inference, model output |
| `accent3` (green) | `#34d399` | healthy, complete, protected, success |
| `accent4` (orange) | `#fb923c` | load, pressure building, caution |
| `accent5` (pink) | `#f472b6` | drive, anomaly, unusual pattern |
| `green` (terminal) | `#00ff88` | **reserved** — proof the daemon is alive (prompt / heartbeat) only |
| `red` | `#f87171` | error, danger, abstract/interface, prune |

`#00ff88` is used for exactly one thing. Using it anywhere else destroys the signal.

### 3.3 Layer palette (from the class diagram)

| Layer | Hex |
| --- | --- |
| L1 Core | `#34d399` |
| L2 Sensing | `#6c8fff` |
| L3 Diagnosis | `#fb923c` |
| L4 Learning | `#a78bfa` |
| L5 Drive | `#f472b6` |
| L6 Idle Cognition | `#38bdf8` |
| L9 Interface | `#fbbf24` |
| L10 Orchestration | `#e879f9` |
| Abstract / interface | `#f87171` (square dot) |

### 3.4 State scales

**`relevance_score` (0–10)** — shown on graph nodes:

| Range | Label | Colour | Glyph |
| --- | --- | --- | --- |
| ≥ 7 | Protected — kept, replayed often | `accent3` | 🔒 |
| 4–7 | Keep | `accent` | ↔ |
| 0–3 | Prune candidate — graph cleanup | `red` | ✂️ |

**Pressure (0 → threshold):** `muted` below 40 % · `accent4` 40–99 % · `red` bold at/over threshold.

**Signal types:** `FOCUS_SESSION` green · `DISTRACTION` pink · `HIGH_LOAD` orange · `IDLE` muted.

### 3.5 Overview site (light) — from `neuropaca-overview.html`

`bg` `#eef2fb` · `text` `#1a1d2e` · `text2` `#4b5280` · `blue` `#4f6ef7` · `violet` `#8b5cf6` · `green` `#10b981` · `amber` `#f59e0b`. Glass cards, 20 px radius. This palette is for documentation and the public site only — never product UI.

---

## 4. Iconography

ASCII / Unicode only — must render in any terminal.

| Glyph | Means |
| --- | --- |
| `◆` | NeuroPACA speaking (the system's voice marker) |
| `$` | user input / prompt |
| `✓` | success, healthy, complete |
| `⚠` | warning, needs review |
| `✕` | error, failed |
| `🔒` | protected (score ≥ 7) |
| `✂️` | prune candidate (score ≤ 3) |
| `↔` | compressible (score 4–7) |
| `●` / `○` | live / stopped |
| `[DRY]` | an action that did **not** happen |

---

## 5. Voice & microcopy

- **Specific over generic.** "webpack has been at 94 % for 41 minutes" beats "high CPU detected."
- **Cite the evidence.** Every claim names the nodes or metrics behind it.
- **Express uncertainty honestly.** "Probably" is correct when confidence is 0.4.
- **No anthropomorphic filler.** No "Oops!", no apologies for existing, no exclamation marks.
- **Say what will happen before it happens.** Action prompts state the effect, the target, and whether it is reversible.
- **Admit when there's nothing to say.** "No patterns yet — I've been running 3 days." is the correct early answer.

Example system response:

```
◆ webpack --watch has been at 94% CPU for 41 minutes.
  It starts when you save in ~/src/app — 3 sessions this week.
  based on   webpack · ~/src/app · focus_session
  confidence 0.82
```

Provenance is mandatory — a response without it is a bug.

---

## 6. Progress & latency

CPU inference takes seconds. Design for it.

- < 200 ms: no indicator
- 200 ms – 1 s: static `◆ thinking…`
- \> 1 s: spinner **with elapsed seconds** — `◆ thinking… 3.2s`
- Never a fake progress bar for work whose duration is unknown.

---

## 7. Shell prefixes

| Prefix | Meaning |
| --- | --- |
| `$` | ask — natural language |
| `$?` | diagnose — question with project context + live snapshot |
| `$!` | emergency — immediate autonomous action |
| `$$` | safe — backup + verify before acting |

---

*Related: [PRD.md](PRD.md) · [Architecture.md](Architecture.md) · [rules.md](rules.md) · [phases.md](phases.md) · [memory.md](memory.md)*
