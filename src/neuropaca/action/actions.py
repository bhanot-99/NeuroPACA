# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L7 · the concrete actions (Architecture.md §11b, B7, D-14).

Four ship in B7, in ascending order of what they can break:

| action | tier | effect | reversible by |
| --- | --- | --- | --- |
| `NotificationAction` | safe | publishes an *intent*; L9 delivers it | nothing to undo |
| `MemoryWriteAction` | safe | one graph node (+ edges) | deleting the node it created |
| `FileWriteAction` | dangerous | one file, backed up first | restoring the quarantined copy |
| `RunCommandAction` | dangerous | one child process, no shell/env | nothing — hence confirmation |

A6.2 adds three more. All three run a process, which is what first put them
at `dangerous` under the same rule as `RunCommandAction` — a fixed, verified
argv shape narrows *what* can run, not whether it's a process. User decision
2026-09-14, after that classification made every voice "open X"/volume/
brightness command require a spoken confirmation: reclassified to `safe`.
The operator's own reasoning — kept here because it's the actual boundary,
not "processes are safe now" — is that unlike `RunCommandAction` (an
arbitrary argv) or `FileWriteAction` (persistent data loss/corruption if
wrong), all three are drawn from closed, narrow, fully-reversible option
sets: `OpenAppAction` can only launch an app already in the verified
installed-apps registry (never an arbitrary string), and volume/brightness
only ever nudge a percentage up or down. Worst case from a misheard command
is an unwanted window or a wrong slider position — annoying, not damaging,
and trivially undone by hand. Writing or running arbitrary things stays
gated; opening/adjusting from a closed, verified set does not.

| action | tier | effect | reversible by |
| --- | --- | --- | --- |
| `OpenAppAction` | safe | launches a verified installed app | closing the app |
| `AdjustVolumeAction` | safe | one wpctl/pactl call | adjusting it back |
| `AdjustBrightnessAction` | safe | one brightnessctl call | adjusting it back |

`ApiCallAction` is deliberately **not** built. It is the only component that
would ever be allowed an outbound socket (rules.md §5.5), the system's whole
premise is zero egress (rules.md §6), and `problems.md` 1.9 says to leave the
riskiest reconstructed pieces for last. `Config.api_call_enabled` /
`api_allowlist` are the reserved switches; with no class behind them, the daemon
has no code path to a network socket at all.

`NotificationAction` is the load-bearing decoupling (D-14): L7 never touches
`notify-send`, D-Bus, or a terminal. It publishes what it wants said, L9 owns
delivery. That keeps the desktop out of the action layer, keeps L7 testable
headless, and means a notification cannot fail because nobody is logged in.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import tempfile
from abc import abstractmethod
from pathlib import Path
from typing import Any

from neuropaca.action.base import ActionTier, BaseAction
from neuropaca.action.quarantine import Quarantine
from neuropaca.action.sandbox import Sandbox
from neuropaca.core.enums import NodeType, RelationType
from neuropaca.core.errors import SafetyGateError
from neuropaca.core.graph_memory import GraphMemory

_log = logging.getLogger(__name__)

_MAX_NOTIFICATION_CHARS = 400
_MAX_FILE_BYTES = 1 << 20  # 1 MiB — an autonomous write is a note, not a dataset


class NotificationAction(BaseAction):
    """Say something to the human. The saying is L9's job."""

    name = "notification"
    tier = ActionTier.SAFE

    def __init__(self, *, reason: str, text: str, node_ids: tuple[str, ...] = ()) -> None:
        super().__init__(reason=reason)
        self.text = text.strip()
        self.node_ids = node_ids

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.name,
            "reason": self.reason,
            "text": self.text,
            "node_ids": list(self.node_ids),
        }

    async def validate(self) -> None:
        if not self.text:
            raise SafetyGateError("empty notification")
        if len(self.text) > _MAX_NOTIFICATION_CHARS:
            raise SafetyGateError(
                f"notification is {len(self.text)} chars, max {_MAX_NOTIFICATION_CHARS}"
            )

    async def dry_run(self) -> str:
        return f"tell the user: {self.text}"

    async def execute(self) -> str:
        # Nothing happens here on purpose: the effect *is* the ACTION_TRIGGERED
        # intent the gate publishes, which L9 turns into a line in the terminal.
        return f"notified: {self.text}"

    async def rollback(self) -> bool:
        return False  # you cannot unsay a thing


