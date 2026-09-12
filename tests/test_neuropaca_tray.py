# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A1 · the L9 tray's pure logic (`scripts/neuropaca_tray.py`).

Only the half with no `gi` import — `compute_tray_view()`, `menu_header()`,
`thought_lines()`, `request()`'s failure modes. The GTK/AppIndicator glue in
`_run_tray()` is verified live, not here — same split as
`tests/test_soak_tray.py` and its own module docstring.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
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


# ------------------------------------------------------------------------------ compute_tray_view


def test_none_presence_is_asleep_not_a_crash() -> None:
    view = tray.compute_tray_view(None)
    assert view.state == "asleep"
    assert view.icon_name == tray.ICON_ASLEEP
    assert view.error == "daemon unreachable"


def test_not_ok_presence_is_a_reported_error() -> None:
    view = tray.compute_tray_view({"ok": False, "error": "reload failed: boom"})
    assert view.state == "error"
    assert view.error == "reload failed: boom"


def test_each_real_state_maps_to_its_own_icon() -> None:
    for state in ("thinking", "noticed", "focused", "idle", "awake"):
        presence = {
            "ok": True,
            "state": state,
            "thoughts_today": [],
            "last_moment": None,
            "paused_until": None,
        }
        view = tray.compute_tray_view(presence)
        assert view.state == state
        assert view.icon_name == tray._ICON_BY_STATE[state]
        assert view.label == state.capitalize()


def test_thoughts_today_and_last_moment_and_paused_are_carried_through() -> None:
    presence = {
        "ok": True,
        "state": "noticed",
        "thoughts_today": [
            {"text": "why was app:code busy?", "node_id": "idle:a", "at": "x"},
            {"text": "does Brave affect Obsidian?", "node_id": "idle:b", "at": "y"},
        ],
        "last_moment": {"text": "Welcome back.", "evidence": ["app:code"]},
        "paused_until": "2026-09-15T10:00:00+00:00",
    }
    view = tray.compute_tray_view(presence)
    assert view.thoughts_today == ("why was app:code busy?", "does Brave affect Obsidian?")
    assert view.last_moment_text == "Welcome back."
    assert view.paused is True


def test_unknown_state_falls_back_to_the_awake_icon() -> None:
    """Forward-compatible: a future presence state this build does not know
    about yet must still render something sane, never crash the tray."""
    view = tray.compute_tray_view(
        {
            "ok": True,
            "state": "brand_new",
            "thoughts_today": [],
            "last_moment": None,
            "paused_until": None,
        }
    )
    assert view.icon_name == tray.ICON_AWAKE


# ---------------------------------------------------------------- menu_header


def test_menu_header_shows_the_error_when_present() -> None:
    view = tray.compute_tray_view(None)
    assert "daemon unreachable" in tray.menu_header(view)


def test_menu_header_notes_when_paused() -> None:
    presence = {
        "ok": True,
        "state": "awake",
        "thoughts_today": [],
        "last_moment": None,
        "paused_until": "2026-09-15T10:00:00+00:00",
    }
    view = tray.compute_tray_view(presence)
    assert "paused" in tray.menu_header(view)


def test_thought_lines_caps_at_the_limit_keeping_the_newest() -> None:
    presence = {
        "ok": True,
        "state": "awake",
        "thoughts_today": [{"text": f"q{i}", "node_id": f"idle:{i}", "at": "x"} for i in range(10)],
        "last_moment": None,
        "paused_until": None,
    }
    view = tray.compute_tray_view(presence)
    lines = tray.thought_lines(view, limit=3)
    assert lines == ["q7", "q8", "q9"]


# ------------------------------------------------------- mirror_summary_text


def test_mirror_summary_text_none_response_is_unreachable() -> None:
    assert tray.mirror_summary_text(None) == "Couldn't reach the daemon."


def test_mirror_summary_text_not_ok_reports_the_error() -> None:
    resp = {"ok": False, "error": "mirror request timed out"}
    assert tray.mirror_summary_text(resp) == "mirror request timed out"


def test_mirror_summary_text_no_moment_is_nothing_unusual() -> None:
    assert tray.mirror_summary_text({"ok": True, "moment": None}) == "Nothing unusual today."


def test_mirror_summary_text_carries_the_moment_text_through() -> None:
    resp = {"ok": True, "moment": {"text": "You spent unusual time in Spreadsheet today."}}
    assert tray.mirror_summary_text(resp) == "You spent unusual time in Spreadsheet today."


# ------------------------------------------------------------- request() live


def _fake_server(sock_path: Path, response: dict) -> threading.Thread:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)

    def _serve() -> None:
        conn, _ = srv.accept()
        with conn:
            conn.recv(65536)
            conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        srv.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return thread


def test_request_round_trips_over_a_real_unix_socket(tmp_path) -> None:
    sock_path = tmp_path / "np.sock"
    thread = _fake_server(sock_path, {"ok": True, "state": "focused"})
    resp = tray.request(sock_path, {"op": "presence"})
    thread.join(timeout=2.0)
    assert resp == {"ok": True, "state": "focused"}


def test_request_returns_none_when_nothing_is_listening(tmp_path) -> None:
    assert tray.request(tmp_path / "absent.sock", {"op": "presence"}) is None


def test_request_returns_none_on_a_malformed_reply(tmp_path) -> None:
    sock_path = tmp_path / "np.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)

    def _serve() -> None:
        conn, _ = srv.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(b"not json\n")
        srv.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    resp = tray.request(sock_path, {"op": "presence"})
    thread.join(timeout=2.0)
    assert resp is None


def test_default_socket_path_honours_the_env_override(monkeypatch) -> None:
    monkeypatch.setenv("NEUROPACA_SOCKET", "/tmp/custom.sock")
    assert tray.default_socket_path() == Path("/tmp/custom.sock")


def test_default_socket_path_falls_back_to_xdg_runtime_dir(monkeypatch) -> None:
    monkeypatch.delenv("NEUROPACA_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert tray.default_socket_path() == Path("/run/user/1000/neuropaca.sock")
