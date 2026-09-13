# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Extractive text formatting for media continuity (S3 · Media).

Pure functions shared between MediaIngest and BriefingComposer.
Never hallucinates prose; only renders clauses from factual attributes.
"""

from __future__ import annotations

import re


def series_slug(name: str) -> str:
    """Normalize a series/show/album name to a graph-safe node slug."""
    clean = re.sub(r"['\"]", "", name)
    slug = re.sub(r"[^a-zA-Z0-9\-]+", "-", clean).strip("-").lower()
    return slug or "media"


def format_media_continuity(
    media_type: str,
    *,
    show: str | None = None,
    season: int | None = None,
    episode: int | None = None,
    artist: str | None = None,
    album: str | None = None,
    title: str | None = None,
) -> str:
    """Format the deterministic "where you left off / stopped" media string.

    Rules:
    - Video with season & episode:
      "You were on episode {episode} of season {season} of {show}."
    - Video with episode only (season absent):
      "You were on episode {episode} of {show}."
    - Video with show only:
      "You were watching {show}."
    - Music with album and artist:
      "You were listening to {album} by {artist}."
    - Music with title and artist (no album):
      "You were listening to {title} by {artist}."
    - Music with album only:
      "You were listening to {album}."
    - Music with title only:
      "You were listening to {title}."
    - Music with artist only:
      "You were listening to {artist}."
    """
    if media_type == "video":
        if show and episode is not None:
            if season is not None:
                return f"You were on episode {episode} of season {season} of {show}."
            return f"You were on episode {episode} of {show}."
        if show:
            return f"You were watching {show}."
        if title:
            return f"You were watching {title}."
        return ""

    if media_type == "music":
        if album and artist:
            return f"You were listening to {album} by {artist}."
        if title and artist:
            return f"You were listening to {title} by {artist}."
        if album:
            return f"You were listening to {album}."
        if title:
            return f"You were listening to {title}."
        if artist:
            return f"You were listening to {artist}."
        return ""

    return ""


# gen-ref: 62f36eb2
