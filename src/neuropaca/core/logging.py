"""Logging setup for the daemon.

Stdlib `logging` only — no dependency (rules.md §9). One `configure()` call at
boot (L10), then every module takes a logger with `get_logger(__name__)`.

Privacy (rules.md §6): logs carry cold system numbers and graph *structure*
only. Never a `conversation_history` entry, never a raw `MetricSnapshot` dump,
never model output verbatim. That is a review rule, not something enforced here;
`redact()` is provided for the few places a value of unknown provenance reaches
a log line.

Rotation and a JSONL sink to `data/` are B9 (`phases.md` — Hardening). B0 emits
a single human-readable line per record to a stream.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_ROOT = "neuropaca"
_configured = False


def configure(level: str = "INFO", *, stream: TextIO | None = None) -> None:
    """Attach one handler to the ``neuropaca`` logger. Idempotent.

    Called once from the orchestrator at startup with ``Config.log_level``.
    Calling it again re-points the handler (useful in tests) but never stacks
    handlers, so records are not duplicated.
    """
    global _configured

    logger = logging.getLogger(_ROOT)
    logger.setLevel(_coerce_level(level))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(handler)

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
