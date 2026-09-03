# B7 · Exit Criterion 5 — positive-control evidence

This directory is the canonical, paper-backing artifact for B7 criterion 5
("a review period in dry-run with zero false positives before any tier goes
live"). It is committed deliberately (the `spikes/` convention in
`.gitignore`).

## Why the positive control, not the 24 h soak

The criterion was **agreed** as a 24 h dogfooding soak under
`neuropaca.soak.toml`. Three soak attempts (2026-09-01 → 2026-09-03) each
produced **zero action proposals — `data/actions.jsonl` was never written**.
Root cause, traced to source:

* L5 `PressureAccumulator` only consumes `SIGNAL_CORRELATED` (L3) and
  `INSIGHT_GENERATED` from `learning` (L4). Pressure lands on a signal's
  `related_node_ids`; a signal with none contributes nothing.
* `IdlePattern` / `DistractionPattern` emit signals with **no node specs** ->
  zero pressure, structurally cannot drive an action.
* `FocusSessionPattern` needs the Wayland activity collector, which
  **self-disables under `systemd --user`** (`no $WAYLAND_DISPLAY` — the unit
  does not inherit the graphical session env).
* That leaves **`HighLoadPattern`** (`system.cpu_percent > 90 %` for >= 5
  samples **and** files changed under a `watch_paths` root) as the *only*
  pressure path a headless soak can reach — which a normal desktop day does
  not trigger.

So an empty soak log cannot distinguish a correct-and-quiet pipeline from a
broken one. The **positive control** closes exactly that gap: a bounded,
repeatable synthetic load driven through a second daemon whose thresholds are
byte-for-byte identical to `neuropaca.soak.toml`, proving L3->L5->L7 fires a
traceable proposal into the audit log and that the high-tier corroboration
gate stays shut.

## The run (`summary.json`, `meta.json`, `actions.jsonl`)

| | |
| --- | --- |
| Harness | `scripts/b7_positive_control.py --minutes 300` |
| Window | 2026-09-02 20:02 -> 2026-09-03 00:49 UTC (~4.5 h) |
| Audit lines | 120 — **60 attempt / 60 result, fully paired** |
| Low-tier (safe) proposals | 60 x `memory_write`, every one `dry_run=true` |
| High-tier proposals | **0** (corroboration gate never opened) |
| Executed effects | **0** |
| Trigger, every proposal | `L3 high_load: cpu 100% (> 90%) for 5 samples (~5 min)` |
| `validate_b7_dryrun.py` | PASS (mechanical) |

Each of the 60 proposals traces to a synthetic `HighLoadPattern` spike citing
the four churn files under `~/.cache/neuropaca-b7-control/churn/`. Zero
high-tier proposals => nothing needed a human false-positive verdict => the
zero-false-positive rule holds mechanically.

`actions.jsonl` here is a verbatim copy of the control daemon's
`~/.cache/neuropaca-b7-control/actions.jsonl` for the run in `summary.json`.
It is **synthetic** (`synthetic: true`, `not_a_soak_result: true`) and is not
a soak result.

## Reproduce

```
uv run python scripts/b7_positive_control.py --print-plan --minutes 300   # inspect
uv run python scripts/b7_positive_control.py --minutes 300                # run (~4.8 h)
```

Do **not** run it while the real soak (`neuropaca-b7-soak.service`) is live —
the real soak watches the repo recursively and the control's CPU storm plus
any repo file churn will drive the real soak's `HighLoadPattern`, putting
synthetic proposals into `data/actions.jsonl`.
