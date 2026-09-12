# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""F2 · `NotificationDispatcher` (VISION_PHASES.md).

`notify-send` itself is never actually invoked — `asyncio.create_subprocess_exec`
is monkeypatched to a fake process, so these tests exercise the outcome mapping
and the timeout/kill path without touching the real desktop (rules.md §8's
spirit: no real external dependency in a unit test).

Two groups: with `guardian_trust_notification_actions` off (the default —
every outcome reads as `ignored`, confirmed necessary because
`cosmic-notifications` fabricates an "accepted" signal on its own), and with
it on (real stdout parsing — confirmed safe on a verified daemon like
`swaync`, per `interface/notifier.py`'s own docstring).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.models import Event, Moment
from neuropaca.interface.notifier import NotificationDispatcher

_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)


def _moment(*, expires_in_minutes: float = 10.0) -> Moment:
    return Moment(
        kind="welcome_back",
        text="Welcome back.",
        evidence=("app:code",),
        value=1.0,
        context={"focus_bucket": "normal", "hour_bucket": "afternoon", "dismissal_bucket": "0"},
        expires_at=_NOW + timedelta(minutes=expires_in_minutes),
    )


class _FakeProcess:
    def __init__(self, stdout: bytes = b"", *, hang: bool = False) -> None:
        self._stdout = stdout
        self._hang = hang
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(3600.0)
        return self._stdout, b""

    async def wait(self) -> int:
        # A real `kill()` makes any subsequent `wait()` return promptly once
        # the OS reaps the process — the unwrapped `await proc.wait()` in
        # `_show()`'s timeout cleanup relies on exactly that.
        if self._hang and not self.killed:
            await asyncio.sleep(3600.0)
        return -9 if self.killed else 0

    def kill(self) -> None:
        self.killed = True


async def _notifier(
    monkeypatch,
    clock=None,
    fake_process=None,
    which_result="/usr/bin/notify-send",
    trust=False,
):
    bus = EventBus.get_instance()
    await bus.start()
    monkeypatch.setattr("neuropaca.interface.notifier.shutil.which", lambda _name: which_result)
    if fake_process is not None:

        async def _fake_exec(*_argv, **_kwargs):
            if isinstance(fake_process, Exception):
                raise fake_process
            return fake_process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    notifier = NotificationDispatcher(
        bus,
        Config(inference_backend="fake", guardian_trust_notification_actions=trust),
        clock=clock or FakeClock(wall=_NOW),
    )
    await notifier.initialize()
    await notifier.start()
    return notifier, bus


def _collect(sink: list[Event]):
    async def _cb(event: Event) -> None:
        sink.append(event)

    return _cb


async def test_a_normal_close_still_reads_as_ignored(monkeypatch) -> None:
    """The confirmed-safe behaviour: even a clean, fast process exit is never
    read as "accepted" — cosmic-notifications proved that signal fabricated."""
    notifier, bus = await _notifier(monkeypatch, fake_process=_FakeProcess())
    outcome = await notifier._show(_moment())
    assert outcome == "ignored"
    await bus.stop()


async def test_timeout_is_ignored_and_kills_the_process(monkeypatch) -> None:
    proc = _FakeProcess(hang=True)
    notifier, bus = await _notifier(monkeypatch, fake_process=proc)
    outcome = await notifier._show(_moment(expires_in_minutes=1 / 60.0))  # ~1 second
    assert outcome == "ignored"
    assert proc.killed is True
    await bus.stop()


async def test_missing_notify_send_is_ignored_without_spawning(monkeypatch) -> None:
    notifier, bus = await _notifier(monkeypatch, which_result=None)
    outcome = await notifier._show(_moment())
    assert outcome == "ignored"
    await bus.stop()


async def test_already_expired_moment_is_ignored_without_spawning(monkeypatch) -> None:
    notifier, bus = await _notifier(monkeypatch)
    outcome = await notifier._show(_moment(expires_in_minutes=-1.0))
    assert outcome == "ignored"
    await bus.stop()


async def test_subprocess_launch_failure_is_ignored_not_raised(monkeypatch) -> None:
    notifier, bus = await _notifier(monkeypatch, fake_process=OSError("no such file"))
    outcome = await notifier._show(_moment())
    assert outcome == "ignored"
    await bus.stop()


# ---------------------------------------------- trust=True (a verified daemon)


async def test_trusted_keep_click_is_accepted(monkeypatch) -> None:
    notifier, bus = await _notifier(
        monkeypatch, fake_process=_FakeProcess(stdout=b"keep\n"), trust=True
    )
    outcome = await notifier._show(_moment())
    assert outcome == "accepted"
    await bus.stop()


async def test_trusted_dismiss_click_is_dismissed(monkeypatch) -> None:
    notifier, bus = await _notifier(
        monkeypatch, fake_process=_FakeProcess(stdout=b"dismiss\n"), trust=True
    )
    outcome = await notifier._show(_moment())
    assert outcome == "dismissed"
    await bus.stop()


async def test_trusted_close_without_a_labelled_action_is_dismissed(monkeypatch) -> None:
    notifier, bus = await _notifier(monkeypatch, fake_process=_FakeProcess(stdout=b""), trust=True)
    outcome = await notifier._show(_moment())
    assert outcome == "dismissed"
    await bus.stop()


async def test_trusted_timeout_is_still_ignored(monkeypatch) -> None:
    proc = _FakeProcess(hang=True)
    notifier, bus = await _notifier(monkeypatch, fake_process=proc, trust=True)
    outcome = await notifier._show(_moment(expires_in_minutes=1 / 60.0))
    assert outcome == "ignored"
    assert proc.killed is True
    await bus.stop()


async def test_end_to_end_publishes_moment_feedback(monkeypatch) -> None:
    notifier, bus = await _notifier(monkeypatch, fake_process=_FakeProcess())
    feedback: list[Event] = []
    bus.subscribe(EventType.MOMENT_FEEDBACK, _collect(feedback))

    moment = _moment()
    await notifier.on_moment_delivered(
        Event(event_type=EventType.MOMENT_DELIVERED, payload={"moment": moment})
    )
    assert len(notifier._tasks) == 1
    await asyncio.gather(*notifier._tasks)
    await bus.join()

    assert len(feedback) == 1
    assert feedback[0].payload["outcome"] == "ignored"
    assert feedback[0].payload["moment"] is moment
    await bus.stop()
