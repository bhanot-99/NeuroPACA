# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L7 · the sandbox — the only place in NeuroPACA that writes outside the graph
or starts a process (Architecture.md §11b, rules.md §5, D-14).

Two guarantees, both enforced here rather than in each action:

**Path containment.** A write target is fully resolved (symlinks included) and
must land under one of the allowed roots. Resolution happens *before* the
containment test, so `~/notes/../../etc/passwd` and a symlink pointing out of an
allowed root are both refused, not followed.

**Process isolation.** `asyncio.create_subprocess_exec(*argv)` — never
`create_subprocess_shell`, never `shell=True`, never a string command. There is
no shell, so there is nothing to quote-escape and nothing to inject
(rules.md §5.4). The child gets `env={}` — no `PATH`, no `HOME`, no API tokens
inherited from the daemon — its own session (so a timeout kills the whole
process group, not just the leader), no stdin, and a hard wall-clock timeout.

`argv[0]` is resolved to a real executable file here, once, because with `env={}`
the child has no `PATH` to resolve a bare name against.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from neuropaca.core.errors import SafetyGateError

_log = logging.getLogger(__name__)

_MAX_ARGV = 64
_MAX_ARG_LEN = 4096
_MAX_CAPTURE = 8192  # bytes of stdout/stderr kept for the audit line


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Sandbox:
    """Path containment + process isolation. Holds no state beyond its roots."""

    def __init__(self, allowed_roots: Sequence[str | Path]) -> None:
        roots: list[Path] = []
        for raw in allowed_roots:
            root = Path(raw).expanduser()
            with contextlib.suppress(OSError):
                root = root.resolve()
            if root not in roots:
                roots.append(root)
        self._roots: tuple[Path, ...] = tuple(roots)

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return self._roots

    # ------------------------------------------------------------------- paths
    def resolve_write_path(self, path: str | Path) -> Path:
        """Resolve `path` and prove it is inside an allowed root, or refuse.

        Fails closed: with no roots configured, no path is writable at all.
        """
        if not self._roots:
            raise SafetyGateError(
                "no writable roots are configured — set `watch_paths` before enabling file actions"
            )
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise SafetyGateError(f"write path must be absolute: {path!r}")
        # `strict=False`: the file itself may not exist yet, but every existing
        # component of its path — and any symlink among them — is resolved.
        resolved = candidate.resolve()
        for root in self._roots:
            if resolved == root or resolved.is_relative_to(root):
                return resolved
        raise SafetyGateError(
            f"write path escapes the sandbox: {resolved} is not under "
            f"{[str(r) for r in self._roots]}"
        )

    def contains(self, path: str | Path) -> bool:
        try:
            self.resolve_write_path(path)
        except SafetyGateError:
            return False
        return True

    # ---------------------------------------------------------------- commands
    @staticmethod
    def validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
        """Structural checks + executable resolution. Never builds a string."""
        if not argv:
            raise SafetyGateError("empty command")
        if len(argv) > _MAX_ARGV:
            raise SafetyGateError(f"command has {len(argv)} arguments, max {_MAX_ARGV}")
        for arg in argv:
            if not isinstance(arg, str):
                raise SafetyGateError(f"command arguments must be strings, got {type(arg)!r}")
            if "\x00" in arg:
                raise SafetyGateError("command argument contains a NUL byte")
            if len(arg) > _MAX_ARG_LEN:
                raise SafetyGateError(f"command argument longer than {_MAX_ARG_LEN} chars")

        head = argv[0]
        located = head if os.path.isabs(head) else shutil.which(head)
        if located is None:
            raise SafetyGateError(f"executable not found: {head!r}")
        exe = Path(located)
        if not exe.is_file() or not os.access(exe, os.X_OK):
            raise SafetyGateError(f"not an executable file: {exe}")
        return (str(exe), *argv[1:])

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        cwd: str | Path | None = None,
        gui: bool = False,
    ) -> CommandOutcome:
        """Run a validated argv with no shell, no environment (or a fixed GUI
        display allowlist if `gui=True`), and a hard timeout.

        `gui=True` (A6.2's `OpenAppAction`): a launched GUI app needs
        `DISPLAY`/`WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR`/etc. to actually show a
        window — the default `env={}` strips those along with everything
        else. Also skips waiting for the process to exit: after a short
        launch-check window, a still-running process is reported as a
        successful, detached launch (no `stdout`/`stderr` captured for it —
        nothing reads a GUI app's output), and `timeout_seconds` never kills
        it the way it does the non-gui path."""
        resolved = self.validate_argv(argv)
        workdir = str(cwd) if cwd is not None else None
        _log.info("L7 sandbox exec %s (gui=%s)", resolved[0], gui)
        env: dict[str, str] = {}
        if gui:
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                in (
                    "DISPLAY",
                    "WAYLAND_DISPLAY",
                    "XDG_RUNTIME_DIR",
                    "XDG_SESSION_TYPE",
                    "XDG_CURRENT_DESKTOP",
                    "HOME",
                    "PATH",
                    "DBUS_SESSION_BUS_ADDRESS",
                )
            }
        proc = await asyncio.create_subprocess_exec(
            *resolved,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=workdir,
            start_new_session=True,
        )
        if gui:
            try:
                await asyncio.wait_for(proc.wait(), 0.15)
                if proc.returncode != 0:
                    out, err = await proc.communicate()
                    return CommandOutcome(
                        argv=resolved,
                        returncode=proc.returncode if proc.returncode is not None else -1,
                        stdout=out.decode("utf-8", "replace")[:_MAX_CAPTURE],
                        stderr=err.decode("utf-8", "replace")[:_MAX_CAPTURE],
                    )
            except TimeoutError:
                # App is running detached and healthy after initial launch window
                pass
            return CommandOutcome(argv=resolved, returncode=0, stdout="", stderr="")

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_seconds)
        except TimeoutError:
            await self._kill(proc)
            raise SafetyGateError(
                f"command exceeded its {timeout_seconds}s budget and was killed: {resolved[0]}"
            ) from None
        return CommandOutcome(
            argv=resolved,
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode("utf-8", "replace")[:_MAX_CAPTURE],
            stderr=err.decode("utf-8", "replace")[:_MAX_CAPTURE],
        )

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        """Kill the child's whole session — `start_new_session=True` made it a
        process-group leader precisely so a runaway cannot outlive its parent."""
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5.0)


# gen-ref: 9e6c1e3d
