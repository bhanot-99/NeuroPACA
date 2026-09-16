"""
7-day soak monitor for voice-standalone. Samples the health of
voice-daemon.service and voice-tray.service at a fixed interval, writes
one JSON line per sample, and produces a summary report when the 7-day
target is actually reached.

PAUSE/RESUME ACROSS REBOOTS, not wall-clock-since-first-start: a real
"7-day soak" has to mean 7 days of the system actually being observed,
not 7 calendar days including however long the machine happened to be
off — a reboot mid-soak would otherwise either reset the count to zero
(the original version of this script did exactly that — a real bug,
found via direct user feedback, not a design someone asked for) or count
downtime as if it were uptime. `soak_state.json` persists
`cumulative_elapsed_seconds` (real observed time only) across runs;
`soak_log.jsonl`'s samples keep accumulating across restarts the same
way, since it's opened in append mode and never truncated. On SIGTERM
(systemd sends this on logout/shutdown, same as before) the current
session's elapsed time gets folded into that cumulative total and
persisted before exiting, so the next launch — the next login, whenever
that is — picks up the count from exactly where this one left off, not
from zero.

Deliberately NOT the main NeuroPaca project's soak infrastructure
(scripts/soak_probe.py etc.) — that reads a daemon-authored health_check()
JSON dump, which voice-standalone's daemon doesn't have. This reads what
actually exists here instead: `systemctl --user show` for each service's
real state (ActiveState, restart count, memory) and the audit log
(skills/_audit.py) for real activity.

Usage (installed as voice-soak.service, see systemd/voice-soak.service):
    python3 soak_monitor.py [--duration-days 7] [--interval-seconds 300]
"""

import argparse
import json
import os
import signal
import subprocess
import time

_LOG_PATH = os.path.expanduser("~/.local/share/voice-standalone/soak_log.jsonl")
_SUMMARY_PATH = os.path.expanduser("~/.local/share/voice-standalone/soak_summary.json")
_STATE_PATH = os.path.expanduser("~/.local/share/voice-standalone/soak_state.json")
_AUDIT_PATH = os.path.expanduser("~/.local/share/voice-standalone/audit.jsonl")

_SERVICES = ["voice-daemon", "voice-tray"]
_PROPS = "ActiveState,SubState,NRestarts,MemoryCurrent,MainPID,ActiveEnterTimestamp"

_stop_requested = False


def _handle_sigterm(_signum, _frame) -> None:
    global _stop_requested
    _stop_requested = True


def _service_state(name: str) -> dict:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", name, "-p", _PROPS],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"service": name, "error": str(exc)}
    state = {"service": name}
    for line in result.stdout.strip().splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        state[key] = value
    return state


