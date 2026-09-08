"""B14 · derive an allowlisted web-app label from a focused window title.

The window title is the only place the compositor exposes *which browser tab*
has focus — and it also carries whatever the page put there: an email address,
an unread count, a document name. This module is the membrane.

`derive_webapp(app_id, title, ...)` takes a title, matches it against a fixed
allowlist of site names, and returns **only** the matched label (`"gmail"`) plus
its routing domain, or `None`. The `title` is an argument and a local — it is
never returned, logged, or stored. Everything downstream of the caller sees the
label or nothing. (Contrast B13's process census, which made "read a cmdline"
structurally impossible; this makes "leak a window title" structurally
impossible.)

Pure and synchronous. The one-shot TOML read is in `WebAppMap.from_file`, same
contract as `AppMap` (D-10): user-editable, missing / malformed => empty map +
one warning, a row pointing at an unknown domain => that row dropped, never
raised (rules.md §2).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from neuropaca.core.graph_memory import DOMAIN_SLUGS

__all__ = ["WebAppHit", "WebAppMap", "derive_webapp"]

_log = logging.getLogger(__name__)

# Stripped off the end of the title before matching so the allowlist keys stay
# browser-agnostic ("gmail", not "gmail - Brave"). Mutually exclusive — the
# first that matches wins and the loop stops.
_BROWSER_SUFFIXES: tuple[str, ...] = (
    " - Brave",
    " — Mozilla Firefox",
    " - Mozilla Firefox",
    " - Chromium",
    " - Google Chrome",
    " - Vivaldi",
)

# A browser title is "<page bits><delimiter><site name>". Chrome/Brave use
# " - "; some sites use em dash / en dash / middle dot / pipe. Split on all of
# them and test every resulting segment against the allowlist.
_DELIMITERS: tuple[str, ...] = (" - ", " \u2014 ", " \u2013 ", " \u00b7 ", " | ")

_SECTION = "webapp"


@dataclass(frozen=True, slots=True)
class WebAppHit:
    """The only thing that crosses the membrane."""

    label: str  # allowlist key, slugified — `webapp:<label>` in the graph
    domain: str  # full `domain:<slug>` node id


def _slug(token: str) -> str:
    return token.strip().lower().replace(" ", "-")


class WebAppMap:
    """Allowlist: a site-name token (as it appears in a window title) -> routing
    domain. Immutable once built. Build via :meth:`from_file` / :meth:`from_dict`."""

    __slots__ = ("_by_token",)

    def __init__(self, by_token: dict[str, WebAppHit]) -> None:
        # key: the token lowercased exactly as it would appear as a title segment
        self._by_token = by_token

    # ------------------------------------------------------------------ builders
    @classmethod
    def empty(cls) -> WebAppMap:
        return cls({})

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> WebAppMap:
        valid = frozenset(DOMAIN_SLUGS)
        for section in set(raw) - {_SECTION}:
            _log.warning("webapp_map: ignoring unknown section [%s]", section)
        entries = raw.get(_SECTION) or {}
        if not isinstance(entries, dict):
            _log.warning("webapp_map: [%s] is not a table — ignored", _SECTION)
            entries = {}
        by_token: dict[str, WebAppHit] = {}
        for token, domain in entries.items():
            if not isinstance(domain, str) or domain not in valid:
                _log.warning(
                    "webapp_map: [%s] %r -> %r is not one of the 10 domains — skipped",
                    _SECTION,
                    token,
                    domain,
                )
                continue
            by_token[token.strip().lower()] = WebAppHit(
                label=_slug(token), domain=f"domain:{domain}"
            )
        return cls(by_token)

    @classmethod
    def from_file(cls, path: str | Path) -> WebAppMap:
        p = Path(path)
        try:
            raw = tomllib.loads(p.read_text("utf-8"))
        except FileNotFoundError:
            _log.warning("webapp_map: %s not found — the browser stays one node", p)
            return cls.empty()
        except (OSError, tomllib.TOMLDecodeError) as exc:
            _log.warning("webapp_map: cannot read %s (%s) — the browser stays one node", p, exc)
            return cls.empty()
        return cls.from_dict(raw)

    # ------------------------------------------------------------------ lookup
    @property
    def rule_count(self) -> int:
        return len(self._by_token)

    def match_segments(self, segments: list[str]) -> WebAppHit | None:
        """Match a title's segments against the allowlist.

        1. A whole segment *is* a token: ``... - Gmail`` -> ``gmail``.
        2. Only when a delimiter actually split the title (>= 2 segments, so the
           last one is structurally the "site name" slot), a single word of the
           last segment is a token: ``Episode 5 - Watch on Crunchyroll`` ->
           ``crunchyroll``. Skipped for a one-segment title, where it would
           match an article that merely names the site.
        """
        for seg in segments:
            hit = self._by_token.get(seg)
            if hit is not None:
                return hit
        if len(segments) >= 2:
            for word in segments[-1].split():
                hit = self._by_token.get(word)
                if hit is not None:
                    return hit
        return None


def _segments(title: str) -> list[str]:
    """Strip a browser suffix, split on every title delimiter, lowercase, drop blanks.

    The raw title enters here and leaves as a list of lowercased tokens; the
    caller only ever sees the one that matched the allowlist.
    """
    for suffix in _BROWSER_SUFFIXES:
        if title.endswith(suffix):
            title = title[: -len(suffix)]
            break
    parts = [title]
    for delim in _DELIMITERS:
        parts = [piece for chunk in parts for piece in chunk.split(delim)]
    return [p.strip().lower() for p in parts if p.strip()]


def derive_webapp(
    app_id: str,
    title: str,
    *,
    browsers: frozenset[str],
    webapp_map: WebAppMap,
    enabled: bool = True,
) -> WebAppHit | None:
    """The membrane. Returns a `WebAppHit` (label + domain) or `None`.

    `None` when the feature is off, `app_id` is not a configured browser, the
    title is empty, or no title segment is an allowlisted site. The `title` is
    read here and nowhere else.
    """
    if not enabled or not title or app_id not in browsers:
        return None
    return webapp_map.match_segments(_segments(title))
