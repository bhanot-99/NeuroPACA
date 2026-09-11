# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""V-12 · no action has ever actually reached the user (VISION.md §5).

The audit log holds 24 actions, every one a dry-run `memory_write`; there has
never been a single notification attempt. It is a chain of gates, and V-12
found that the last link did not exist at all: L7 publishes a notification
*intent* (D-14), L9 queued it for `neuropaca notifications` — pull-only, a
terminal the user has to think to run — and nothing ever reached a screen.

L9 now also hands a **live** intent to the desktop (`interface/desktop.py`, the
system `notify-send`). A dry-run intent never is: a popup saying "would have
told you" is an effect, and dry-run causes none. So with the shipped
`action_dry_run = True` nothing changes on screen — going live stays the user's
decision, which B7's review period requires.
"""

from __future__ import annotations

import asyncio

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.interface import desktop
from neuropaca.interface.layer import InterfaceLayer


def _stand_in(tmp_path, body: str) -> str:
    """A fake `notify-send`: the real subprocess path, no real desktop."""
    exe = tmp_path / "fake-notify-send"
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(0o755)
    return str(exe)


# ================================ 1 · desktop.notify — the real spawn, safely


async def test_text_goes_in_as_argv_after_an_end_of_options_marker(tmp_path) -> None:
    """rules.md §5.4: never a shell string. And `--` means a node named
    `--urgency=critical` is shown, never obeyed as a flag."""
    out = tmp_path / "argv"
    exe = _stand_in(tmp_path, f'for a in "$@"; do printf "%s\\n" "$a"; done > "{out}"\n')

    assert await desktop.notify("NeuroPACA", "--urgency=critical is a node", binary=exe)

    assert out.read_text().splitlines() == [
        "--app-name=NeuroPACA",
        "--",
        "NeuroPACA",
        "--urgency=critical is a node",
    ]


async def test_a_refusing_server_is_reported(tmp_path) -> None:
    assert await desktop.notify("s", "b", binary=_stand_in(tmp_path, "exit 1\n")) is False


async def test_a_hung_server_is_timed_out_not_waited_on(tmp_path) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    exe = _stand_in(tmp_path, "sleep 30\n")
    assert await desktop.notify("s", "b", binary=exe, wait_seconds=0.2) is False
    assert loop.time() - started < 5


async def test_no_notify_send_means_no_notification() -> None:
    assert await desktop.notify("s", "b") is False  # conftest: none on this box


async def test_an_unrunnable_binary_is_survived(tmp_path) -> None:
    assert await desktop.notify("s", "b", binary=str(tmp_path / "missing")) is False


def test_text_is_bounded() -> None:
    argv = desktop.notify_argv("x", "s" * 1000, "b" * 5000)
    assert len(argv[3]) <= 120 and len(argv[4]) <= 400


# ============================ 2 · L9: live intents go out, dry-run ones never


async def _layer(tmp_path, **cfg):
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await gm.load()
    clock = FakeClock()
    layer = InterfaceLayer(
        EventBus.get_instance(),
        Config(inference_backend="fake", **cfg),
        gm,
        BitNetRuntime.get_instance(),
        clock=clock,
        socket_path=str(tmp_path / "l9.sock"),
    )
    return layer, gm, clock


def _recorder(monkeypatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    async def fake_notify(summary: str, body: str, **_kw) -> bool:
        sent.append((summary, body))
        return True

    monkeypatch.setattr(desktop, "notify", fake_notify)
    return sent


def _intent(text: str, *, dry_run: bool, ok: bool = True, node_ids=()) -> Event:
    return Event(
        event_type=EventType.ACTION_TRIGGERED,
        source="action",
        payload={
            "ok": ok,
            "dry_run": dry_run,
            "intent": {
                "kind": "notification",
                "reason": "r",
                "text": text,
                "node_ids": list(node_ids),
            },
        },
    )


async def _settle(layer: InterfaceLayer) -> None:
    if layer._desktop_task is not None:
        await layer._desktop_task


async def test_a_live_notification_reaches_the_desktop(tmp_path, monkeypatch) -> None:
    sent = _recorder(monkeypatch)
    layer, _, _ = await _layer(tmp_path)
    await layer.on_action_triggered(_intent("webpack is hot", dry_run=False))
    await _settle(layer)
    assert sent == [("NeuroPACA", "webpack is hot")]
    assert layer._desktop_sent == 1


async def test_a_dry_run_notification_never_reaches_the_desktop(tmp_path, monkeypatch) -> None:
    sent = _recorder(monkeypatch)
    layer, _, _ = await _layer(tmp_path)
    await layer.on_action_triggered(_intent("would have said", dry_run=True))
    await _settle(layer)
    assert sent == []
    assert layer._pending_notifications[0]["dry_run"] is True  # terminal queue, flagged


async def test_a_failed_action_is_not_announced(tmp_path, monkeypatch) -> None:
    sent = _recorder(monkeypatch)
    layer, _, _ = await _layer(tmp_path)
    await layer.on_action_triggered(_intent("x", dry_run=False, ok=False))
    await _settle(layer)
    assert sent == []


async def test_the_switch_keeps_notifications_terminal_only(tmp_path, monkeypatch) -> None:
    sent = _recorder(monkeypatch)
    layer, _, _ = await _layer(tmp_path, notify_desktop=False)
    await layer.on_action_triggered(_intent("x", dry_run=False))
    await _settle(layer)
    assert sent == []
    assert layer._pending_notifications  # still readable in the terminal


async def test_a_burst_is_rate_limited_and_nothing_is_lost(tmp_path, monkeypatch) -> None:
    sent = _recorder(monkeypatch)
    layer, _, clock = await _layer(tmp_path)
    for i in range(5):
        await layer.on_action_triggered(_intent(f"n{i}", dry_run=False))
        await _settle(layer)
    assert len(sent) == 1  # one popup for the burst
    assert len(layer._pending_notifications) == 5  # every one kept for the terminal

    await clock.advance(31)
    await layer.on_action_triggered(_intent("later", dry_run=False))
    await _settle(layer)
    assert len(sent) == 2


async def test_the_popup_names_nodes_instead_of_raw_ids(tmp_path, monkeypatch) -> None:
    """B18 took raw ids out of every label; a popup must not put them back."""
    sent = _recorder(monkeypatch)
    layer, gm, _ = await _layer(tmp_path)
    await gm.add_node("app:cosmic-term", NodeType.APP, {"label": "cosmic-term"})
    await layer.on_action_triggered(
        _intent(
            "app:cosmic-term is under corroborated pressure",
            dry_run=False,
            node_ids=["app:cosmic-term"],
        )
    )
    await _settle(layer)
    body = sent[0][1]
    assert "app:cosmic-term" not in body
    assert body == f"{gm.display_name('app:cosmic-term')} is under corroborated pressure"


async def test_a_delivery_crash_is_logged_not_raised(tmp_path, monkeypatch) -> None:
    async def boom(*_a, **_k) -> bool:
        raise RuntimeError("bus exploded")

    monkeypatch.setattr(desktop, "notify", boom)
    layer, _, _ = await _layer(tmp_path)
    await layer.on_action_triggered(_intent("x", dry_run=False))
    await _settle(layer)
    assert layer._desktop_failed == 1
