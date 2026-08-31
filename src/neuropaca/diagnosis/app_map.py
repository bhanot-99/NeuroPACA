"""L3 · `AppMap` — activity → one of the 10 routing domains (B2.5b, D-10).

`problems.md 1.6`: sorting activity into the blueprint's 10 domains cannot be a
model call (that breaks "no inference in the fast path", rules.md §4.1) and a
hard-coded table is brittle. The resolution is a **simple, user-editable rules
file** — `data/app_map.default.toml` — read once at `SignalCorrelator.initialize()`
and consulted with a pure dict + glob lookup on every `APP_SWITCH`.

Lookup order (first hit wins):

1. exact `app_id`   — O(1)   e.g. `"dev.zed.Zed" -> engineering`
2. exact `wm_class` — O(1)   the XWayland fallback identifier
3. `path_glob`      — O(k)   `fnmatch` against a working-directory path, k = rule count

A miss returns ``None`` — the caller leaves the activity unclassified rather
than guessing. Domains are validated against `GraphMemory.DOMAIN_SLUGS` at load;
an unknown domain is dropped with a warning, never raised — a typo in the rules
file must not stop the daemon (rules.md §2).
"""

from __future__ import annotations

import fnmatch
import logging
import re
import tomllib
from pathlib import Path

from neuropaca.core.graph_memory import DOMAIN_SLUGS

_log = logging.getLogger(__name__)

_SECTIONS = ("app_id", "wm_class", "path_glob")


class AppMap:
    """Immutable once built. Construct via :meth:`from_file` or :meth:`from_dict`."""

    __slots__ = ("_by_app_id", "_by_wm_class", "_path_globs")

    def __init__(
        self,
        by_app_id: dict[str, str],
        by_wm_class: dict[str, str],
        path_globs: list[tuple[str, str]],
    ) -> None:
        # Values are the full `domain:<slug>` node id, not the bare slug — the
        # APP_SWITCH fast path returns them verbatim, no per-call f-string.
        self._by_app_id = {k: f"domain:{v}" for k, v in by_app_id.items()}
        self._by_wm_class = {k: f"domain:{v}" for k, v in by_wm_class.items()}
        # Compile the globs once — `fnmatch.fnmatch` per call re-normcases and
        # hits its own cache every time; here we want a bare `re.Pattern.match`.
        self._path_globs: list[tuple[re.Pattern[str], str]] = [
            (re.compile(fnmatch.translate(pattern)), f"domain:{domain}")
            for pattern, domain in path_globs
        ]

    # ------------------------------------------------------------------ builders
    @classmethod
    def empty(cls) -> AppMap:
        return cls({}, {}, [])

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> AppMap:
        """Build from an already-parsed TOML mapping. Unknown top-level sections
        and rules pointing at unknown domains are logged and skipped."""
        valid = frozenset(DOMAIN_SLUGS)
        for section in set(raw) - set(_SECTIONS):
            _log.warning("app_map: ignoring unknown section [%s]", section)

        def _clean(section: str) -> dict[str, str]:
            entries = raw.get(section) or {}
            if not isinstance(entries, dict):
                _log.warning("app_map: section [%s] is not a table — ignored", section)
                return {}
            out: dict[str, str] = {}
            for key, domain in entries.items():
                if isinstance(domain, str) and domain in valid:
                    out[key] = domain
                else:
                    _log.warning(
                        "app_map: [%s] %r -> %r is not one of the 10 domains — skipped",
                        section,
                        key,
                        domain,
                    )
            return out

        return cls(
            by_app_id=_clean("app_id"),
            by_wm_class=_clean("wm_class"),
            path_globs=list(_clean("path_glob").items()),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> AppMap:
        """Read a TOML rules file. A missing or malformed file yields an empty
        map plus one warning — classification then always returns ``None`` and
        the daemon runs on unclassified activity (rules.md §2)."""
        p = Path(path)
        try:
            raw = tomllib.loads(p.read_text("utf-8"))
        except FileNotFoundError:
            _log.warning("app_map: %s not found — activity will be unclassified", p)
            return cls.empty()
        except (OSError, tomllib.TOMLDecodeError) as exc:
            _log.warning("app_map: cannot read %s (%s) — activity will be unclassified", p, exc)
            return cls.empty()
        return cls.from_dict(raw)

    # ------------------------------------------------------------------ lookup
    @property
    def rule_count(self) -> int:
        return len(self._by_app_id) + len(self._by_wm_class) + len(self._path_globs)

    def classify(
        self,
        app_id: str | None,
        *,
        wm_class: str | None = None,
        path: str | None = None,
    ) -> str | None:
        """Return a ``domain:<slug>`` node id, or ``None`` on a miss."""
        if app_id is not None:
            hit = self._by_app_id.get(app_id)
            if hit is not None:
                return hit
        if wm_class is not None:
            hit = self._by_wm_class.get(wm_class)
            if hit is not None:
                return hit
        if path:
            for pattern, domain in self._path_globs:
                if pattern.match(path):
                    return domain
        return None
