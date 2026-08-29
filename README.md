# NeuroPACA

### Neuromorphic Personal Autonomous Computing Agent

> A local AI that learns your machine, builds a behavioural graph of how you work, and gives grounded answers from a small local model — with zero cloud dependency. *(A later phase grows a sparse model around your actual work — see `pruning.md`.)*

**Status:** B0 — Foundation. Repo skeleton, tooling, and the BitNet de-risking spike harness are in place; the spike has not yet been run on the target machine.
**Version:** v4
**Author:** Jatin Bhanot · Chitkara University · 2026

---

## What is NeuroPACA?

NeuroPACA passively watches OS-level signals (CPU, RAM, disk, temperature, processes, system logs) every 60 seconds, turns them into named behavioural patterns, and stores them in a personal knowledge graph. Every node carries one `relevance_score` that governs **what the graph keeps, what it replays during idle time, and how it ranks context for your questions**. A later, deferred phase uses that same score to prune a local model toward your work (`pruning.md`).

Nothing leaves the machine. It does not record your screen or keystrokes — only cold system numbers.

## Building

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"     # ruff + mypy + pytest + pre-commit
pre-commit install

uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q
```

Source lives in `src/neuropaca/`, one package per architectural layer. The
current phase and next action are always in [`memory.md`](memory.md). The B0
BitNet de-risking spike (run on the target machine) is
[`spikes/b0_bitnet/`](spikes/b0_bitnet/README.md) — it is throwaway code and is
never imported by the daemon.

## Source of truth

The concept documents this build is derived from:

| File | What it is |
| --- | --- |
| `1000071408.png` | The v4 class diagram — the authoritative blueprint |
| `neuropaca-v4.html` | The full concept: architecture, memory design, workflow, sparse-model reasoning |
| `neuropaca-overview.html` | Condensed overview + the BitNet/llama.cpp tech-fix note |

The Markdown documents below are derived from those three:

| Document | Purpose |
| --- | --- |
| [`PRD.md`](PRD.md) | Product scope, the "one score, several jobs" thesis, features, users, non-goals, privacy |
| [`Architecture.md`](Architecture.md) | The 10 layers, class shapes, the four critical invariants, event catalogue |
| [`rules.md`](rules.md) | Binding engineering rules and AI-agent boundaries |
| [`phases.md`](phases.md) | Build order — the Init → Sensing → Diagnosis → Learning → Action → Comms lifecycle |
| [`design.md`](design.md) | The terminal-first visual identity |
| [`memory.md`](memory.md) | Living project state tracker |
| [`problems.md`](problems.md) | Problems & risks register, plus the testing log |
| [`pruning.md`](pruning.md) | The deferred personal-model-pruning design (end-of-roadmap) |

---

*Local by construction. Zero cloud calls.*