def _audit_count_since(cutoff_ts: float) -> int:
    """Counts audit.jsonl entries written since cutoff_ts. Cheap and
    approximate on purpose (re-reads the whole file each sample) — this
    file stays small (a personal activity log, not a firehose) for the
    scale this runs at, and correctness/simplicity matters more here than
    avoiding a re-read every 5 minutes."""
    if not os.path.exists(_AUDIT_PATH):
        return 0
    count = 0
    try:
        with open(_AUDIT_PATH) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    ts = time.mktime(time.strptime(entry["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if ts >= cutoff_ts:
                    count += 1
    except OSError:
        return 0
    return count


def _load_state() -> dict:
    """Returns the persisted soak state, or a fresh one if none exists
    yet. `first_started` is wall-clock (for the summary's human-readable
    "when did this soak begin"); `cumulative_elapsed_seconds` is real
    observed time only, the thing that actually counts toward the 7-day
    target. `completed` marks a soak that already reached its target —
    loading a completed state means "this is a new soak," not "resume
    one that's already done"."""
    default = {"first_started": None, "cumulative_elapsed_seconds": 0.0, "completed": False}
    if not os.path.exists(_STATE_PATH):
        return _migrate_legacy_log_if_present(default)
    try:
        with open(_STATE_PATH) as f:
            state = json.load(f)
        for key, value in default.items():
            state.setdefault(key, value)
        return state
    except (OSError, json.JSONDecodeError):
        return default


def _migrate_legacy_log_if_present(default: dict) -> dict:
    """One-time bridge for a soak that was already running under the
    pre-resume version of this script (no state file existed yet, but
    real samples were already logged) — bootstraps cumulative_elapsed
    from the last sample's elapsed_hours instead of silently discarding
    already-collected data on the first run of the fixed version."""
    if not os.path.exists(_LOG_PATH):
        return default
    last_elapsed_hours = None
    first_timestamp = None
    try:
        with open(_LOG_PATH) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if first_timestamp is None:
                    first_timestamp = entry.get("timestamp")
                if "elapsed_hours" in entry:
                    last_elapsed_hours = entry["elapsed_hours"]
    except OSError:
        return default
    if last_elapsed_hours is None:
        return default
    print(f"[soak] migrating pre-resume log: bootstrapping {last_elapsed_hours}h already collected")
    return {
        "first_started": first_timestamp,
        "cumulative_elapsed_seconds": last_elapsed_hours * 3600,
        "completed": False,
    }


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    tmp_path = _STATE_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(state, f)
        os.replace(tmp_path, _STATE_PATH)  # atomic — never a half-written state file
    except OSError as exc:
        print(f"[soak] failed to save state: {exc}")


def _archive_completed_cycle() -> None:
    """Called only when starting a genuinely new soak after a previous
    one already reached its 7-day target — moves the old log/summary
    aside instead of letting a new cycle's samples mix into the same
    file as the finished one's."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    for path in (_LOG_PATH, _SUMMARY_PATH):
        if os.path.exists(path):
            os.rename(path, f"{path}.{stamp}.done")


def _take_sample(elapsed_seconds: float) -> dict:
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_hours": round(elapsed_seconds / 3600, 2),
        "services": {name: _service_state(name) for name in _SERVICES},
        "commands_in_last_interval": None,  # filled by caller, which knows the interval
    }


def _write_sample(sample: dict) -> None:
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(json.dumps(sample) + "\n")
    except OSError as exc:
        print(f"[soak] failed to write sample: {exc}")


def _load_samples() -> list[dict]:
    if not os.path.exists(_LOG_PATH):
        return []
    samples = []
    with open(_LOG_PATH) as f:
        for line in f:
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return samples


def _write_summary(first_started: str, elapsed_seconds: float, stopped_early: bool) -> None:
    samples = _load_samples()
    first_started_epoch = (
        time.mktime(time.strptime(first_started[:19], "%Y-%m-%dT%H:%M:%S"))
        if first_started else time.time() - elapsed_seconds
    )
    summary = {
        "first_started": first_started or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "run_end": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_hours_observed": round(elapsed_seconds / 3600, 2),
        "stopped_early": stopped_early,
        "total_samples": len(samples),
        "total_commands_processed": _audit_count_since(first_started_epoch),
    }
    for name in _SERVICES:
        states = [s["services"].get(name, {}) for s in samples]
        active_count = sum(1 for st in states if st.get("ActiveState") == "active")
        restarts = [int(st["NRestarts"]) for st in states if str(st.get("NRestarts", "")).isdigit()]
        mem_values = [
            int(st["MemoryCurrent"]) for st in states
            if str(st.get("MemoryCurrent", "")).isdigit() and st.get("MemoryCurrent") != "18446744073709551615"
        ]
        summary[name] = {
            "uptime_pct": round(100 * active_count / len(states), 1) if states else None,
            "restart_count_final": restarts[-1] if restarts else None,
            "restart_count_delta": (restarts[-1] - restarts[0]) if len(restarts) >= 2 else None,
            "memory_min_mb": round(min(mem_values) / 1024 / 1024, 1) if mem_values else None,
            "memory_max_mb": round(max(mem_values) / 1024 / 1024, 1) if mem_values else None,
            "memory_avg_mb": round(sum(mem_values) / len(mem_values) / 1024 / 1024, 1) if mem_values else None,
        }
    try:
        with open(_SUMMARY_PATH, "w") as f:
            json.dump(summary, f, indent=2)
    except OSError as exc:
        print(f"[soak] failed to write summary: {exc}")
    print(f"[soak] summary written to {_SUMMARY_PATH}")
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-days", type=float, default=7.0)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    duration_seconds = args.duration_days * 86400
    state = _load_state()

    if state["completed"]:
        print("[soak] previous soak already reached its target — starting a new one, archiving the old log/summary")
        _archive_completed_cycle()
        state = {"first_started": None, "cumulative_elapsed_seconds": 0.0, "completed": False}

    session_start = time.time()
    cumulative_before = state["cumulative_elapsed_seconds"]

    if state["first_started"] is None:
        state["first_started"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        print(f"[soak] starting fresh — {args.duration_days} day(s) target, sampling every {args.interval_seconds}s")
    else:
        print(f"[soak] resuming — {cumulative_before/3600:.2f}h already observed toward the "
              f"{args.duration_days*24:.0f}h target (started {state['first_started']})")
    print(f"[soak] log: {_LOG_PATH}")

    def elapsed_seconds() -> float:
        return cumulative_before + (time.time() - session_start)

    last_sample_time = session_start
    stopped_early = False
    while True:
        if _stop_requested:
            stopped_early = elapsed_seconds() < duration_seconds
            print("[soak] stop requested (SIGTERM/SIGINT) — saving progress and exiting cleanly")
            break
        if elapsed_seconds() >= duration_seconds:
            print("[soak] duration elapsed — writing summary and exiting")
            break

        sample = _take_sample(elapsed_seconds())
        sample["commands_in_last_interval"] = _audit_count_since(last_sample_time)
        last_sample_time = time.time()
        _write_sample(sample)

        # Sleep in short slices so a SIGTERM during a long interval is
        # noticed promptly instead of waiting out the full interval —
        # matters for a clean, fast shutdown, not just a clean one.
        slept = 0.0
        while slept < args.interval_seconds and not _stop_requested and elapsed_seconds() < duration_seconds:
            time.sleep(min(5.0, args.interval_seconds - slept))
            slept += 5.0

    final_elapsed = elapsed_seconds()
    state["cumulative_elapsed_seconds"] = final_elapsed
    state["completed"] = final_elapsed >= duration_seconds
    _save_state(state)

    _write_summary(state["first_started"], final_elapsed, stopped_early)


if __name__ == "__main__":
    main()
