"""B9 · tray widget status logic (`scripts/b9_soak_tray.py`).

Only the pure half -- `compute_status()`, `read_status()`, `format_popup_text()`,
`raise_popup()`. None of it imports `gi`: the project .venv has no PyGObject by
design (same reasoning as the lazy `pywayland` import in
`sensing/activity/wayland_idle.py`), and this file has to import the module
under that same venv. The GTK/AppIndicator glue in `_run_tray()` is verified
live, not here -- see the module docstring for why that split exists.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "b9_soak_tray.py"
_spec = importlib.util.spec_from_file_location("b9_soak_tray", _MODULE_PATH)
assert _spec and _spec.loader
tray = importlib.util.module_from_spec(_spec)
# Registered before exec for the same reason test_b9_soak_state.py registers
# b9_soak_state: b9_soak_tray imports that module's slotted dataclass, and
# `dataclasses` resolves annotations through sys.modules[cls.__module__].
sys.modules["b9_soak_tray"] = tray
_spec.loader.exec_module(tray)


T0 = datetime(2026, 9, 4, 9, 0, 0, tzinfo=UTC)


def _closed_session(start: datetime, hours: float, reason: str = "sigterm") -> dict:
    return {
        "started_utc": tray.soak_state._iso(start),
        "heartbeat_utc": tray.soak_state._iso(start + timedelta(hours=hours)),
        "ended_utc": tray.soak_state._iso(start + timedelta(hours=hours)),
        "reason": reason,
        "samples": 1,
    }


def _state(**overrides: object) -> dict:
    base = {
        "version": 1,
        "target_seconds": 7 * 24 * 60 * 60,
        "first_started_utc": tray.soak_state._iso(T0),
        "completed_utc": None,
        "sessions": [_closed_session(T0, 6.0)],
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------------------
# compute_status
# ------------------------------------------------------------------------------


def test_a_fresh_never_started_state_shows_zero_percent_and_the_running_icon() -> None:
    state = tray.soak_state.load_state(Path("/nonexistent/state.json"))
    status = tray.compute_status(state, [], now=T0)

    assert status.pct == 0.0
    assert status.completed is False
    assert status.icon_name == tray.ICON_RUNNING
    assert status.label.strip() == "0.0%"


def test_a_running_session_with_a_live_daemon_gets_the_running_icon() -> None:
    state = _state()
    samples = [
        {"ts": tray.soak_state._iso(T0 + timedelta(hours=1)), "daemon_up": True, "rss_mib": 50.0}
    ]

    status = tray.compute_status(state, samples, now=T0 + timedelta(hours=6))

    assert status.icon_name == tray.ICON_RUNNING
    assert status.daemon_up is True


def test_the_latest_sample_reporting_the_daemon_down_overrides_the_running_icon() -> None:
    """Only the LATEST sample decides this -- an earlier crash the daemon has
    since recovered from must not leave the tray stuck on a warning icon."""
    state = _state()
    samples = [
        {"ts": tray.soak_state._iso(T0 + timedelta(hours=1)), "daemon_up": False, "rss_mib": 50.0},
        {"ts": tray.soak_state._iso(T0 + timedelta(hours=2)), "daemon_up": True, "rss_mib": 51.0},
    ]

    status = tray.compute_status(state, samples, now=T0 + timedelta(hours=6))

    assert status.daemon_up is True
    assert status.icon_name == tray.ICON_RUNNING


def test_daemon_down_at_the_latest_sample_shows_the_warning_icon() -> None:
    state = _state()
    samples = [
        {"ts": tray.soak_state._iso(T0 + timedelta(hours=1)), "daemon_up": False, "rss_mib": 50.0}
    ]

    status = tray.compute_status(state, samples, now=T0 + timedelta(hours=6))

    assert status.icon_name == tray.ICON_DAEMON_DOWN


def test_no_samples_yet_is_not_treated_as_the_daemon_being_down() -> None:
    """The first sample lands within a minute of open() -- a soak that is
    seconds old with an empty samples.jsonl is healthy, not degraded."""
    state = _state(sessions=[_closed_session(T0, 0.001)])

    status = tray.compute_status(state, [], now=T0)

    assert status.daemon_up is True
    assert status.icon_name == tray.ICON_RUNNING


def test_a_completed_soak_gets_the_complete_icon_even_if_the_last_sample_was_down() -> None:
    """Completion is the more important fact once it is true -- the run is
    over; a stray DOWN sample right before shutdown shouldn't repaint it as a
    live warning."""
    state = _state(completed_utc=tray.soak_state._iso(T0 + timedelta(days=7)))
    samples = [
        {"ts": tray.soak_state._iso(T0 + timedelta(days=7)), "daemon_up": False, "rss_mib": 50.0}
    ]

    status = tray.compute_status(state, samples, now=T0 + timedelta(days=7))

    assert status.completed is True
    assert status.icon_name == tray.ICON_COMPLETE


def test_pct_is_clamped_at_100_past_the_target() -> None:
    long_session = _closed_session(T0, hours=24 * 30)  # 30 days, target is 7
    state = _state(sessions=[long_session])

    status = tray.compute_status(state, [], now=T0 + timedelta(days=30))

    assert status.pct == 100.0


def test_the_tray_text_matches_summarise_verbatim() -> None:
    """The whole point of importing summarise() directly: the popup and the
    tray must render identical prose for identical state, not two hand-rolled
    templates that can quietly drift apart."""
    state = _state()
    samples = [
        {"ts": tray.soak_state._iso(T0 + timedelta(hours=1)), "daemon_up": True, "rss_mib": 50.0}
    ]

    status = tray.compute_status(state, samples, now=T0 + timedelta(hours=6))
    expected = tray.soak_state.summarise(state, samples, T0 + timedelta(hours=6))

    assert status.text == expected


# ------------------------------------------------------------------------------
# read_status
# ------------------------------------------------------------------------------


def test_read_status_never_raises_on_a_missing_state_file(tmp_path: Path) -> None:
    status = tray.read_status(tmp_path / "state.json", tmp_path / "samples.jsonl")

    assert status.completed is False
    assert status.pct == 0.0


def test_read_status_surfaces_a_corrupt_state_file_as_a_visible_error_status(
    tmp_path: Path,
) -> None:
    """A tray that silently falls back to '0% healthy' on a read it could not
    make sense of would repeat the exact B7 mistake this soak exists to catch
    -- a broken monitor that looks identical to a quiet, healthy one."""
    bad_state = tmp_path / "state.json"
    bad_state.write_text("{not valid json")

    status = tray.read_status(bad_state, tmp_path / "samples.jsonl")

    assert status.icon_name == tray.ICON_ERROR
    assert "unreadable" in status.text


# ------------------------------------------------------------------------------
# popup
# ------------------------------------------------------------------------------


def test_format_popup_text_escapes_markup_special_characters() -> None:
    escaped = tray.format_popup_text("RSS <100> & climbing")

    assert escaped == "<tt>RSS &lt;100&gt; &amp; climbing</tt>"


def test_raise_popup_does_nothing_when_zenity_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tray.shutil, "which", lambda _name: None)
    called = False

    def _fail_if_called(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(tray.subprocess, "Popen", _fail_if_called)

    result = tray.raise_popup("some text")

    assert result is False
    assert called is False


def test_raise_popup_invokes_zenity_with_the_formatted_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tray.shutil, "which", lambda _name: "/usr/bin/zenity")
    captured: dict[str, object] = {}

    def _capture(argv: list[str], **kwargs: object) -> None:
        captured["argv"] = argv
        captured["kwargs"] = kwargs

    monkeypatch.setattr(tray.subprocess, "Popen", _capture)

    result = tray.raise_popup("hello")

    assert result is True
    argv = captured["argv"]
    assert argv[0] == "zenity"
    assert any(arg == "--text=<tt>hello</tt>" for arg in argv)
    assert captured["kwargs"] == {"start_new_session": True}


def test_raise_popup_never_lets_a_shell_interpret_the_summary_text() -> None:
    """The summary text embeds live memory numbers and file paths -- Popen
    with a list argv and no shell=True is what keeps that from ever being
    interpreted as shell syntax, however it's formatted."""
    import inspect

    source = inspect.getsource(tray.raise_popup)
    assert "shell=True" not in source
    assert subprocess.list2cmdline  # sanity: real subprocess module in scope
