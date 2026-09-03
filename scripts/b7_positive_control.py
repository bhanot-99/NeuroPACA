#!/usr/bin/env python3
"""B7 · Exit Criterion 5 — the positive control (companion to the 24 h soak).

The soak (scripts/start_b7_soak.sh) proves NeuroPACA stays *quiet* under a normal
day. It cannot prove the pipeline is *alive*: a broken L3->L4->L5->L7 chain and a
genuinely calm day both produce an empty data/actions.jsonl. This script closes
that gap. It drives a second, throwaway daemon (NEUROPACA_CONFIG=
neuropaca.control.toml) with a bounded, repeatable synthetic load and shows that
a real behavioural spike lands a traceable proposal in the audit log.

    uv run python scripts/b7_positive_control.py --minutes 300
    uv run python scripts/b7_positive_control.py --print-plan --minutes 300

WHAT IT KEYS ON (traced from source, 2026-09-02)
------------------------------------------------
L5 `PressureAccumulator` only accepts two events: `SIGNAL_CORRELATED` (L3) and
`INSIGHT_GENERATED` from source "learning" (L4). Pressure lands on a signal's
`related_node_ids`; a signal with none contributes nothing. Of the four L3
patterns:

  * `IdlePattern`, `DistractionPattern` -> emit signals with NO node specs
    -> zero pressure. Dead ends for driving an action.
  * `FocusSessionPattern` -> needs the Wayland activity collector, which is
    DISABLED in the systemd soak unit ("no $WAYLAND_DISPLAY"). Also needs a
    20 min uninterrupted focus on a domain:engineering / domain:research app
    with mean CPU >= 10 %. Not reachable unattended.
  * `HighLoadPattern` -> `system.cpu_percent > 90 %` for >= 5 consecutive
    samples (poll 60 s => ~5-6 min) AND files changed under a watched path
    during that window. Cites those files as `file:` nodes. Signal confidence
    approaches 1.0 near 100 % CPU, so one episode puts a file node at ~1.0 ->
    crosses `pressure_low_threshold` (1.0) -> L7 proposes a safe-tier
    `MemoryWriteAction` -> two lines in the audit log. This is the only path a
    headless run can exercise, so it is the one used here.

HIGH TIER is only reached when L3 *and* L4 both contribute high-confidence
evidence (>= 0.75) to the same node inside ~2 min, and the stacked pressure
clears 3.0. `--attempt-high-tier` (default on) ends the run with a few
short-gap bursts on the same churn files to try to make that happen — it needs
BitNet to return a non-abstaining insight, so it is best-effort, not guaranteed.

ISOLATION
---------
Everything the control writes lives under ~/.cache/neuropaca-b7-control/ or
$XDG_RUNTIME_DIR — never inside the repo. The real soak watches
/home/bhanot/NeuroPaca recursively, so churning there would make the *real*
soak log synthetic proposals into the acceptance log. The only thing the two
daemons share is the host CPU: a synthetic storm with no repo file changes
makes the real soak's HighLoadPattern emit a fileless signal, which carries
zero pressure, so data/actions.jsonl stays clean. Still, prefer to schedule
this off-hours (scripts/schedule_b7_positive_control.sh).

TEARDOWN
--------
Bounded by --minutes. On the deadline, SIGTERM, SIGINT, or systemd
`RuntimeMaxSec`, it kills the load, stops the control daemon (SIGTERM -> clean
graph save), prints a summary, runs validate_b7_dryrun.py against the control
log, and writes run_<ts>.summary.json. Safe to Ctrl-C at any point.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = "neuropaca.control.toml"
CACHE = Path.home() / ".cache" / "neuropaca-b7-control"
CHURN = CACHE / "churn"
DAEMON = REPO / ".venv" / "bin" / "neuropacad"
CONTROL_LOG = CACHE / "actions.jsonl"
DAEMON_LOG = CACHE / "control_daemon.log"
SOCKET = Path("/run/user") / str(os.getuid()) / "neuropaca-b7-control.sock"

# A small, fixed set of churn targets so HighLoadPattern._file_specs (cap 5)
# resolves to the *same* file: nodes every episode — that stability is what lets
# the high-tier finale stack pressure on one node, and what makes the audit
# trail readable.
CHURN_FILES = [CHURN / f"edit_{i:02d}.txt" for i in range(4)]
CHURN_WRITE_INTERVAL_S = 15.0

# Defaults chosen against the shipped constants (do not tune to force a pass):
#   burst   >= ceil(300 / 60) = 5 system samples over the HIGH_LOAD threshold,
#           + margin for the 1 s cpu_percent read and scheduler jitter.
#   cooldown long enough that a 1.0 spike decays below _EVICT_BELOW (0.001):
#           0.5 ** (720 / 60) = 2.4e-4 -> the node is evicted, so every episode
#           is an independent, cleanly-attributable crossing.
DEFAULT_BURST_S = 480
DEFAULT_COOLDOWN_S = 720
FINALE_BURST_S = 360
FINALE_GAP_S = 40
FINALE_BURSTS = 4

_stop = threading.Event()


# --------------------------------------------------------------------- load gen
def _cpu_worker() -> None:
    """One core, pinned. `nice 19` so foreground work still preempts instantly —
    utilisation (what psutil reports) stays ~100 %, responsiveness does not
    suffer. Exits when the parent terminates it."""
    try:
        os.nice(19)
    except OSError:
        pass
    x = 1.0001
    while True:
        for _ in range(500_000):
            x = (x * 1.0000001) % 987654.321
            x += 1.0000001


class Load:
    """A CPU storm + a file-churn thread, started and stopped as a unit."""

    def __init__(self, workers: int) -> None:
        self._workers = workers
        self._procs: list[mp.Process] = []
        self._churn: threading.Thread | None = None
        self._churn_stop = threading.Event()

    def start(self) -> None:
        self._procs = [mp.Process(target=_cpu_worker, daemon=True) for _ in range(self._workers)]
        for p in self._procs:
            p.start()
        self._churn_stop.clear()
        self._churn = threading.Thread(target=self._churn_loop, daemon=True)
        self._churn.start()

    def stop(self) -> None:
        self._churn_stop.set()
        if self._churn is not None:
            self._churn.join(timeout=5)
            self._churn = None
        for p in self._procs:
            p.terminate()
        for p in self._procs:
            p.join(timeout=5)
        self._procs = []

    def _churn_loop(self) -> None:
        n = 0
        while not self._churn_stop.is_set():
            stamp = datetime.now(UTC).isoformat()
            for f in CHURN_FILES:
                try:
                    f.write_text(f"synthetic b7 positive-control edit {n} @ {stamp}\n")
                except OSError:
                    pass
            n += 1
            self._churn_stop.wait(CHURN_WRITE_INTERVAL_S)


# ------------------------------------------------------------------- daemon mgmt
def _control_daemon_running() -> bool:
    r = subprocess.run(
        ["pgrep", "-f", f"NEUROPACA_CONFIG={CONFIG}"], capture_output=True, text=True
    )
    if r.returncode == 0:
        return True
    # pgrep matches the command line, not the env; also check the socket.
    return SOCKET.exists() and _socket_alive()


def _socket_alive() -> bool:
    import socket

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(str(SOCKET))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _start_daemon() -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env["NEUROPACA_CONFIG"] = CONFIG
    log = DAEMON_LOG.open("ab")
    log.write(f"\n=== control daemon start {datetime.now(UTC).isoformat()} ===\n".encode())
    log.flush()
    proc = subprocess.Popen([str(DAEMON)], cwd=str(REPO), env=env, stdout=log, stderr=log)
    for _ in range(60):
        if proc.poll() is not None:
            raise RuntimeError(
                f"control daemon exited early (rc={proc.returncode}); see {DAEMON_LOG}"
            )
        if SOCKET.exists():
            time.sleep(2)  # let modules finish start()
            return proc
        time.sleep(1)
    raise RuntimeError(f"control daemon did not open {SOCKET} within 60 s; see {DAEMON_LOG}")


def _stop_daemon(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ------------------------------------------------------------------- experiment
def _plan(minutes: int, burst_s: int, cooldown_s: int, high: bool) -> dict:
    cycle = burst_s + cooldown_s
    budget = minutes * 60
    finale = (FINALE_BURSTS * (FINALE_BURST_S + FINALE_GAP_S)) if high else 0
    episodes = max(1, (budget - finale) // cycle)
    return {
        "minutes": minutes,
        "burst_seconds": burst_s,
        "cooldown_seconds": cooldown_s,
        "episodes": int(episodes),
        "attempt_high_tier": high,
        "finale_seconds": finale,
        "estimated_seconds": int(episodes) * cycle + finale,
    }


def _run_episode(idx: int, total: int, load: Load, burst_s: int, cooldown_s: int) -> None:
    tag = f"episode {idx}/{total}"
    print(f"[{_now()}] {tag}: burst {burst_s}s ({load._workers} workers)", flush=True)
    load.start()
    _interruptible_sleep(burst_s)
    load.stop()
    if _stop.is_set():
        return
    print(f"[{_now()}] {tag}: cooldown {cooldown_s}s (pressure decays out)", flush=True)
    _interruptible_sleep(cooldown_s)


def _run_finale(load: Load) -> None:
    print(
        f"[{_now()}] high-tier finale: {FINALE_BURSTS} bursts, {FINALE_GAP_S}s gaps, same files",
        flush=True,
    )
    for i in range(1, FINALE_BURSTS + 1):
        if _stop.is_set():
            return
        print(f"[{_now()}]   finale burst {i}/{FINALE_BURSTS}", flush=True)
        load.start()
        _interruptible_sleep(FINALE_BURST_S)
        load.stop()
        _interruptible_sleep(FINALE_GAP_S)


def _interruptible_sleep(seconds: float) -> None:
    _stop.wait(seconds)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------- summary
def _summarize(started_at: datetime) -> dict:
    lines: list[dict] = []
    if CONTROL_LOG.exists():
        for raw in CONTROL_LOG.read_text().splitlines():
            raw = raw.strip()
            if raw:
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    attempts = [ln for ln in lines if ln.get("phase") == "attempt"]
    results = [ln for ln in lines if ln.get("phase") == "result"]
    by_trigger: dict[str, list[str]] = {}
    for ln in attempts:
        by_trigger.setdefault(str(ln.get("trigger", "?")), []).append(str(ln.get("reason", "")))
    low = {k: v for k, v in by_trigger.items() if k.startswith("pressure:low:")}
    high = {k: v for k, v in by_trigger.items() if k.startswith("pressure:high:")}
    executed = [ln for ln in results if ln.get("ok") and "dry-run" not in str(ln.get("detail", ""))]

    return {
        "kind": "b7-positive-control",
        "synthetic": True,
        "not_a_soak_result": True,
        "started_at": started_at.isoformat(),
        "ended_at": datetime.now(UTC).isoformat(),
        "control_log": str(CONTROL_LOG),
        "audit_lines": len(lines),
        "attempts": len(attempts),
        "results": len(results),
        "paired": len(attempts) == len(results),
        "low_tier_proposals": sum(len(v) for v in low.values()),
        "high_tier_proposals": sum(len(v) for v in high.values()),
        "executed_effects": len(executed),
        "low_tier_reasons": sorted({r for v in low.values() for r in v}),
        "high_tier_reasons": sorted({r for v in high.values() for r in v}),
    }


def _print_summary(s: dict) -> None:
    print("\n" + "=" * 72)
    print("B7 POSITIVE CONTROL — RESULT  (synthetic; NOT a soak result)")
    print("=" * 72)
    print(f"  control log        : {s['control_log']}")
    print(
        f"  audit lines        : {s['audit_lines']}  "
        f"({s['attempts']} attempt / {s['results']} result, paired={s['paired']})"
    )
    print(f"  low-tier proposals : {s['low_tier_proposals']}")
    print(f"  high-tier proposals: {s['high_tier_proposals']}")
    print(f"  executed effects   : {s['executed_effects']}   (MUST be 0 — dry-run)")
    if s["low_tier_reasons"]:
        print("\n  low-tier reasons (each should trace to a synthetic HIGH_LOAD spike):")
        for r in s["low_tier_reasons"]:
            print(f"    - {r}")
    if s["high_tier_reasons"]:
        print("\n  high-tier reasons (corroborated L3+L4):")
        for r in s["high_tier_reasons"]:
            print(f"    - {r}")
    alive = s["low_tier_proposals"] > 0 and s["executed_effects"] == 0
    print(f"\n  verdict: {'PIPELINE ALIVE' if alive else 'INCONCLUSIVE'}")
    print("=" * 72)


def _run_validator() -> None:
    py = REPO / ".venv" / "bin" / "python"
    py = str(py) if py.exists() else sys.executable
    print("\n--- validate_b7_dryrun.py against the control log ---", flush=True)
    subprocess.run(
        [py, "scripts/validate_b7_dryrun.py", "--log", str(CONTROL_LOG), "--require-hours", "0"],
        cwd=str(REPO),
    )


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--minutes", type=int, default=300, help="hard wall-clock bound (default 300 = 5 h)"
    )
    ap.add_argument("--burst-seconds", type=int, default=DEFAULT_BURST_S)
    ap.add_argument("--cooldown-seconds", type=int, default=DEFAULT_COOLDOWN_S)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--attempt-high-tier", dest="high", action="store_true", default=True)
    ap.add_argument("--no-attempt-high-tier", dest="high", action="store_false")
    ap.add_argument("--print-plan", action="store_true", help="print the episode plan and exit")
    args = ap.parse_args()

    plan = _plan(args.minutes, args.burst_seconds, args.cooldown_seconds, args.high)
    print("plan:", json.dumps(plan, indent=2))
    if args.print_plan:
        return 0

    if not DAEMON.exists():
        print(f"HALT — {DAEMON} not found; run 'uv pip install -e .'", file=sys.stderr)
        return 1
    if _control_daemon_running():
        print("HALT — a control daemon is already running. stop it first:", file=sys.stderr)
        print("  ./scripts/stop_b7_positive_control.sh", file=sys.stderr)
        return 1

    CACHE.mkdir(parents=True, exist_ok=True)
    CHURN.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    if CONTROL_LOG.exists() and CONTROL_LOG.stat().st_size > 0:
        archive = CACHE / f"actions.{started.strftime('%Y%m%dT%H%M%S')}.jsonl"
        CONTROL_LOG.rename(archive)
        print(f"archived previous control log -> {archive}")
    meta = CACHE / f"run_{started.strftime('%Y%m%dT%H%M%S')}.meta.json"
    meta.write_text(
        json.dumps(
            {
                "synthetic": True,
                "purpose": "B7 positive control",
                "not_for_paper_soak_results": True,
                "plan": plan,
                "started_at": started.isoformat(),
            },
            indent=2,
        )
    )

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())
    # systemd RuntimeMaxSec sends SIGTERM; this alarm is an in-process backstop.
    signal.signal(signal.SIGALRM, lambda *_: _stop.set())
    signal.alarm(args.minutes * 60 + 120)

    load = Load(args.workers)
    proc = None
    try:
        print(f"[{_now()}] starting control daemon ({CONFIG})…", flush=True)
        proc = _start_daemon()
        print(f"[{_now()}] daemon up (pid {proc.pid}); socket {SOCKET}", flush=True)

        deadline = time.monotonic() + args.minutes * 60
        n = plan["episodes"]
        for i in range(1, n + 1):
            if _stop.is_set() or time.monotonic() >= deadline:
                break
            _run_episode(i, n, load, args.burst_seconds, args.cooldown_seconds)
        if args.high and not _stop.is_set() and time.monotonic() < deadline:
            _run_finale(load)
    finally:
        signal.alarm(0)
        load.stop()
        if proc is not None:
            print(f"[{_now()}] stopping control daemon…", flush=True)
            _stop_daemon(proc)

    summary = _summarize(started)
    out = CACHE / f"run_{started.strftime('%Y%m%dT%H%M%S')}.summary.json"
    out.write_text(json.dumps(summary, indent=2))
    _print_summary(summary)
    _run_validator()
    print(f"\nsummary written -> {out}")
    return 0


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    sys.exit(main())