class MemoryWriteAction(BaseAction):
    """Write one node, and optionally edges from it, into the graph."""

    name = "memory_write"
    tier = ActionTier.SAFE

    def __init__(
        self,
        graph_memory: GraphMemory,
        *,
        reason: str,
        node_id: str,
        node_type: NodeType,
        attributes: dict[str, Any] | None = None,
        edges: tuple[tuple[str, RelationType], ...] = (),
    ) -> None:
        super().__init__(reason=reason)
        self._graph = graph_memory
        self.node_id = node_id
        self.node_type = node_type
        self.attributes = dict(attributes or {})
        self.edges = edges
        self._created = False

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.name,
            "reason": self.reason,
            "node_id": self.node_id,
            "node_type": self.node_type.value,
            "edges": [[target, relation.value] for target, relation in self.edges],
        }

    async def validate(self) -> None:
        if not self.node_id:
            raise SafetyGateError("memory write needs a node id")
        if not isinstance(self.node_type, NodeType):
            raise SafetyGateError(f"not a NodeType: {self.node_type!r}")
        for target, relation in self.edges:
            if not target:
                raise SafetyGateError("edge target must be a node id")
            if not isinstance(relation, RelationType):
                raise SafetyGateError(f"not a RelationType: {relation!r}")

    async def dry_run(self) -> str:
        return f"write {self.node_type.value} node {self.node_id} with {len(self.edges)} edge(s)"

    async def execute(self) -> str:
        self._created = self._graph.get_node(self.node_id) is None
        await self._graph.upsert_node(self.node_id, self.node_type, self.attributes)
        for target, relation in self.edges:
            if self._graph.get_node(target) is not None:
                await self._graph.add_edge(self.node_id, target, relation)
        return f"wrote {self.node_id}"

    async def rollback(self) -> bool:
        """Only undo what this action created — never delete a node that already
        existed and that we merely updated."""
        if not self._created:
            return False
        await self._graph.delete_node(self.node_id)
        return True


class FileWriteAction(BaseAction):
    """Write one file inside the sandbox, after its current bytes are preserved."""

    name = "file_write"
    tier = ActionTier.DANGEROUS

    def __init__(
        self,
        sandbox: Sandbox,
        quarantine: Quarantine,
        *,
        reason: str,
        path: str | Path,
        content: str,
    ) -> None:
        super().__init__(reason=reason)
        self._sandbox = sandbox
        self._quarantine = quarantine
        self.path = Path(path)
        self.content = content
        self._resolved: Path | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.name,
            "reason": self.reason,
            "path": str(self.path),
            "bytes": len(self.content.encode("utf-8")),
        }

    def backup_targets(self) -> tuple[Path, ...]:
        """The gate quarantines this before `execute()` — `rollback()` restores it."""
        target = self._resolved or self.path
        return (target,) if target.exists() else ()

    async def validate(self) -> None:
        size = len(self.content.encode("utf-8"))
        if size > _MAX_FILE_BYTES:
            raise SafetyGateError(f"refusing a {size}-byte write, max {_MAX_FILE_BYTES}")
        resolved = self._sandbox.resolve_write_path(self.path)
        if resolved.is_dir():
            raise SafetyGateError(f"write target is a directory: {resolved}")
        self._resolved = resolved

    async def dry_run(self) -> str:
        target = self._resolved or self.path
        verb = "overwrite" if target.exists() else "create"
        return f"{verb} {target} ({len(self.content.encode('utf-8'))} bytes)"

    async def execute(self) -> str:
        if self._resolved is None:  # never reachable via the gate, which validates first
            raise SafetyGateError("file write executed without validation")
        target = self._resolved
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a crash mid-write leaves the old file intact, and the
        # old file is in quarantine either way.
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".np-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return f"wrote {len(self.content.encode('utf-8'))} bytes to {target}"

    async def rollback(self) -> bool:
        token = self.backups.get(str(self._resolved or self.path))
        if token is None:
            return False
        return await self._quarantine.restore(token)


