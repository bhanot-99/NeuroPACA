"""B9 · 7-day soak bookkeeping — state math and the human summary (BL-5).

Why this is Python and not more bash inside `b9_soak_7day.sh`:

The soak no longer runs as one uninterrupted `systemd-inhibit` invocation. It
starts with the graphical session and stops when the machine does, so a "7-day
soak" is now a *sequence of sessions* whose runtime has to be added up across
power cycles, healed after an unclean shutdown, and summarised in a popup that a
human reads at every boot. That is arithmetic with edge cases, and arithmetic
with edge cases belongs somewhere it can be tested -- see
`tests/test_b9_soak_state.py`. The driver script keeps only the parts that must
be shell: signal traps, sampling, and the systemd handshake.

THE COMPLETION RULE (stated because it is a judgement, not an obvious default).

Completion is 7 days of **accrued daemon runtime**, not 7 days of wall clock
elapsed since the first start. A soak exists to catch what only appears with
process age -- RSS creep, unbounded buffers, scores that never settle, a race
that needs 50 000 events. A box that is powered off for twelve hours a night
accrues no process age in those hours, and counting them would let a 3.5-day
soak claim a 7-day result. That is exactly the failure that ended the B2 soak at
11 h of a 24 h window (a suspending laptop), and this criterion is supposed to
*subsume* the carried B1/B2/B4 windows rather than repeat their mistake. Both
numbers are recorded and shown; only accrued runtime advances the bar.

Commands:
    open      start a session, healing any session a power cut left open
    sample    append one measurement and beat the open session's heartbeat
    close     end the open session with a reason
    summary   render the popup / stdout text
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TARGET_SECONDS = 7 * 24 * 60 * 60
STATE_VERSION = 1


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _humanise(seconds: float) -> str:
    """`93784` -> `1d 2h 03m`. Days matter here; seconds never do."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": STATE_VERSION,
            "target_seconds": TARGET_SECONDS,
            "first_started_utc": None,
            "completed_utc": None,
            "sessions": [],
        }
    state: dict[str, Any] = json.loads(path.read_text())
    if state.get("version") != STATE_VERSION:
        raise SystemExit(
            f"{path}: state version {state.get('version')!r} is not {STATE_VERSION} -- "
            "refusing to guess at a format this build does not know"
        )
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic: temp -> replace. A power cut mid-write must not eat the record of
    six days of soak, which is the one thing here that cannot be regenerated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(path)


def accrued_seconds(state: dict[str, Any], now: datetime | None = None) -> float:
    """Total runtime across every session, including the one still open."""
    now = now or _now()
    total = 0.0
    for session in state["sessions"]:
        started = _parse(session["started_utc"])
        if session.get("ended_utc"):
            total += (_parse(session["ended_utc"]) - started).total_seconds()
        else:
            total += (now - started).total_seconds()
    return total


