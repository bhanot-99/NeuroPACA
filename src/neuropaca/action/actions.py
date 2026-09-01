"""L7 · the concrete actions (Architecture.md §11b, B7, D-14).

Four ship in B7, in ascending order of what they can break:

| action | tier | effect | reversible by |
| --- | --- | --- | --- |
| `NotificationAction` | safe | publishes an *intent*; L9 delivers it | nothing to undo |
| `MemoryWriteAction` | safe | one graph node (+ edges) | deleting the node it created |
| `FileWriteAction` | dangerous | one file, backed up first | restoring the quarantined copy |
| `RunCommandAction` | dangerous | one child process, no shell/env | nothing — hence confirmation |

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
import tempfile
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
