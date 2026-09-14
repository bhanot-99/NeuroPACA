# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.3 · `interface/activation.py` (VoiceActivationModule).

`_handle_trigger()` is tested directly, never through `_poll_trigger_file()`'s
sleep loop (rules.md §8: "no test sleeps") — the loop is a trivial "call this
repeatedly forever" wrapper with nothing of its own to verify.
"""

from __future__ import annotations

import subprocess

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event
from neuropaca.interface.activation import VoiceActivationModule, probe_global_shortcuts_portal


def test_probe_returns_false_when_gdbus_is_missing(monkeypatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("no gdbus")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert probe_global_shortcuts_portal() is False


def test_probe_returns_false_on_timeout(monkeypatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="gdbus", timeout=5.0)

    monkeypatch.setattr(subprocess, "run", _raise)
    assert probe_global_shortcuts_portal() is False


def test_probe_returns_true_only_when_the_interface_is_listed(monkeypatch) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="interface org.freedesktop.portal.GlobalShortcuts {"
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert probe_global_shortcuts_portal() is True


def test_probe_returns_false_when_the_interface_is_absent(monkeypatch) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="interface org.freedesktop.portal.Notification {"
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert probe_global_shortcuts_portal() is False


def _module() -> VoiceActivationModule:
    return VoiceActivationModule(EventBus(), Config(inference_backend="fake"))


async def test_first_trigger_publishes_started() -> None:
    module = _module()
    await module.event_bus.start()
    published: list[Event] = []
    module.event_bus.subscribe(EventType.VOICE_PTT_STARTED, published.append)
    module._handle_trigger({"seq": 1})
    await module.event_bus.join()
    assert len(published) == 1
    assert published[0].event_type is EventType.VOICE_PTT_STARTED
    await module.event_bus.stop()


async def test_second_trigger_toggles_to_stopped() -> None:
    module = _module()
    await module.event_bus.start()
    started: list[Event] = []
    stopped: list[Event] = []
    module.event_bus.subscribe(EventType.VOICE_PTT_STARTED, started.append)
    module.event_bus.subscribe(EventType.VOICE_PTT_STOPPED, stopped.append)
    module._handle_trigger({"seq": 1})
    module._handle_trigger({"seq": 2})
    await module.event_bus.join()
    assert len(started) == 1
    assert len(stopped) == 1
    await module.event_bus.stop()


async def test_a_repeated_seq_is_ignored() -> None:
    module = _module()
    await module.event_bus.start()
    published: list[Event] = []
    module.event_bus.subscribe(EventType.VOICE_PTT_STARTED, published.append)
    module._handle_trigger({"seq": 1})
    module._handle_trigger({"seq": 1})
    await module.event_bus.join()
    assert len(published) == 1
    await module.event_bus.stop()


def test_a_malformed_trigger_is_ignored() -> None:
    module = _module()
    published: list[Event] = []
    module.event_bus.subscribe(EventType.VOICE_PTT_STARTED, published.append)
    module._handle_trigger({"seq": "not-an-int"})
    module._handle_trigger(None)
    assert published == []


async def test_health_reports_hotkey_availability() -> None:
    cfg = Config(inference_backend="fake", voice_activation_mode="hotkey")
    module = VoiceActivationModule(EventBus(), cfg)
    module._hotkey_available = False
    module.is_running = True
    health = module.health()
    assert "unavailable" in health.detail


async def test_health_reports_the_trigger_path_in_tray_mode() -> None:
    cfg = Config(
        inference_backend="fake",
        voice_activation_mode="tray",
        voice_ptt_trigger_path="data/voice_ptt_trigger.json",
    )
    module = VoiceActivationModule(EventBus(), cfg)
    module.is_running = True
    health = module.health()
    assert "voice_ptt_trigger.json" in health.detail


# gen-ref: 2e38118d
