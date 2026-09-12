# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""V-11 · "the Wayland sensor still crashes the orchestrator" (VISION.md §5).

Read against the daemon's own log, the premise does not hold. At
2026-09-10T19:14:23 the pump logged `Failed to read events` at ERROR — so the
daemon was not yet shutting down — then, in the same second, `orchestrator
stopped`. Nothing but SIGTERM/SIGINT can stop the orchestrator, and the pump
had already done its job ("will reconnect"). Both lines are consequences of one
event: the graphical session went away, taking the compositor's socket with it,
and `PartOf=graphical-session.target` stopped the daemon.

Two real defects sat behind it, and both are fixed here:

* **The daemon did not come back.** The next start was 21:53:43 — 2 h 39 min
  down — because the live unit was installed but *disabled*, so `WantedBy=`
  never pulled it in when the session returned, and a clean SIGTERM exit is not
  a failure `Restart=` acts on. Enabling it is a one-command change to how the
  machine boots, so it is left to the user; `neuropaca doctor` now flags it.
* **The pump gave up.** After ~62 s of failed reconnects it returned for good,
  leaving the sensor deaf for the daemon's life. It now retries forever at a slow
  pace (tests in test_wayland_conn.py).
"""

from __future__ import annotations

import pytest

from neuropaca.core.config import Config
from neuropaca.interface import offline


def _stub_state(monkeypatch, state: str | None) -> None:
    monkeypatch.setattr(offline, "_systemctl_is_enabled", lambda _unit: state)


# ======================================================== 1 · the service check


@pytest.mark.parametrize("state", ["enabled", "enabled-runtime", "linked"])
def test_an_enabled_service_is_not_a_problem(monkeypatch, state: str) -> None:
    _stub_state(monkeypatch, state)
    rows, problems = offline._service_report()
    assert problems == []
    assert rows and state in rows[0][1]


@pytest.mark.parametrize("state", ["disabled", "masked"])
def test_a_service_that_will_not_return_is_a_problem_with_its_fix(monkeypatch, state) -> None:
    _stub_state(monkeypatch, state)
    rows, problems = offline._service_report()
    assert len(problems) == 1
    assert "systemctl --user enable neuropacad.service" in rows[0][1]


def test_an_uninstalled_service_is_reported_not_flagged(monkeypatch) -> None:
    """A dev checkout run by hand has no unit; that is not a fault."""
    _stub_state(
        monkeypatch,
        "Failed to get unit file state for neuropacad.service: No such file or directory",
    )
    rows, problems = offline._service_report()
    assert problems == []
    assert "not installed" in rows[0][1]


def test_no_user_manager_means_no_row_at_all(monkeypatch) -> None:
    _stub_state(monkeypatch, None)
    assert offline._service_report() == ([], [])


# ================================================ 2 · asking systemctl safely


def test_a_missing_systemctl_binary_is_survived(monkeypatch) -> None:
    def missing(*_a, **_k):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(offline.subprocess, "run", missing)
    assert offline._systemctl_is_enabled("neuropacad.service") is None


def test_a_wedged_user_bus_times_out_instead_of_hanging_doctor(monkeypatch) -> None:
    def wedged(*_a, **_k):
        raise offline.subprocess.TimeoutExpired(cmd="systemctl", timeout=3)

    monkeypatch.setattr(offline.subprocess, "run", wedged)
    assert offline._systemctl_is_enabled("neuropacad.service") is None


def test_systemctl_is_asked_read_only_and_bounded(monkeypatch) -> None:
    seen: dict = {}

    class _Done:
        stdout, stderr = "disabled\n", ""

    def spy(argv, **kw):
        seen.update(argv=argv, **kw)
        return _Done()

    monkeypatch.setattr(offline.subprocess, "run", spy)
    assert offline._systemctl_is_enabled("neuropacad.service") == "disabled"
    assert seen["argv"] == ["systemctl", "--user", "is-enabled", "neuropacad.service"]
    assert seen["timeout"] <= 5 and seen["check"] is False


# =========================================================== 3 · through doctor


def _offline_config(monkeypatch, tmp_path) -> None:
    cfg = Config(
        inference_backend="fake",
        graph_db_path=str(tmp_path / "g.json"),
        log_file_path=str(tmp_path / "n.log"),
        action_log_path=str(tmp_path / "a.log"),
        quarantine_path=str(tmp_path / "q"),
    )
    monkeypatch.setattr(offline, "_load_config", lambda: (cfg, None))
    monkeypatch.setattr(offline, "_socket_path", lambda: str(tmp_path / "none.sock"))


def test_doctor_fails_when_the_service_will_not_return(monkeypatch, tmp_path, capsys) -> None:
    _offline_config(monkeypatch, tmp_path)
    _stub_state(monkeypatch, "disabled")
    assert offline.doctor([]) == 1
    assert "enable" in capsys.readouterr().out


def test_doctor_passes_when_the_service_is_enabled(monkeypatch, tmp_path) -> None:
    _offline_config(monkeypatch, tmp_path)
    _stub_state(monkeypatch, "enabled")
    assert offline.doctor([]) == 0


# gen-ref: 930bc1eb
