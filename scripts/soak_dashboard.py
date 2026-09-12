#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Soak status as a browsable HTML dashboard -- the explained version.

WHY THIS EXISTS

`soak_7day.sh` and `soak_tray.py` both surface the same terse
`summarise()` block: label, number, at most a half-line of hint. It answers
"is the soak alive" in five seconds but not "what does 'Sessions 20 (3
unclean)' actually mean, and is 3 bad?". This dashboard is that second layer:
every metric gets a plain-language description and a good / watch / bad band,
plus a progress ring and an RSS sparkline.

    scripts/soak_dashboard.py            # write data/soak_dashboard.html
    scripts/soak_dashboard.py --open     # write it and xdg-open it
    scripts/soak_dashboard.py --out /tmp/d.html

NOT A LIVE PAGE, BY DESIGN

It is regenerated from `data/soak/*` every time it is opened (the tray's
"Open dashboard" item calls this module), the same way
`scripts/neuropaca_graph.py` regenerates `graph_view.html`. An already-open
tab does not update itself -- reopen it. One less moving part than a JSON
sidecar + a `file://` fetch loop.

ONE SOURCE OF THE NUMBERS

Like `soak_tray.py`, this imports `soak_state.py` by path and reuses its
`accrued_seconds` / `rss_trend` / `counter_total` / `restarts` / `_humanise`
so the dashboard, the tray and the popup can never disagree about the same
soak. The row-building and rendering below are plain functions over stdlib
types -- no `gi`, no toolkit -- so `tests/test_soak_dashboard.py` exercises
them under the project .venv.

ZERO EGRESS (rules.md §6)

The output is one self-contained HTML file: no CDN, no webfont, no script src,
no fetch. System font stack only. Same guarantee `graph_view_template.html`
carries.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

REPO = Path(__file__).resolve().parent.parent
SOAK_DIR = REPO / "data" / "soak"
STATE_PATH = SOAK_DIR / "state.json"
SAMPLES_PATH = SOAK_DIR / "samples.jsonl"
DEFAULT_OUT = REPO / "data" / "soak_dashboard.html"

Health = Literal["good", "watch", "bad", "neutral", "na"]


