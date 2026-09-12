# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""V-12 · the one place NeuroPACA reaches the desktop.

L7 never touches the desktop (D-14): it publishes what it wants said, and L9
owns delivery. Until V-12 that delivery was a queue drained only by
`neuropaca notifications`, so nothing the action layer said ever reached a
screen. This is L9's desktop half: a live notification becomes a real popup
through the session's `org.freedesktop.Notifications` server (COSMIC's
`cosmic-notifications` on the target machine).

It spawns the system `notify-send` rather than speaking D-Bus from Python,
because rules.md §9 puts every new runtime dependency behind approval and
`notify-send` ships with every freedesktop desktop. It talks to the session bus
over its unix socket, so zero egress holds (rules.md §6).

`notify()` never raises and never blocks the loop: an absent binary, no bus, a
hung server — each is a `False` return.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil

_log = logging.getLogger(__name__)

APP_NAME = "NeuroPACA"
_TIMEOUT_SECONDS = 5.0
_MAX_SUMMARY_CHARS = 120
_MAX_BODY_CHARS = 400


def notify_send_path() -> str | None:
    """The `notify-send` binary, or `None` on a box without one."""
    return shutil.which("notify-send")


def notify_argv(binary: str, summary: str, body: str) -> list[str]:
    """The exact argv. A list, never a shell string (rules.md §5.4), and `--`
    ends option parsing, so text that starts with `-` — a node named
    `--urgency=critical`, say — is shown, never obeyed."""
    return [
        binary,
        f"--app-name={APP_NAME}",
        "--",
        summary[:_MAX_SUMMARY_CHARS],
        body[:_MAX_BODY_CHARS],
    ]


async def notify(
    summary: str,
    body: str,
    *,
    binary: str | None = None,
    wait_seconds: float = _TIMEOUT_SECONDS,
) -> bool:
    """Show one desktop notification. True only if the server accepted it."""
    exe = binary if binary is not None else notify_send_path()
    if exe is None:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            *notify_argv(exe, summary, body),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        _log.warning("desktop notification could not start %s: %s", exe, exc)
        return False
    try:
        async with asyncio.timeout(wait_seconds):
            return await proc.wait() == 0
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        _log.warning("desktop notification timed out after %.0fs", wait_seconds)
        return False


# gen-ref: 68090d6e
