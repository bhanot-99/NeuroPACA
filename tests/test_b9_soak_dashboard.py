# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B9 · detailed dashboard logic (`scripts/b9_soak_dashboard.py`).

Pure functions only -- `build_rows()`, `render()`, `_warm_slope()`, `generate()`.
No `gi`, no toolkit; imports under the project .venv like `test_b9_soak_tray.py`.
The tray's "Open detailed dashboard" glue calls `generate()` + `xdg-open` and is
verified live, not here.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "b9_soak_dashboard.py"
_spec = importlib.util.spec_from_file_location("b9_soak_dashboard", _MODULE_PATH)
assert _spec and _spec.loader
dash = importlib.util.module_from_spec(_spec)
sys.modules["b9_soak_dashboard"] = dash  # registered before exec, see test_b9_soak_state.py
_spec.loader.exec_module(dash)

T0 = datetime(2026, 9, 4, 9, 0, 0, tzinfo=UTC)
_Z = lambda d: d.isoformat().replace("+00:00", "Z")  # noqa: E731


def _state(n_sessions: int = 4, unclean: int = 1) -> dict:
    sessions = []
    for i in range(n_sessions):
        start = T0 + timedelta(hours=i * 8)
        sessions.append(
            {
                "started_utc": _Z(start),
                "ended_utc": _Z(start + timedelta(hours=6)),
                "heartbeat_utc": _Z(start + timedelta(hours=6)),
                "reason": "unclean" if i < unclean else "sigterm",
                "samples": 300,
            }
        )
    return {
        "version": 1,
        "target_seconds": 7 * 86400,
        "first_started_utc": _Z(T0),
        "completed_utc": None,
        "sessions": sessions,
    }


def _samples(
    n: int = 300,
    *,
    rss_flat: float = 1467.0,
    leak_per_min: float = 0.0,
    errors: int = 0,
    insights: int = 1,
    census: int = 8,
) -> list[dict]:
    out = []
    for i in range(n):
        t = T0 + timedelta(minutes=i)
        rss = 44.0 if i < 3 else rss_flat + (i - 3) * leak_per_min
        out.append(
            {
                "ts": _Z(t),
                "daemon_up": True,
                "rss_mib": round(rss, 1),
                "graph_nodes": 12 + min(i, 14),
                "graph_edges": 1 + min(i, 19),
                "activity_edges": i // 5,
                "app_switches": i // 10,
                "census_groups": census,
                "signals": min(i // 40, 9),
                "pressure_events": min(i // 30, 10),
                "pressure_low": 1 if i > 25 else 0,
                "pressure_high": 0,
                "insights": insights,
                "proposed": 1 if insights else 0,
                "errors": errors,
                "events_dropped": 0,
                "actions": 2,
                "degraded": [],
            }
        )
    return out


def _row(rows: list, label: str):
    return next(r for r in rows if r.label == label)


# --------------------------------------------------------------------- build_rows


def test_every_row_has_a_nonempty_description() -> None:
    rows = dash.build_rows(_state(), _samples(), T0 + timedelta(days=1))
    assert rows
    for r in rows:
        assert r.description.strip(), f"{r.label} has no description"
        assert r.health in ("good", "watch", "bad", "neutral", "na")
        assert r.group


def test_sessions_row_explains_unclean_and_bands_on_the_fraction() -> None:
    clean = _row(dash.build_rows(_state(unclean=0), _samples(), T0 + timedelta(days=1)), "Sessions")
    assert clean.health == "good"
    assert "unclean" in clean.description.lower()

    many = _row(
        dash.build_rows(_state(n_sessions=4, unclean=3), _samples(), T0 + timedelta(days=1)),
        "Sessions",
    )
    assert many.health == "bad"  # 3/4 unclean


def test_leak_slope_is_judged_on_the_warm_window_not_the_cold_start_jump() -> None:
    # RSS goes 44 -> 1467 then dead flat. Raw first/last slope is enormous;
    # warm-window slope is ~0, so the band must read good, not bad.
    rows = dash.build_rows(_state(), _samples(n=600, leak_per_min=0.0), T0 + timedelta(days=1))
    slope = _row(rows, "Leak slope")
    assert slope.health == "good"
    assert "model load" in slope.description or "startup jump" in slope.description


def test_leak_slope_flags_a_real_post_warmup_leak() -> None:
    # +2 MiB/min after warm-up = ~2880 MiB/day of genuine growth
    rows = dash.build_rows(_state(), _samples(n=600, leak_per_min=2.0), T0 + timedelta(days=1))
    assert _row(rows, "Leak slope").health == "bad"


def test_errors_and_degraded_drive_the_health_band() -> None:
    ok = dash.build_rows(_state(), _samples(errors=0), T0 + timedelta(days=1))
    assert _row(ok, "Errors & drops").health == "good"
    bad = dash.build_rows(_state(), _samples(errors=3), T0 + timedelta(days=1))
    assert _row(bad, "Errors & drops").health == "bad"


def test_no_samples_yet_still_builds_progress_rows() -> None:
    rows = dash.build_rows(_state(), [], T0 + timedelta(hours=2))
    labels = [r.label for r in rows]
    assert "Accrued runtime" in labels and "Sessions" in labels
    assert _row(rows, "Samples").health == "watch"


def test_zero_sensing_is_flagged_bad() -> None:
    dead = _samples()
    for s in dead:
        s["activity_edges"] = 0
        s["app_switches"] = 0
    assert _row(dash.build_rows(_state(), dead, T0 + timedelta(days=1)), "Sensing").health == "bad"


# ------------------------------------------------------------------------- render


def test_render_is_self_contained_no_network_no_script() -> None:
    page = dash.render(_state(), _samples(), T0 + timedelta(days=1))
    assert page.lstrip().startswith("<!doctype html>")
    low = page.lower()
    for forbidden in ("http://", "https://", "<script", "src=", "@import", "fonts.googleapis"):
        assert forbidden not in low, f"dashboard must be offline — found {forbidden!r}"


def test_render_contains_the_ring_the_sparkline_and_every_label() -> None:
    import html as _html

    rows = dash.build_rows(_state(), _samples(), T0 + timedelta(days=1))
    page = dash.render(_state(), _samples(), T0 + timedelta(days=1))
    assert "<svg" in page and "stroke-dashoffset" in page  # progress ring
    assert "polyline" in page  # sparkline
    for r in rows:
        assert _html.escape(r.label) in page


def test_render_escapes_degraded_module_names() -> None:
    s = _samples()
    s[-1]["degraded"] = ["<script>x</script>"]
    page = dash.render(_state(), s, T0 + timedelta(days=1))
    assert "<script>x" not in page
    assert "&lt;script&gt;x" in page


def test_generate_writes_a_page_even_with_no_soak_dir(tmp_path: pytest.TempPathFactory) -> None:
    out = Path(tmp_path) / "d.html"  # type: ignore[arg-type]
    # STATE_PATH points at the real (wiped) data dir; generate must not raise
    result = dash.generate(out)
    assert result == out
    text = out.read_text("utf-8")
    assert "<!doctype" in text.lower()


# gen-ref: 0da0bfad
