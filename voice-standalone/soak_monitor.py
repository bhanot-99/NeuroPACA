"""
7-day soak monitor for voice-standalone. Samples the health of
voice-daemon.service and voice-tray.service at a fixed interval for a
fixed duration, writes one JSON line per sample, and produces a summary
report on completion — whether that's the full 7 days elapsing, or a
clean early stop (SIGTERM, sent by systemd on logout/shutdown).

Deliberately NOT the main NeuroPaca project's soak infrastructure
(scripts/soak_probe.py etc.) — that reads a daemon-authored health_check()
JSON dump, which voice-standalone's daemon doesn't have. This reads what
actually exists here instead: `systemctl --user show` for each service's
real state (ActiveState, restart count, memory) and the audit log
(skills/_audit.py) for real activity — the daemon's own account of what
it actually did, same "read the source of truth, don't invent one"
principle, just against this project's existing data instead of building
a matching health-dump feature this ask didn't call for.

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


def _take_sample(run_start: float) -> dict:
    sample = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_hours": round((time.time() - run_start) / 3600, 2),
        "services": {name: _service_state(name) for name in _SERVICES},
        "commands_in_last_interval": None,  # filled by caller, which knows the interval
    }
    return sample


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


def _write_summary(run_start: float, stopped_early: bool) -> None:
    samples = _load_samples()
    elapsed_hours = round((time.time() - run_start) / 3600, 2)
    summary = {
        "run_start": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(run_start)),
        "run_end": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_hours": elapsed_hours,
        "stopped_early": stopped_early,
        "total_samples": len(samples),
        "total_commands_processed": _audit_count_since(run_start),
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

    run_start = time.time()
    duration_seconds = args.duration_days * 86400
    deadline = run_start + duration_seconds
    last_sample_time = run_start

    print(f"[soak] starting — {args.duration_days} day(s), sampling every {args.interval_seconds}s")
    print(f"[soak] log: {_LOG_PATH}")

    stopped_early = False
    while True:
        if _stop_requested:
            stopped_early = time.time() < deadline
            print("[soak] stop requested (SIGTERM/SIGINT) — writing summary and exiting cleanly")
            break
        if time.time() >= deadline:
            print("[soak] duration elapsed — writing summary and exiting")
            break

        sample = _take_sample(run_start)
        sample["commands_in_last_interval"] = _audit_count_since(last_sample_time)
        last_sample_time = time.time()
        _write_sample(sample)

        # Sleep in short slices so a SIGTERM during a long interval is
        # noticed promptly instead of waiting out the full interval —
        # matters for a clean, fast shutdown, not just a clean one.
        slept = 0.0
        while slept < args.interval_seconds and not _stop_requested and time.time() < deadline:
            time.sleep(min(5.0, args.interval_seconds - slept))
            slept += 5.0

    _write_summary(run_start, stopped_early)


if __name__ == "__main__":
    main()
