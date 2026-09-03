"""L7 · the action audit log (rules.md §5.6, Architecture.md §11b).

Every attempt writes **two** JSONL lines to `Config.action_log_path`: one before
anything happens (`phase: "attempt"`) and one after it resolves
(`phase: "result"`). Two lines, not one, is the whole point — a single
after-the-fact line cannot distinguish "never tried" from "tried and the daemon
died mid-effect". A pair with a missing second half is itself the evidence.

Refusals are attempts. A tier that is disabled, a validation that fails, a
confirmation that times out — each writes the pair, so the log answers "what did
this thing try to do to me" completely, which is the B7 exit criterion.

The file is opened per record and appended with a trailing newline (JSONL) at
mode 0600. `open(..., "a")` on a local filesystem is atomic for writes below
`PIPE_BUF`, and the write runs in a worker thread so the loop never blocks on it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


class ActionAudit:
    """Append-only JSONL sink. Never raises into the caller — an audit failure is
    logged and counted, because losing the log must not also lose the action's
    error (the gate refuses before executing if it cannot record; see `SafetyGate`)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._writes = 0
        self._failures = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def writes(self) -> int:
        return self._writes

    @property
    def failures(self) -> int:
        return self._failures

    async def record(self, phase: str, **fields: Any) -> bool:
        """Append one line. Returns False if it could not be written."""
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "phase": phase,
            **fields,
        }
        try:
            await asyncio.to_thread(self._append, json.dumps(record, default=str))
        except OSError as exc:
            self._failures += 1
            _log.error("action audit write failed (%s): %r", self._path, exc)
            return False
        self._writes += 1
        return True

    def _append(self, line: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())  # the log must survive the crash it is recording
        os.chmod(self._path, 0o600)
