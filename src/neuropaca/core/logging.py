"""Logging setup for the daemon.

Stdlib `logging` only — no dependency (rules.md §9). One `configure()` call at
boot (L10), then every module takes a logger with `get_logger(__name__)`.

Privacy (rules.md §6): logs carry cold system numbers and graph *structure*
only. Never a `conversation_history` entry, never a raw `MetricSnapshot` dump,
never model output verbatim. That is a review rule, not something enforced here;
`redact()` is provided for the few places a value of unknown provenance reaches
a log line.

The file sink is B9 (`log_to_file` / `log_file_path`). Rotation is deliberately
*not* done in-process: `scripts/logrotate/neuropaca` rotates it with
`copytruncate`, which preserves the inode, so the handle opened here stays
valid across a rotation and the daemon needs no SIGHUP and no restart.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TextIO

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_ROOT = "neuropaca"
_configured = False


def configure(
    level: str = "INFO",
    *,
    stream: TextIO | None = None,
    file_path: str | None = None,
) -> None:
    """Attach handlers to the ``neuropaca`` logger. Idempotent.

    Called once from the orchestrator at startup with ``Config.log_level`` and,
    when ``Config.log_to_file`` is on, ``Config.log_file_path``. Calling it again
    re-points the handlers (useful in tests) but never stacks them, so records
    are not duplicated.

    ``file_path`` adds a plain ``FileHandler`` alongside the stream — plain, not
    rotating, because logrotate owns rotation (B9/BL-4); two rotators on one file
    is how logs get lost. A file sink that cannot be opened is logged and
    skipped: a daemon must not fail to boot over its own logging.
    """
    global _configured

    logger = logging.getLogger(_ROOT)
    logger.setLevel(_coerce_level(level))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    if file_path:
        try:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            # 0600: the log carries graph structure and process names (rules.md §6).
            path.chmod(0o600)
        except OSError as exc:
            logger.error("cannot open log file %s (%r) — stream sink only", file_path, exc)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``neuropaca`` root.

    Accepts a dotted module name (``neuropaca.sensing.collectors.system``) or a
    bare label; either way the result sits under the configured root so it
    inherits the level and the single handler.
    """
    if name == _ROOT or name.startswith(_ROOT + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT}.{name}")


def is_configured() -> bool:
    """True once `configure()` has run — for a startup assertion."""
    return _configured


def redact(value: object, *, keep: int = 0) -> str:
    """Collapse a value to a length-tagged placeholder for safe logging.

    Use at the boundary where a string of unknown provenance (a file path from
    the graph, a fragment of model output) would otherwise land in a log line.
    ``keep`` optionally preserves a short leading slice for debugging.
    """
    text = str(value)
    if keep > 0 and len(text) > keep:
        return f"{text[:keep]}…<redacted {len(text)} chars>"
    return f"<redacted {len(text)} chars>"


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelNamesMapping().get(level.upper())
    if resolved is None:
        raise ValueError(f"unknown log level: {level!r}")
    return resolved
