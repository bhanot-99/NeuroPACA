# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L3 · `AppIdentity` — one canonical key per real application (B17, D-20).

**Why this exists.** Two sensors name the same app differently: the focus sensor
uses the Wayland `app_id` (`com.system76.CosmicFiles`), the B13 process census
uses the Unix process name (`cosmic-files`). Both then form `app:<name>`, so
`GraphMemory.upsert_node` — which de-dups by exact id — builds two or three nodes
for one app: the behavioural edges land on one, the RAM/CPU on another. B13 §7
called for this "round-2 name map" and deferred it.

`AppIdentity.resolve()` collapses every alias of one app to a single slug so the
node id is stable regardless of which sensor saw it. Resolution order, first hit
wins:

1. exact ``[alias]`` entry — the escape hatch for reverse-DNS ids that the
   normaliser cannot reach (`com.system76.CosmicTerm` and the process name
   ``cosmic-comp`` genuinely do not converge). User-editable, a dogfood output.
2. **normalise** — lowercase; strip a trailing ``-browser`` / ``-desktop`` /
   ``-bin`` / ``-gtk`` / ``-wayland`` / ``-stable``; ``_`` ``.`` space → ``-``;
   collapse repeats; trim. Catches `Brave-Browser` → `brave`, leaves `foot`
   alone.
3. the normalised form itself — an unknown app is still tracked, just under a
   tidy key.

``is_non_app()`` flags thread names and bare shells that the census sometimes
reports as a process ``name`` — never a real activity.

Pure, immutable, built once at ``SignalCorrelator.initialize()``. A missing or
malformed file degrades to *normalise-only* with one warning (rules.md §2) — the
daemon still runs, apps are just less tidily merged until the file is fixed.
"""

from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path

__all__ = ["AppIdentity"]

_log = logging.getLogger(__name__)

_SECTIONS = frozenset({"alias", "non_app"})
_STRIP_SUFFIXES: tuple[str, ...] = (
    "-browser",
    "-desktop",
    "-bin",
    "-gtk",
    "-wayland",
    "-stable",
    "-nightly",
)
# A process `name` (from /proc/<pid>/comm) that is a thread label, not an app.
# Deliberately conservative — a name that reaches an `is_non_app` node is DROPPED,
# so this must never match a real app_id. Other junk (`gmain`, `gdbus`) goes in
# the `[non_app]` table where the operator can see and edit it.
_THREAD_NAME_RE = re.compile(
    r"^(MainThread|Thread-\d+.*|asyncio_\d+|ThreadPoolExecutor.*|"
    r"pool-\d+-thread-\d+|Timer-\d+|tokio-runtime-w.*|rayon-.*)$"
)
_NON_APP_BUILTIN: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "fish", "dash", "env", "sudo", "doas", "which", "?"}
)


def normalise(raw: str) -> str:
    """The deterministic half of `resolve` — no table, safe to reuse (the cleanup
    script duplicates exactly this)."""
    s = raw.strip().lower()
    for suffix in _STRIP_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    s = re.sub(r"[\s._]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or raw.strip().lower()


class AppIdentity:
    """Immutable once built. Construct via :meth:`from_file` / :meth:`from_dict`."""

    __slots__ = ("_alias", "_non_app")

    def __init__(self, alias: dict[str, str], non_app: frozenset[str]) -> None:
        # alias keys are matched raw *and* case-folded, so a table entry works
        # whether the sensor reports `Brave` or `brave`.
        self._alias: dict[str, str] = {}
        for k, v in alias.items():
            canon = normalise(v)
            self._alias[k] = canon
            self._alias.setdefault(k.lower(), canon)
        self._non_app = frozenset(n.lower() for n in non_app)

    # ------------------------------------------------------------------ builders
    @classmethod
    def empty(cls) -> AppIdentity:
        return cls({}, frozenset())

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> AppIdentity:
        for section in set(raw) - _SECTIONS:
            _log.warning("app_identity: ignoring unknown section [%s]", section)

        alias_raw = raw.get("alias") or {}
        alias: dict[str, str] = {}
        if isinstance(alias_raw, dict):
            for k, v in alias_raw.items():
                if isinstance(v, str) and v.strip():
                    alias[str(k)] = v
                else:
                    _log.warning("app_identity: [alias] %r -> %r is not a name — skipped", k, v)
        else:
            _log.warning("app_identity: [alias] is not a table — ignored")

        non_app_raw = raw.get("non_app") or []
        non_app: set[str] = set()
        if isinstance(non_app_raw, list):
            non_app = {str(x) for x in non_app_raw if isinstance(x, str) and x.strip()}
        else:
            _log.warning("app_identity: [non_app] is not an array — ignored")

        return cls(alias, frozenset(non_app))

    @classmethod
    def from_file(cls, path: str | Path) -> AppIdentity:
        p = Path(path)
        try:
            raw = tomllib.loads(p.read_text("utf-8"))
        except FileNotFoundError:
            _log.warning("app_identity: %s not found — apps merged by normalisation only", p)
            return cls.empty()
        except (OSError, tomllib.TOMLDecodeError) as exc:
            _log.warning("app_identity: cannot read %s (%s) — normalisation only", p, exc)
            return cls.empty()
        return cls.from_dict(raw)

    # ------------------------------------------------------------------ lookup
    @property
    def alias_count(self) -> int:
        # each entry is stored under at most two keys; report the logical count
        return len({v for v in self._alias.values()}) if self._alias else 0

    def resolve(self, raw: str | None) -> str:
        """The canonical slug for a Wayland app_id or a process name. Never
        raises; an empty/None input returns ``""`` and the caller skips it."""
        if not raw:
            return ""
        hit = self._alias.get(raw) or self._alias.get(raw.lower())
        if hit is not None:
            return hit
        return normalise(raw)

    def is_non_app(self, raw: str | None) -> bool:
        """True for a process ``name`` that is a thread label or a bare shell —
        the census should not have made a node for it."""
        if not raw:
            return True
        low = raw.strip().lower()
        if low in self._non_app or low in _NON_APP_BUILTIN:
            return True
        return bool(_THREAD_NAME_RE.match(raw.strip()))

    def pretty(self, canonical: str) -> str:
        """A human label for a canonical slug — `cosmic-files` -> "Cosmic Files".
        Title-cases word by word, keeps known acronyms upper."""
        if not canonical:
            return ""
        words = [w for w in re.split(r"[-\s]+", canonical) if w]
        out: list[str] = []
        for w in words:
            if w in _ACRONYMS:
                out.append(w.upper())
            elif w in _PRETTY_WORDS:
                out.append(_PRETTY_WORDS[w])
            else:
                out.append(w[:1].upper() + w[1:])
        return " ".join(out)


_ACRONYMS: frozenset[str] = frozenset({"ide", "cli", "vs", "db", "ai", "mcp", "os"})
_PRETTY_WORDS: dict[str, str] = {
    "vscode": "VS Code",
    "code": "VS Code",
    "cosmicterm": "Cosmic Term",
    "cosmicfiles": "Cosmic Files",
    "cosmicmonitor": "Cosmic Monitor",
    "cosmiccomp": "Cosmic Comp",
    "github": "GitHub",
    "gitlab": "GitLab",
    "youtube": "YouTube",
    "chatgpt": "ChatGPT",
    "whatsapp": "WhatsApp",
    "linkedin": "LinkedIn",
}


# gen-ref: b17-app-identity
