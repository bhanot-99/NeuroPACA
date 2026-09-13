# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Extractive text formatting for media continuity (S3 · Media).

Pure functions shared between MediaIngest and BriefingComposer.
Never hallucinates prose; only renders clauses from factual attributes.
"""

from __future__ import annotations


def series_slug(name: str) -> str:
    """Normalize a series/show/album name to a graph-safe node slug."""
    import re

    slug = re.sub(r"[^a-zA-Z0-9\-]+", "-", name).strip("-").lower()
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
    - Music with album and artist:
      "You were listening to {album} by {artist}."
    - Music with title and artist (no album):
      "You were listening to {title} by {artist}."
    - Music with title only:
      "You were listening to {title}."
    """
    if media_type == "video" and show and episode is not None:
        if season is not None:
            return f"You were on episode {episode} of season {season} of {show}."
        return f"You were on episode {episode} of {show}."

    if media_type == "music":
        if album and artist:
            return f"You were listening to {album} by {artist}."
        if title and artist:
            return f"You were listening to {title} by {artist}."
        if artist:
            return f"You were listening to {artist}."
        if title:
            return f"You were listening to {title}."

    # Fallback if fields are incomplete
    name = show or title or album or artist or "media"
    return f"You were listening to {name}."
