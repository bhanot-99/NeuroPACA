# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A1 · the presence tray's pure logic (`scripts/neuropaca_tray.py`).

Only the half with no `gi` import — `read_health()`, `is_stale()`,
`compute_tray_view()`, `menu_header()`. The GTK/AppIndicator glue in
`_run_tray()` is verified live, not here — same split as
`tests/test_soak_tray.py` and its own module docstring.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "neuropaca_tray.py"
_spec = importlib.util.spec_from_file_location("neuropaca_tray", _MODULE_PATH)
assert _spec and _spec.loader
tray = importlib.util.module_from_spec(_spec)
# Registered before exec: `TrayView` is a slotted dataclass, and `dataclasses`
# resolves annotations through `sys.modules[cls.__module__]` — skip this and
# it dies with "'NoneType' object has no attribute '__dict__'". Same fix
# `tests/test_soak_tray.py` already applies for the same reason.
sys.modules["neuropaca_tray"] = tray
_spec.loader.exec_module(tray)


def _health(*, presence_detail: str | None = None, ok: bool = True) -> dict:
    modules = []
    if presence_detail is not None:
        modules.append({"name": "presence", "ok": True, "detail": presence_detail})
    return {"ok": ok, "modules": modules}


# --------------------------------------------------------------------- read_health


def test_read_health_returns_none_when_the_file_is_absent(tmp_path: Path) -> None:
    assert tray.read_health(tmp_path / "absent.json") is None


def test_read_health_returns_none_on_malformed_json(tmp_path: Path) -> None:
    bad = tmp_path / "health.json"
    bad.write_text("not json", encoding="utf-8")
    assert tray.read_health(bad) is None


def test_read_health_returns_none_when_the_top_level_is_not_an_object(tmp_path: Path) -> None:
    arr = tmp_path / "health.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    assert tray.read_health(arr) is None


def test_read_health_reads_a_real_dump(tmp_path: Path) -> None:
    dump = tmp_path / "health.json"
    dump.write_text(json.dumps(_health(presence_detail="state=idle since=x")), encoding="utf-8")
    health = tray.read_health(dump)
    assert health is not None
    assert health["ok"] is True


# ------------------------------------------------------------------------ is_stale


def test_is_stale_true_past_the_threshold() -> None:
    assert tray.is_stale(mtime=0.0, now=1000.0, max_age_seconds=90.0) is True


def test_is_stale_false_within_the_threshold() -> None:
    assert tray.is_stale(mtime=1000.0, now=1010.0, max_age_seconds=90.0) is False


def test_is_stale_uses_the_default_threshold_when_unset() -> None:
    assert tray.is_stale(mtime=0.0, now=tray.STALE_AFTER_SECONDS + 1.0) is True
    assert tray.is_stale(mtime=0.0, now=tray.STALE_AFTER_SECONDS - 1.0) is False


# ------------------------------------------------------------------ compute_tray_view


def test_none_health_is_asleep_not_a_crash() -> None:
    view = tray.compute_tray_view(None, stale=False)
    assert view.state == "asleep"
    assert view.error == "daemon unreachable"


def test_stale_health_is_asleep_with_its_own_reason() -> None:
    view = tray.compute_tray_view(_health(presence_detail="state=idle since=x"), stale=True)
    assert view.state == "asleep"
    assert view.error == "health dump is stale"


def test_not_ok_health_is_a_reported_error() -> None:
    view = tray.compute_tray_view(_health(ok=False), stale=False)
    assert view.state == "error"
    assert view.error is not None


def test_missing_presence_module_is_unknown_not_a_crash() -> None:
    view = tray.compute_tray_view(_health(), stale=False)
    assert view.state == "unknown"
    assert view.error is None


def test_each_real_state_maps_to_its_own_icon() -> None:
    for state, icon in [
        ("thinking", tray.ICON_THINKING),
        ("noticed", tray.ICON_NOTICED),
        ("focused", tray.ICON_FOCUSED),
        ("idle", tray.ICON_IDLE),
        ("awake", tray.ICON_AWAKE),
    ]:
        view = tray.compute_tray_view(
            _health(presence_detail=f"state={state} since=2026-09-12T10:00:00+05:30"),
            stale=False,
        )
        assert view.state == state
        assert view.icon_name == icon
        assert view.label == state.capitalize()


def test_since_is_carried_through() -> None:
    view = tray.compute_tray_view(
        _health(presence_detail="state=focused since=2026-09-12T10:00:00+05:30"), stale=False
    )
    assert view.since == "2026-09-12T10:00:00+05:30"


def test_unknown_state_falls_back_to_the_awake_icon() -> None:
    view = tray.compute_tray_view(_health(presence_detail="state=bogus since=x"), stale=False)
    assert view.icon_name == tray.ICON_AWAKE


# ---------------------------------------------------------------------- menu_header


def test_menu_header_shows_the_error_when_present() -> None:
    view = tray.compute_tray_view(None, stale=False)
    assert "daemon unreachable" in tray.menu_header(view)


def test_menu_header_is_plain_when_healthy() -> None:
    view = tray.compute_tray_view(
        _health(presence_detail="state=idle since=x"), stale=False
    )
    header = tray.menu_header(view)
    assert "Idle" in header
    assert "(" not in header


# --------------------------------------------------------------- default_health_dump_path


def test_default_health_dump_path_honours_the_env_override(monkeypatch) -> None:
    monkeypatch.setenv("NEUROPACA_HEALTH_DUMP", "/tmp/custom-health.json")
    assert tray.default_health_dump_path() == Path("/tmp/custom-health.json")


def test_default_health_dump_path_falls_back_to_the_repo_data_dir(monkeypatch) -> None:
    monkeypatch.delenv("NEUROPACA_HEALTH_DUMP", raising=False)
    assert tray.default_health_dump_path() == tray.REPO / "data" / "health.json"