def _load_soak_state_module() -> Any:
    """Import scripts/soak_state.py by path -- it is not a package, and this
    file must run standalone under system python3 (from the tray) as well as
    under the project .venv (from pytest). Same shim as soak_tray.py.

    Reuse an already-loaded copy: `soak_tray` loads it before it loads this
    module, and a second copy would mean two distinct `RssTrend` classes."""
    if "soak_state" in sys.modules:
        return sys.modules["soak_state"]
    module_path = REPO / "scripts" / "soak_state.py"
    spec = importlib.util.spec_from_file_location("soak_state", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["soak_state"] = module  # dataclass needs it in sys.modules first
    spec.loader.exec_module(module)
    return module


soak_state = _load_soak_state_module()


# --------------------------------------------------------------------------- rows


@dataclass(frozen=True)
class Row:
    """One metric line: what it is, what it says now, what that means, and
    whether the number is where you want it."""

    group: str
    label: str
    value: str
    description: str
    health: Health = "neutral"


# The warm-window leak slope (post model-load) lives in soak_state now, so the
# dashboard, the popup and the pass/fail verdict all fit the same window
# (problems.md T6). This is a thin alias kept for readability below.
_warm_slope = soak_state.warm_rss_slope


def _rss_slope_health(trend: Any, samples: list[dict[str, Any]]) -> tuple[Health, str]:
    """The leak-slope band, judged on the *warm* window (post model-load), not
    the raw first-to-last delta the popup prints."""
    if trend is None:
        return "na", "not enough samples yet."
    warm = _warm_slope(samples)
    if warm is None:
        return "watch", (
            "still warming up -- the raw figure is dominated by the "
            f"~{trend.peak_mib:.0f} MiB one-time model load, not a leak. "
            "A real slope needs a few hours of samples taken after that."
        )
    slope, hours = warm
    tail = (
        f" (measured over {hours:.0f} h since the model finished loading, "
        f"which is {slope:+.0f} MiB/day -- the headline figure still counts the startup jump)"
    )
    if slope <= 150:
        return (
            "good",
            "resident memory is holding flat over process age. This is the pass condition." + tail,
        )
    if slope <= 800:
        return (
            "watch",
            "a mild upward drift once warm -- worth watching as the run lengthens." + tail,
        )
    return (
        "bad",
        "sustained growth with uptime after warm-up. This is the leak the soak is built to catch."
        + tail,
    )


def build_rows(
    state: dict[str, Any],
    samples: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[Row]:
    now = now or soak_state._now()
    h = soak_state._humanise
    accrued = soak_state.accrued_seconds(state, now)
    target = state["target_seconds"]
    sessions = state["sessions"]
    unclean = sum(1 for s in sessions if s.get("reason") == "unclean")
    rows: list[Row] = []

    # ---- Progress -----------------------------------------------------------
    rows.append(
        Row(
            "Progress",
            "Accrued runtime",
            h(accrued),
            "Total time the daemon has actually been alive, summed across every "
            "start/stop. Completion is 7 days of THIS, not 7 days on the calendar "
            "-- a machine switched off overnight ages no process.",
            "neutral",
        )
    )
    rows.append(
        Row(
            "Progress",
            "Remaining",
            h(max(0.0, target - accrued)),
            "Accrued runtime still needed to reach 7 days.",
            "good" if accrued >= target else "neutral",
        )
    )
    if state.get("first_started_utc"):
        wall = (now - soak_state._parse(state["first_started_utc"])).total_seconds()
        gap = wall - accrued
        rows.append(
            Row(
                "Progress",
                "Wall clock",
                f"{h(wall)} since first start",
                "Calendar time since the soak first began. The gap versus accrued "
                f"runtime ({h(gap)}) is time the machine spent off or asleep -- expected, "
                "and the honest reason a soak started last week is not done.",
                "neutral",
            )
        )
    frac = unclean / len(sessions) if sessions else 0.0
    rows.append(
        Row(
            "Progress",
            "Sessions",
            f"{len(sessions)} ({unclean} ended unclean)",
            "One daemon start-to-stop is a session; their runtimes are added up. "
            "'Unclean' means no graceful shutdown arrived -- a crash, a hard reset, "
            "an OOM kill. A few over a week is normal; a rising share means "
            "something keeps killing the daemon.",
            "good" if unclean == 0 else "watch" if frac <= 0.25 else "bad",
        )
    )

    if not samples:
        rows.append(
            Row(
                "Measurements",
                "Samples",
                "0",
                "No health readings yet -- the first lands within a minute of the "
                "daemon coming up.",
                "watch",
            )
        )
        return rows

    latest = samples[-1]

    # ---- Memory -----------------------------------------------------------
    rows.append(
        Row(
            "Measurements",
            "Samples",
            f"{len(samples)} ({soak_state.restarts(samples)} daemon restarts)",
            "One reading per minute of the daemon's own health-dump file "
            "(scripts/soak_probe.py). Restarts are counted from the daemon's own "
            "counters resetting to zero.",
            "neutral",
        )
    )
    trend = soak_state.rss_trend(samples)
    if trend is None:
        rows.append(
            Row(
                "Memory",
                "Resident memory",
                "not enough samples yet",
                "Needs a few readings before a trend exists.",
                "na",
            )
        )
    else:
        arrow = "+" if trend.delta_mib >= 0 else ""
        mem_value = (
            f"{trend.last_mib:.0f} MiB now  ({arrow}{trend.delta_mib:.0f} over "
            f"{h(trend.span_seconds)}, peak {trend.peak_mib:.0f})"
        )
        rows.append(
            Row(
                "Memory",
                "Resident memory",
                mem_value,
                "Physical RAM the daemon holds. Expect a one-time climb to ~1.4 GB "
                "as the BitNet model maps in (mostly the memory-mapped GGUF), then "
                "a flat line.",
                "neutral",
            )
        )
        slope_health, slope_why = _rss_slope_health(trend, samples)
        rows.append(
            Row(
                "Memory",
                "Leak slope",
                f"{trend.mib_per_day:+.1f} MiB/day",
                "Rate of memory growth per day of uptime -- the single number this "
                f"whole soak exists to produce. {slope_why}",
                slope_health,
            )
        )

    # ---- Graph & sensing --------------------------------------------------
    rows.append(
        Row(
            "Graph & sensing",
            "Behavioural graph",
            f"{latest.get('graph_nodes', '?')} nodes, {latest.get('graph_edges', '?')} edges",
            "Size of the graph the daemon has built from what it observed. Should "
            "grow while the machine is used, then settle as housekeeping prunes "
            "stale nodes.",
            "neutral",
        )
    )
    sens_edges = soak_state.counter_total(samples, "activity_edges")
    sens_sw = soak_state.counter_total(samples, "app_switches")
    rows.append(
        Row(
            "Graph & sensing",
            "Sensing",
            f"{sens_edges} idle/active edges, {sens_sw} app switches",
            "Raw activity the low-level collectors picked up. Non-zero means the "
            "sensing path is alive; a week of zeros on a machine in use is the "
            "failure B7 taught this project to never miss.",
            "good" if (sens_edges + sens_sw) > 0 else "bad",
        )
    )
    census_now = latest.get("census_groups", 0)
    census_peak = max((s.get("census_groups", 0) for s in samples), default=0)
    rows.append(
        Row(
            "Graph & sensing",
            "Per-app census",
            f"{census_now} groups now (peak {census_peak})",
            "Apps the per-process memory scanner is tracking, grouped by name, at "
            "≥ 200 MB of resident memory (B13). Should stay bounded -- a couple "
            "of dozen at most on a busy desktop.",
            "good" if census_peak <= 40 else "watch",
        )
    )

    # ---- The loop -------------------------------------------------------
    d_contrib = soak_state.counter_total(samples, "pressure_events")
    d_low = soak_state.counter_total(samples, "pressure_low")
    d_high = soak_state.counter_total(samples, "pressure_high")
    rows.append(
        Row(
            "The loop",
            "Drive",
            f"{d_contrib} pressure contributions, {d_low} low / {d_high} high crossings",
            "How much the system has built up toward acting, and how often that "
            "crossed a threshold. Long stretches at zero while the machine is in "
            "use mean the loop is not being fed -- exactly the gap B13 set out to "
            "close.",
            "good" if d_contrib > 0 else "watch",
        )
    )
    ins = soak_state.counter_total(samples, "insights")
    prop = soak_state.counter_total(samples, "proposed")
    rows.append(
        Row(
            "The loop",
            "Cognition",
            f"{ins} insights, {prop} actions proposed",
            "Insights the model actually wrote into the graph, and actions it "
            "proposed (dry-run -- nothing executes). This is the end product of "
            "the whole pipeline.",
            "good" if ins > 0 else "watch",
        )
    )

    # ---- Health -------------------------------------------------------
    errs = soak_state.counter_total(samples, "errors")
    dropped = soak_state.counter_total(samples, "events_dropped")
    audit = latest.get("actions", 0)
    rows.append(
        Row(
            "Health",
            "Errors & drops",
            f"{errs} errors, {dropped} events dropped, {audit} audit lines",
            "Errors the daemon logged, events lost because a queue was full, and "
            "entries written to the action audit trail. Errors and drops should "
            "both be zero.",
            "good" if (errs == 0 and dropped == 0) else "bad",
        )
    )
    up = bool(latest.get("daemon_up", True))
    rows.append(
        Row(
            "Health",
            "Daemon",
            "up at last sample" if up else "DOWN at last sample",
            "Whether the daemon answered the most recent health probe. A brief "
            "'down' across a restart is fine; a persistent one is not.",
            "good" if up else "bad",
        )
    )
    degraded = latest.get("degraded") or []
    rows.append(
        Row(
            "Health",
            "Degraded modules",
            ", ".join(degraded) if degraded else "none",
            "Modules reporting themselves as not fully working. Empty is the goal.",
            "good" if not degraded else "bad",
        )
    )
    return rows


# --------------------------------------------------------------------------- render

_CSS = """
:root{
  --bg:#f4f5f7; --card:#ffffff; --line:#e5e8ec; --line2:#d7dbe0;
  --ink:#1a1d23; --body:#404650; --muted:#6b7280; --faint:#9aa1ad;
  --good:#1c7c4a; --good-bg:#e7f2ec; --watch:#8a6100; --watch-bg:#f6efdd;
  --bad:#b23b3b; --bad-bg:#f6e6e6; --accent:#3160a8; --track:#e3e7ec;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#14161b; --card:#1c1f26; --line:#2a2e37; --line2:#39404c;
    --ink:#e8eaef; --body:#c4c9d2; --muted:#98a0ac; --faint:#6c7480;
    --good:#5cbd88; --good-bg:#17271f; --watch:#d6a44f; --watch-bg:#2a2213;
    --bad:#e07777; --bad-bg:#2b1818; --accent:#6b9bdc; --track:#2a2e37;
  }
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--body);
  font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,Cantarell,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.mono{
  font-family:ui-monospace,"Cascadia Code","JetBrains Mono","Roboto Mono",
    SFMono-Regular,Menlo,monospace;
}
.wrap{max-width:1000px;margin:0 auto;padding:40px 22px 80px}
header{display:flex;flex-wrap:wrap;gap:28px;align-items:center;margin-bottom:8px}
.ring{flex:0 0 auto}
.head-txt{flex:1 1 260px;min-width:240px}
.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint)}
h1{margin:.3rem 0 .1rem;font-size:24px;letter-spacing:-.01em;color:var(--ink);font-weight:650}
.status-line{font-size:14px;color:var(--body)}
.status-line b{color:var(--ink)}
.gen{margin-top:6px;font-size:12px;color:var(--faint)}
.spark-card{
  margin:24px 0 8px;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:16px 18px;
}
.spark-card .t{
  font-size:12px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted);margin-bottom:2px;
}
.spark-card .v{font-size:19px;color:var(--ink);font-weight:600}
.spark-card .v small{font-size:13px;color:var(--muted);font-weight:400}
svg{display:block;max-width:100%}
.group{margin-top:30px}
.group h2{
  font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
  margin:0 0 10px;padding-bottom:8px;border-bottom:1px solid var(--line);font-weight:600;
}
.rows{
  display:flex;flex-direction:column;gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:12px;overflow:hidden;
}
.row{
  display:grid;grid-template-columns:170px minmax(160px,300px) 1fr;gap:18px;
  background:var(--card);padding:14px 18px;align-items:start;
}
@media(max-width:720px){ .row{grid-template-columns:1fr;gap:4px} }
.row .label{font-weight:600;color:var(--ink);font-size:14px}
.row .value{color:var(--body)}
.row .value .dot{
  display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:1px;
}
.row .desc{color:var(--muted);font-size:13.5px;line-height:1.55}
.badge{
  display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
  padding:2px 7px;border-radius:5px;margin-left:8px;vertical-align:1px;
}
.h-good .dot{background:var(--good)}
.h-good .badge{background:var(--good-bg);color:var(--good)}
.h-watch .dot{background:var(--watch)}
.h-watch .badge{background:var(--watch-bg);color:var(--watch)}
.h-bad .dot{background:var(--bad)}
.h-bad .badge{background:var(--bad-bg);color:var(--bad)}
.h-neutral .dot{background:var(--faint)}
.h-na .dot{background:var(--line2)}
footer{
  margin-top:44px;padding-top:18px;border-top:1px solid var(--line);
  font-size:12px;color:var(--faint);line-height:1.7;
}
"""

_RING_R = 52
_RING_C = 2 * 3.141592653589793 * _RING_R


def _ring_svg(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    off = _RING_C * (1 - pct / 100.0)
    return (
        f'<svg class="ring" width="128" height="128" viewBox="0 0 128 128" '
        f'role="img" aria-label="{pct:.1f} percent of the 7-day target">'
        f'<circle cx="64" cy="64" r="{_RING_R}" fill="none" '
        f'stroke="var(--track)" stroke-width="11"/>'
        f'<circle cx="64" cy="64" r="{_RING_R}" fill="none" '
        f'stroke="var(--accent)" stroke-width="11" '
        f'stroke-linecap="round" stroke-dasharray="{_RING_C:.1f}" stroke-dashoffset="{off:.1f}" '
        f'transform="rotate(-90 64 64)"/>'
        f'<text x="64" y="60" text-anchor="middle" font-size="22" font-weight="700" '
        f'fill="var(--ink)" font-family="ui-monospace,monospace">{pct:.0f}%</text>'
        f'<text x="64" y="80" text-anchor="middle" font-size="10.5" fill="var(--muted)" '
        f'letter-spacing="1">OF 7 DAYS</text>'
        f"</svg>"
    )


def _sparkline_svg(samples: list[dict[str, Any]], w: int = 940, hgt: int = 64) -> str:
    pts = [float(s["rss_mib"]) for s in samples if "rss_mib" in s and s.get("daemon_up", True)]
    pts = pts[-240:]  # last ~4 h at 1/min
    if len(pts) < 2:
        return (
            '<div class="mono" style="color:var(--faint);font-size:12px">'
            "sparkline needs more samples</div>"
        )
    lo, hi = min(pts), max(pts)
    span = hi - lo or 1.0
    n = len(pts)
    coords = [
        (i / (n - 1) * (w - 2) + 1, hgt - 2 - (v - lo) / span * (hgt - 4))
        for i, v in enumerate(pts)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"1,{hgt - 1} " + line + f" {w - 1:.1f},{hgt - 1}"
    ex, ey = coords[-1]
    return (
        f'<svg width="{w}" height="{hgt}" viewBox="0 0 {w} {hgt}" preserveAspectRatio="none" '
        f'role="img" aria-label="resident memory, last {n} samples">'
        f'<polygon points="{area}" fill="var(--accent)" fill-opacity="0.10"/>'
        f'<polyline points="{line}" fill="none" stroke="var(--accent)" stroke-width="1.6" '
        f'stroke-linejoin="round"/>'
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="2.6" fill="var(--accent)"/>'
        f"</svg>"
    )


_HEALTH_LABEL = {"good": "OK", "watch": "WATCH", "bad": "CHECK", "neutral": "", "na": ""}


def render(
    state: dict[str, Any],
    samples: list[dict[str, Any]],
    now: datetime | None = None,
) -> str:
    now = now or soak_state._now()
    rows = build_rows(state, samples, now)
    accrued = soak_state.accrued_seconds(state, now)
    pct = min(100.0, 100.0 * accrued / state["target_seconds"])
    complete = bool(state.get("completed_utc"))
    latest = samples[-1] if samples else {}
    daemon_up = bool(latest.get("daemon_up", True)) if samples else True

    if complete:
        status = f"<b>Soak complete.</b> {soak_state._humanise(accrued)} of accrued runtime."
    elif not samples:
        status = f"<b>Session {len(state['sessions'])} running.</b> No measurements yet."
    elif not daemon_up:
        status = (
            f"<b>Session {len(state['sessions'])} running</b> "
            "— but the daemon was down at the last sample."
        )
    else:
        status = (
            f"<b>Session {len(state['sessions'])} running.</b> Daemon healthy at the last sample."
        )

    trend = soak_state.rss_trend(samples) if samples else None
    if trend is not None:
        spark_v = (
            f"{trend.last_mib:.0f} <small>MiB now &middot; "
            f"{'+' if trend.delta_mib >= 0 else ''}{trend.delta_mib:.0f} over "
            f"{soak_state._humanise(trend.span_seconds)} &middot; "
            f"slope {trend.mib_per_day:+.0f} MiB/day</small>"
        )
    else:
        spark_v = "<small>not enough samples</small>"

    groups: list[str] = []
    seen: list[str] = []
    for r in rows:
        if r.group not in seen:
            seen.append(r.group)
    for gname in seen:
        grows = [r for r in rows if r.group == gname]
        row_html = []
        for r in grows:
            badge = _HEALTH_LABEL[r.health]
            badge_html = f'<span class="badge">{badge}</span>' if badge else ""
            row_html.append(
                f'<div class="row h-{r.health}">'
                f'<div class="label">{html.escape(r.label)}</div>'
                f'<div class="value mono"><span class="dot"></span>'
                f"{html.escape(r.value)}{badge_html}</div>"
                f'<div class="desc">{html.escape(r.description)}</div>'
                f"</div>"
            )
        groups.append(
            f'<section class="group"><h2>{html.escape(gname)}</h2>'
            f'<div class="rows">{"".join(row_html)}</div></section>'
        )

    gen = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Soak &middot; detailed monitor</title>
<!-- Generated by scripts/soak_dashboard.py. Self-contained: no network,
     no webfont, no script. Regenerate by reopening it from the tray. -->
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    {_ring_svg(pct)}
    <div class="head-txt">
      <div class="eyebrow">NeuroPACA &middot; seven-day soak</div>
      <h1>Detailed monitor</h1>
      <div class="status-line">{status}</div>
      <div class="gen mono">regenerated {gen}</div>
    </div>
  </header>

  <div class="spark-card">
    <div class="t">Resident memory &mdash; last {min(len(samples), 240)} samples</div>
    <div class="v mono">{spark_v}</div>
    {_sparkline_svg(samples) if samples else ""}
  </div>

  {"".join(groups)}

  <footer>
    Throwaway B9-hardening instrument &mdash; reads <span class="mono">data/soak/*</span>,
    no daemon coupling. The tray widget and login popup show the same numbers without the
    descriptions. Bands (OK / WATCH / CHECK) are rules of thumb, not gates.
  </footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------- cli


def generate(out: Path = DEFAULT_OUT) -> Path:
    """Read the soak state and write the dashboard HTML. Never raises for a
    missing/unreadable soak -- writes a page that says so, same stance as the
    tray."""
    try:
        state = soak_state.load_state(STATE_PATH)
        samples = soak_state.read_samples(SAMPLES_PATH) if SAMPLES_PATH.exists() else []
        page = render(state, samples)
    except Exception as exc:
        page = (
            "<!doctype html><meta charset=utf-8><title>soak</title>"
            "<body style='font-family:system-ui;padding:40px;max-width:640px;margin:auto'>"
            f"<h1>Soak state unreadable</h1><pre>{html.escape(str(exc))}</pre>"
            "<p>Nothing has written a readable <code>data/soak/state.json</code> yet.</p>"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render the Soak status as an HTML dashboard.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the HTML")
    ap.add_argument("--open", action="store_true", help="xdg-open the file after writing it")
    args = ap.parse_args(argv)

    path = generate(args.out)
    print(f"wrote {path}")
    if args.open:
        subprocess.Popen(  # fixed argv, no shell
            ["xdg-open", str(path)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

# gen-ref: 5a013b2b