class RunCommandAction(BaseAction):
    """Run one child process. No shell, no environment, hard timeout, and never
    without a recorded confirmation (rules.md §5.2, §5.4)."""

    name = "run_command"
    tier = ActionTier.DANGEROUS

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        reason: str,
        argv: tuple[str, ...],
        timeout_seconds: float = 30.0,
        cwd: str | Path | None = None,
        backup_paths: tuple[Path, ...] = (),
    ) -> None:
        super().__init__(reason=reason)
        self._sandbox = sandbox
        self.argv = tuple(argv)
        self.timeout_seconds = float(timeout_seconds)
        self.cwd = cwd
        # `$$` (safe mode) supplies the daemon's own state files here so the gate
        # quarantines a restorable copy before the command runs. `$!` supplies
        # nothing — that is the whole difference between the two prefixes.
        self.backup_paths = backup_paths
        self._resolved: tuple[str, ...] = ()

    def backup_targets(self) -> tuple[Path, ...]:
        return tuple(p for p in self.backup_paths if p.exists())

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.name,
            "reason": self.reason,
            "argv": list(self._resolved or self.argv),
            "timeout_seconds": self.timeout_seconds,
        }

    async def validate(self) -> None:
        if self.timeout_seconds <= 0:
            raise SafetyGateError("command timeout must be > 0")
        # Resolves argv[0] to a real executable and rejects malformed arguments.
        # The argv is *never* assembled into a string anywhere in this class.
        self._resolved = self._sandbox.validate_argv(self.argv)

    async def dry_run(self) -> str:
        argv = self._resolved or self.argv
        return f"run {argv[0]} with {len(argv) - 1} argument(s), {self.timeout_seconds}s budget"

    async def execute(self) -> str:
        outcome = await self._sandbox.run(
            self._resolved or self.argv, timeout_seconds=self.timeout_seconds, cwd=self.cwd
        )
        if not outcome.ok:
            raise SafetyGateError(
                f"{outcome.argv[0]} exited {outcome.returncode}: "
                f"{outcome.stderr.strip()[:200] or 'no stderr'}"
            )
        return f"{outcome.argv[0]} exited 0"

    async def rollback(self) -> bool:
        """A finished process cannot be un-run. This is exactly why the tier is
        `DANGEROUS` and why confirmation happens *before* execution rather than
        relying on undo afterwards."""
        return False


class OpenAppAction(BaseAction):
    """Launch a verified installed desktop application.

    Can only launch something already resolved in the verified installed-apps
    list (app_registry.py) — never an arbitrary string from the model. `SAFE`
    tier (user decision 2026-09-14 — module docstring's table has the full
    reasoning): the closed, verified option set is exactly what makes this
    different from `RunCommandAction`'s arbitrary argv, and the worst case
    (an unwanted window from a misheard command) is trivially undone by
    hand — no confirmation gate for that risk shape.
    """

    name = "open_app"
    tier = ActionTier.SAFE

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        reason: str,
        app_name: str,
        launch_command: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(reason=reason)
        self._sandbox = sandbox
        self.app_name = app_name.strip()
        self.launch_command = launch_command.strip()
        self.argv = tuple(shlex.split(self.launch_command))
        self.timeout_seconds = float(timeout_seconds)
        self._resolved: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.name,
            "reason": self.reason,
            "app_name": self.app_name,
            "launch_command": self.launch_command,
            "argv": list(self._resolved or self.argv),
            "timeout_seconds": self.timeout_seconds,
        }

    async def validate(self) -> None:
        if not self.app_name:
            raise SafetyGateError("empty app_name")
        if not self.launch_command or not self.argv:
            raise SafetyGateError("empty launch_command")
        if self.timeout_seconds <= 0:
            raise SafetyGateError("open_app timeout must be > 0")
        self._resolved = self._sandbox.validate_argv(self.argv)

    async def dry_run(self) -> str:
        head = (self._resolved or self.argv)[0]
        return f"open app {self.app_name} via {head}"

    async def execute(self) -> str:
        outcome = await self._sandbox.run(
            self._resolved or self.argv, timeout_seconds=self.timeout_seconds, gui=True
        )
        if not outcome.ok:
            raise SafetyGateError(
                f"failed to open {self.app_name}: "
                f"{outcome.stderr.strip()[:200] or f'exit code {outcome.returncode}'}"
            )
        return f"opened {self.app_name}"

    async def rollback(self) -> bool:
        return False  # opening an app cannot be un-run automatically


