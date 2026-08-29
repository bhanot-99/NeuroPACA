# B0 spike — BitNet b1.58 2B4T de-risking

**Status:** harness ready · not yet run on the target machine
**Why this exists:** `phases.md` B0, `problems.md` 1.1 + 1.13, `rules.md §4.1`.
This is the highest-risk item in the whole roadmap. Everything in L4 / L6 / L9
depends on both answers below. Throwaway code — never imported by
`src/neuropaca/` (a pre-commit hook enforces that).

## The two questions

1. **Does it fit the machine?** RSS after load, tokens/sec, RSS drift, and CPU
   temperature over a 30-minute run. Budgets: `< 1.4 GB` resident after load,
   `< 150 MB` drift, `>= 5 tok/s` median. (`PRD.md §9`, `design.md §6`.)
2. **Is it coherent over graph context, with a grammar?** A controlled ablation
   over context size K in {1, 3, 5, 8}: ~20 synthetic `(signal + K nodes)`
   fixtures per K, each through a per-call GBNF grammar with `cited_nodes`
   locked to that prompt's aliases, greedy. Metrics per K: valid-parse rate,
   citation-accuracy(K), correct-abstain rate on deliberately weak inputs.

## Setup (target machine)

```bash
# 1. Toolchain
uv venv --python 3.12
uv pip install -e ".[spike]"          # llama-cpp-python + psutil

# 2. Model — BitNet b1.58 2B4T GGUF (the only usable BitNet b1.58 checkpoint).
#    Download the official GGUF into ./models/ (gitignored). Q4_0 is the
#    reference quant; also try the i2_s / tl1 BitNet-native quants if the
#    llama.cpp build exposes them.
mkdir -p models
# huggingface-cli download microsoft/BitNet-b1.58-2B-4T-gguf \
#   --include "*.gguf" --local-dir models
```

If `llama-cpp-python`'s bundled build lacks BitNet kernels, build llama.cpp
from source with BitNet support into `spikes/b0_bitnet/llama.cpp/` (gitignored)
and point `LlamaRunner` at that instead — note which path you took in the
results file's surrounding notes.

## Run

```bash
# from the repo root
python spikes/b0_bitnet/benchmark_runtime.py --model models/<file>.gguf --minutes 30
python spikes/b0_bitnet/gen_fixtures.py                         # -> fixtures/ablation.jsonl
python spikes/b0_bitnet/coherence_ablation.py --model models/<file>.gguf
```

Results land in `results/` (gitignored — commit the canonical run with
`git add -f` once, it backs a paper claim).

## Decision rules (what B0 concludes)

| Outcome | Action |
| --- | --- |
| RSS / tok-s / thermal all within budget | proceed — record the numbers in `memory.md` and `PRD.md §9` |
| Doesn't fit RAM or too slow | L4 / L6 / L9 each need a fallback before B4; revisit the model choice |
| citation-accuracy reaches >= ~0.80 at some K | set `_build_context()` production **K = the knee** (Config value, not a guess) |
| citation-accuracy < ~0.80 at every K | 3B Q4 model for `$?` only (`BitNetRuntime` is backend-pluggable); background loop stays on 2B4T |
| valid-parse rate < 1.0 | the grammar or the llama.cpp GBNF path is wrong — fix before trusting any other metric |

## Files

| File | Role |
| --- | --- |
| `_common.py` | model loader, prompt + GBNF assembly, the post-generation validation gate, scoring |
| `grammars/insight.gbnf.template` | static schema skeleton; `__ALIASES__` spliced per call |
| `gen_fixtures.py` | deterministic synthetic fixture generator (seeded) |
| `benchmark_runtime.py` | question 1 — RSS / tok-s / thermal |
| `coherence_ablation.py` | question 2 — citation-accuracy(K) curve + knee + fallback recommendation |
| `results/` | run outputs (gitignored) |