def open_session(state: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Start a session, first healing one that was never closed.

    An unclean shutdown -- a power cut, a hard reset, an OOM kill -- means no
    SIGTERM ever arrived and the previous session is still marked open. Counting
    it as running until *now* would credit the soak with every hour the machine
    spent switched off. The last heartbeat is the newest moment the daemon is
    known to have been alive, so that is where the session is closed, and it is
    labelled `unclean` so the record shows the run was interrupted rather than
    quietly smoothing over it.
    """
    now = now or _now()
    if state["sessions"] and not state["sessions"][-1].get("ended_utc"):
        stale = state["sessions"][-1]
        stale["ended_utc"] = stale.get("heartbeat_utc") or stale["started_utc"]
        stale["reason"] = "unclean"
    session = {
        "started_utc": _iso(now),
        "heartbeat_utc": _iso(now),
        "ended_utc": None,
        "reason": None,
        "samples": 0,
    }
    state["sessions"].append(session)
    if not state["first_started_utc"]:
        state["first_started_utc"] = _iso(now)
    return session


def close_session(state: dict[str, Any], reason: str, now: datetime | None = None) -> None:
    now = now or _now()
    if not state["sessions"] or state["sessions"][-1].get("ended_utc"):
        return
    session = state["sessions"][-1]
    session["ended_utc"] = _iso(now)
    session["reason"] = reason
    if accrued_seconds(state, now) >= state["target_seconds"] and not state["completed_utc"]:
        state["completed_utc"] = _iso(now)


def beat(state: dict[str, Any], now: datetime | None = None) -> None:
    now = now or _now()
    if state["sessions"] and not state["sessions"][-1].get("ended_utc"):
        state["sessions"][-1]["heartbeat_utc"] = _iso(now)
        state["sessions"][-1]["samples"] += 1
    if accrued_seconds(state, now) >= state["target_seconds"] and not state["completed_utc"]:
        state["completed_utc"] = _iso(now)


# ------------------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RssTrend:
    """What the soak is actually for: does resident memory grow with uptime?"""

    first_mib: float
    last_mib: float
    peak_mib: float
    span_seconds: float

    @property
    def delta_mib(self) -> float:
        return self.last_mib - self.first_mib

    @property
    def mib_per_day(self) -> float:
        if self.span_seconds < 600:
            # Under ten minutes the slope is noise amplified by a tiny divisor;
            # reporting 4000 MiB/day from a 30 MiB startup allocation would be
            # worse than reporting nothing.
            return 0.0
        return self.delta_mib * 86_400.0 / self.span_seconds


def read_samples(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # A sample truncated by a power cut is one lost measurement, not a
            # reason to fail the summary that reports the other 9 999.
            continue
    return rows


def segments(samples: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split the sample series wherever the daemon restarted.

    THIS IS THE LOAD-BEARING PIECE OF THE SUMMARY, and it exists because the
    first version of this file was wrong in a way that would have looked fine.

    `neuropaca health` reports CUMULATIVE counters -- 3 app switches means three
    since the daemon started, not three since the last sample. Summing them
    across 10 080 samples the way a per-interval reading would be summed turns a
    quiet week into millions of phantom events, which is worse than useless: it
    is a soak that reports a thriving system no matter what happened.

    A daemon restart resets every one of those counters at once, and the tell is
    `uptime_seconds` going DOWN. Split there, and each segment holds one daemon
    life whose final sample is that life's true total.
    """
    if not samples:
        return []
    out: list[list[dict[str, Any]]] = [[]]
    previous_uptime = -1.0
    for sample in samples:
        if not sample.get("daemon_up", True):
            continue
        uptime = float(sample.get("uptime_seconds", 0.0))
        if uptime < previous_uptime and out[-1]:
            out.append([])
        out[-1].append(sample)
        previous_uptime = uptime
    return [segment for segment in out if segment]


def counter_total(samples: list[dict[str, Any]], key: str) -> int:
    """Reset-aware total: the last value of each daemon life, added up."""
    return sum(int(segment[-1].get(key, 0) or 0) for segment in segments(samples))


def restarts(samples: list[dict[str, Any]]) -> int:
    return max(0, len(segments(samples)) - 1)


def rss_trend(samples: list[dict[str, Any]]) -> RssTrend | None:
    """Measured over the LONGEST single daemon life, not the whole series.

    A restart drops RSS back to its startup value. Spanning one would subtract a
    fresh 40 MiB process from a week-old one and report a healthy negative slope
    for a daemon that had been leaking steadily right up until it died.
    """
    runs = segments(samples)
    if not runs:
        return None
    longest = max(runs, key=len)
    rss = [s for s in longest if isinstance(s.get("rss_mib"), int | float)]
    if len(rss) < 2:
        return None
    span = (_parse(rss[-1]["ts"]) - _parse(rss[0]["ts"])).total_seconds()
    return RssTrend(
        first_mib=float(rss[0]["rss_mib"]),
        last_mib=float(rss[-1]["rss_mib"]),
        peak_mib=max(float(s["rss_mib"]) for s in rss),
        span_seconds=span,
    )


def summarise(
    state: dict[str, Any],
    samples: list[dict[str, Any]],
    now: datetime | None = None,
) -> str:
    """The text the boot popup shows. Written to be read in five seconds by
    someone who just logged in and is not thinking about NeuroPACA yet."""
    now = now or _now()
    accrued = accrued_seconds(state, now)
    target = state["target_seconds"]
    pct = min(100.0, accrued * 100.0 / target)
    sessions = state["sessions"]
    unclean = sum(1 for s in sessions if s.get("reason") == "unclean")

    lines: list[str] = []
    if state["completed_utc"]:
        lines.append(
            f"SOAK COMPLETE -- {_humanise(accrued)} accrued, finished {state['completed_utc']}."
        )
    else:
        lines.append(
            f"Session {len(sessions)} running. This popup means the soak survived the reboot."
        )
    lines.append("")
    lines.append(f"Progress    {pct:5.1f}%  ({_humanise(accrued)} of 7d accrued runtime)")
    lines.append(f"Remaining   {_humanise(target - accrued)}")

    if state["first_started_utc"]:
        wall = (now - _parse(state["first_started_utc"])).total_seconds()
        # Shown because the gap between the two is the honest cost of a machine
        # that gets switched off, and someone will otherwise ask why a soak
        # started last Tuesday is not done.
        lines.append(
            f"Wall clock  {_humanise(wall)} since first start ({state['first_started_utc']})"
        )

    lines.append(f"Sessions    {len(sessions)} ({unclean} ended unclean)")
    lines.append("")

    if not samples:
        lines.append("No measurements yet -- the first sample lands within a minute.")
        return "\n".join(lines)

    lines.append(f"Samples     {len(samples)}  ({restarts(samples)} daemon restarts)")
    trend = rss_trend(samples)
    if trend is None:
        lines.append("Memory      not enough samples yet")
    else:
        arrow = "+" if trend.delta_mib >= 0 else ""
        lines.append(
            f"Memory      {trend.last_mib:.0f} MiB now, {arrow}{trend.delta_mib:.0f} MiB "
            f"over {_humanise(trend.span_seconds)} (peak {trend.peak_mib:.0f})"
        )
        lines.append(
            f"Leak slope  {trend.mib_per_day:+.1f} MiB/day"
            "  <- the number this soak exists to produce"
        )

    latest = samples[-1]
    if not latest.get("daemon_up", True):
        lines.append("Daemon      DOWN at the last sample")
    lines.append(
        f"Graph       {latest.get('graph_nodes', '?')} nodes, "
        f"{latest.get('graph_edges', '?')} edges"
    )
    lines.append(
        f"Sensing     {counter_total(samples, 'activity_edges')} idle/active edges, "
        f"{counter_total(samples, 'app_switches')} app switches"
    )
    lines.append(
        f"Drive       {counter_total(samples, 'pressure_events')} contributions, "
        f"{counter_total(samples, 'pressure_low')} low / "
        f"{counter_total(samples, 'pressure_high')} high crossings"
    )
    lines.append(
        f"Cognition   {counter_total(samples, 'insights')} insights, "
        f"{counter_total(samples, 'proposed')} actions proposed"
    )
    lines.append(
        f"Health      {counter_total(samples, 'errors')} errors, "
        f"{counter_total(samples, 'events_dropped')} events dropped, "
        f"{latest.get('actions', 0)} audit lines"
    )
    if latest.get("degraded"):
        lines.append(f"DEGRADED    {', '.join(latest['degraded'])}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B9 soak bookkeeping.")
    parser.add_argument("command", choices=["open", "close", "sample", "summary", "status"])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--samples", type=Path)
    parser.add_argument("--reason", default="stopped")
    parser.add_argument("--metrics", help="JSON object appended as one sample row")
    args = parser.parse_args(argv)

    state = load_state(args.state)

    if args.command == "open":
        open_session(state)
        save_state(args.state, state)
    elif args.command == "close":
        close_session(state, args.reason)
        save_state(args.state, state)
    elif args.command == "sample":
        if not args.samples or not args.metrics:
            parser.error("sample needs --samples and --metrics")
        row = json.loads(args.metrics)
        row.setdefault("ts", _iso(_now()))
        args.samples.parent.mkdir(parents=True, exist_ok=True)
        with args.samples.open("a") as handle:
            handle.write(json.dumps(row) + "\n")
        beat(state)
        save_state(args.state, state)
    elif args.command == "summary":
        samples = read_samples(args.samples) if args.samples else []
        print(summarise(state, samples))
    elif args.command == "status":
        # Exit code is the interface: 0 = done, 1 = still running. Lets the
        # driver decide whether to keep going without parsing prose.
        return 0 if state["completed_utc"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