class _AdjustAction(BaseAction):
    """Shared plumbing for `AdjustVolumeAction`/`AdjustBrightnessAction`: both
    nudge a percentage up or down via one system tool, differing only in which
    tool and argv shape `_build_argv()` picks.

    `SAFE` tier (user decision 2026-09-14 — module docstring's table has the
    full reasoning): a fixed, narrow "nudge a slider" argv shape, trivially
    reversible by nudging it back — no confirmation gate for that risk shape.
    """

    tier = ActionTier.SAFE
    _label = "adjustment"

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        reason: str,
        direction: str,
        step: str = "5%",
        tool_path: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(reason=reason)
        self._sandbox = sandbox
        self.direction = direction.strip().lower()
        self.step = step.strip()
        self.tool_path = tool_path
        self.timeout_seconds = float(timeout_seconds)
        # Fixed at construction so payload() has a real argv to show in the
        # audit log's pre-execution "attempt" line, before validate() (which
        # runs later, after that line is written) confirms the executable
        # actually exists.
        self.argv = self._build_argv()
        self._resolved: tuple[str, ...] = ()

    @property
    def _is_up(self) -> bool:
        return self.direction in ("increase", "up")

    @abstractmethod
    def _build_argv(self) -> tuple[str, ...]:
        """The tool + args this adjustment would run, given `self.direction`
        and `self.tool_path` as they stand right now."""

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.name,
            "reason": self.reason,
            "direction": self.direction,
            "step": self.step,
            "argv": list(self._resolved or self.argv),
        }

    async def validate(self) -> None:
        if self.direction not in ("increase", "decrease", "up", "down"):
            raise SafetyGateError(f"invalid {self._label} adjustment direction: {self.direction!r}")
        if self.timeout_seconds <= 0:
            raise SafetyGateError("timeout must be > 0")
        self._resolved = self._sandbox.validate_argv(self.argv)

    async def dry_run(self) -> str:
        head = (self._resolved or self.argv)[0]
        return f"adjust {self._label} {self.direction} by {self.step} via {head}"

    async def execute(self) -> str:
        outcome = await self._sandbox.run(
            self._resolved or self.argv, timeout_seconds=self.timeout_seconds
        )
        if not outcome.ok:
            err_detail = outcome.stderr.strip()[:200] or f"exit code {outcome.returncode}"
            raise SafetyGateError(f"failed to adjust {self._label}: {err_detail}")
        return f"adjusted {self._label} {self.direction} by {self.step}"

    async def rollback(self) -> bool:
        return False


class AdjustVolumeAction(_AdjustAction):
    """Adjust system volume up or down via standard system tools (wpctl/pactl)."""

    name = "adjust_volume"
    _label = "volume"

    def _build_argv(self) -> tuple[str, ...]:
        delta = f"{self.step}+" if self._is_up else f"{self.step}-"
        if self.tool_path is not None:
            return (self.tool_path, "set-volume", "@DEFAULT_AUDIO_SINK@", delta)
        if shutil.which("wpctl") or not shutil.which("pactl"):
            return ("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", delta)
        pactl_delta = f"+{self.step}" if self._is_up else f"-{self.step}"
        return ("pactl", "set-sink-volume", "@DEFAULT_SINK@", pactl_delta)


class AdjustBrightnessAction(_AdjustAction):
    """Adjust display brightness up or down via standard system tools (brightnessctl)."""

    name = "adjust_brightness"
    _label = "brightness"

    def _build_argv(self) -> tuple[str, ...]:
        delta = f"{self.step}+" if self._is_up else f"{self.step}-"
        tool = self.tool_path or "brightnessctl"
        return (tool, "set", delta)

    async def rollback(self) -> bool:
        return False


# gen-ref: 3e5206ef
