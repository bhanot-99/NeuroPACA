# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S3 · `MediaIngest` — media and continuity collector (VISION_PHASES.md).

Periodically inspects active MPRIS media players over the user session D-Bus
(`busctl --user --json=short`) to track playback continuity:
- Watched & listened spans (`EpisodeKind.MEDIA_SPAN`) opened on PlaybackStatus=Playing
  and closed on pause/stop/track-change/module stop.
- Superseding position facts (`EpisodeKind.MEDIA_POSITION_FACT`) recording show,
  season, episode, position, and duration with churn suppression.
- `NodeType.SERIES` graph nodes (`series:<slug>`) linked `PART_OF` `domain:media`.

Dual trust membrane:
- Music (xesam:artist / xesam:album populated): trusted structured metadata.
- Video (raw xesam:title mirrored from browser document.title): raw text passed
  through allowlist patterns (`data/media_title_patterns.default.toml`); unparseable
  titles are dropped entirely (zero raw title leakage).
- All D-Bus operations are strictly read-only (`list` and `GetAll`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from neuropaca.core.base_module import BaseModule
from neuropaca.core.clock import Clock, SystemClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EpisodeKind, NodeType, RelationType
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.health import ModuleHealth
from neuropaca.core.media_format import format_media_continuity, series_slug

_log = logging.getLogger(__name__)

# Built-in fallback patterns in case the configuration file is missing or unreadable
_FALLBACK_SUFFIXES = (
    r"\s*[-|•:]?\s*(?:Watch\s+All\s+Episodes.*|Watch\s+Online.*|Full\s+Episode.*|"
    r"Hianime.*|Crunchyroll.*|Netflix.*|YouTube.*|Funimation.*|AnimePahe.*)$"
)

_FALLBACK_PATTERNS: list[tuple[str, str]] = [
    (
        "s_e_compact",
        r"^(?P<show>.+?)\s+[-_:]?\s*[sS](?P<season>\d+)\s*[:._-]?\s*[eE](?P<episode>\d+)",
    ),
    (
        "season_episode_verbose",
        r"^(?P<show>.+?)\s+[-_:]?\s*[sS]eason\s*(?P<season>\d+)\s*[-_:]?\s*[eE]pisode\s*(?P<episode>\d+)",
    ),
    (
        "show_num_episode_num",
        r"^(?P<show>[A-Za-z0-9\s'\-]+?)\s+(?P<season>\d+)\s+[-_:]?\s*[eE]pisode\s*(?P<episode>\d+)",
    ),
    (
        "episode_only_verbose",
        r"^(?P<show>.+?)\s+[-_:]?\s*[eE]pisode\s*(?P<episode>\d+)",
    ),
    (
        "ep_compact",
        r"^(?P<show>.+?)\s+[-_:]?\s*[eE][pP]\.?\s*(?P<episode>\d+)",
    ),
    (
        "hash_episode",
        r"^(?P<show>.+?)\s+[-_:]?\s*#(?P<episode>\d+)",
    ),
]


class MediaIngest(BaseModule):
    """MPRIS media collector and continuity tracker."""

    def __init__(
        self,
        bus: EventBus,
        config: Config,
        gm: GraphMemory,
        episode_store: EpisodeStore | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        super().__init__("media_ingest", bus, config)
        self._gm = gm
        self._store = episode_store
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._poll_task: asyncio.Task[None] | None = None
        self._poll_interval = config.media_poll_interval_seconds
        self._patterns_path = Path(config.media_patterns_path)

        self._available = True
        self._last_poll: datetime | None = None
        self._facts_written = 0
        self._spans_written = 0
        self._titles_dropped = 0

        # Bookkeeping: active open spans: player_service -> (entity_id, start_time,
        # last extracted media dict — kept fresh every tick so a span that closes
        # without a fresh read this tick (player vanished) can still refresh the
        # position fact from the last real observation, not the first one).
        self._open_spans: dict[str, tuple[str, datetime, dict[str, Any]]] = {}
        # Fact churn suppression: entity_id -> identity state_key (show/season/
        # episode/artist/album/title — never position, see _assert_position_fact).
        self._last_state: dict[str, tuple[Any, ...]] = {}
        # entity_id -> identity + rounded position of the last fact actually
        # written. A forced (span-close) refresh skips the write when this is
        # unchanged — nothing new was observed since the last write, so
        # forcing one anyway would just be a redundant duplicate.
        self._last_written: dict[str, tuple[Any, ...]] = {}

        self._cleanup_regex: re.Pattern[str] = re.compile(_FALLBACK_SUFFIXES, re.IGNORECASE)
        self._compiled_patterns: list[tuple[str, re.Pattern[str]]] = [
            (name, re.compile(pat, re.IGNORECASE)) for name, pat in _FALLBACK_PATTERNS
        ]

    async def initialize(self) -> None:
        if not shutil.which("busctl"):
            _log.warning("busctl not found in PATH; MediaIngest disabling itself")
            self._available = False
            return

        self._load_patterns()

    def _load_patterns(self) -> None:
        if not self._patterns_path.is_file():
            _log.info(
                "media_patterns_path '%s' not found; using built-in fallback patterns",
                self._patterns_path,
            )
            return

        try:
            data = tomllib.loads(self._patterns_path.read_text("utf-8"))
            cleanup = data.get("cleanup", {})
            suffixes = cleanup.get("strip_suffixes", [])
            if suffixes:
                combined = "|".join(f"(?:{s})" for s in suffixes)
                self._cleanup_regex = re.compile(combined, re.IGNORECASE)

            patterns = data.get("patterns", [])
            compiled = []
            for p in patterns:
                name = p.get("name", "pattern")
                regex_str = p.get("regex")
                if regex_str:
                    compiled.append((name, re.compile(regex_str, re.IGNORECASE)))
            if compiled:
                self._compiled_patterns = compiled
        except Exception as exc:
            _log.warning(
                "failed to load media title patterns from %s: %s",
                self._patterns_path,
                exc,
            )

    async def start(self) -> None:
        if not self._available or not self.config.media_tracking_enabled:
            return
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop(), name="media_ingest_poll")

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        # Close any remaining open spans on shutdown, refreshing each one's
        # position fact to the last real observation rather than leaving it
        # frozen at whenever the episode/track was first detected.
        now = self._clock.now()
        for _player, (entity_id, start_time, last_extracted) in list(self._open_spans.items()):
            if self._store is not None:
                self._store.record_span(EpisodeKind.MEDIA_SPAN, entity_id, start_time, now)
                self._spans_written += 1
            self._assert_position_fact(entity_id, last_extracted, now, force=True)
        self._open_spans.clear()

    def health(self) -> ModuleHealth:
        if not self._available:
            return ModuleHealth(name=self.name, ok=True, detail="disabled (busctl missing)")
        if not self.config.media_tracking_enabled:
            return ModuleHealth(name=self.name, ok=True, detail="disabled (config)")
        return ModuleHealth(
            name=self.name,
            ok=True,
            detail=(
                f"facts_written={self._facts_written}, "
                f"spans_written={self._spans_written}, "
                f"titles_dropped={self._titles_dropped}"
            ),
            last_event_at=self._last_poll,
        )

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self.poll_tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.warning("MediaIngest poll tick error: %s", exc)

    async def _run_busctl(self, *args: str, timeout_seconds: float = 5.0) -> tuple[int, str]:
        """Strictly read-only busctl invocation."""
        proc = await asyncio.create_subprocess_exec(
            "busctl",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                stdout, _ = await proc.communicate()
                return proc.returncode or 0, stdout.decode("utf-8", errors="replace")
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return -1, ""

    def _unwrap_variant(self, v: Any) -> Any:
        if isinstance(v, dict) and "data" in v:
            return v["data"]
        return v

    def extract_media(
        self, metadata: dict[str, Any], position_us: int = 0
    ) -> dict[str, Any] | None:
        """Extract structured media info from MPRIS metadata.

        Returns a dictionary of extracted fields, or None if the title should be dropped.
        """
        meta = {k: self._unwrap_variant(v) for k, v in metadata.items()}

        raw_artists = meta.get("xesam:artist")
        raw_album = meta.get("xesam:album")
        raw_title = str(meta.get("xesam:title") or "").strip()

        artists: list[str] = []
        if isinstance(raw_artists, list):
            artists = [str(a).strip() for a in raw_artists if str(a).strip()]
        elif isinstance(raw_artists, str) and raw_artists.strip():
            artists = [raw_artists.strip()]

        album = str(raw_album).strip() if raw_album else None
        length_us = meta.get("mpris:length")
        dur_s = float(length_us) / 1_000_000.0 if length_us and length_us > 0 else None
        pos_s = float(position_us) / 1_000_000.0 if position_us > 0 else 0.0

        # 1. Music trust branch
        if (artists or album) and raw_title:
            artist_str = ", ".join(artists) if artists else "Unknown Artist"
            return {
                "media_type": "music",
                "show": None,
                "season": None,
                "episode": None,
                "artist": artist_str,
                "album": album,
                "title": raw_title,
                "position_seconds": pos_s,
                "duration_seconds": dur_s,
            }

        # 2. Video trust branch: filter raw_title through allowlist patterns
        if not raw_title:
            return None

        cleaned_title = self._cleanup_regex.sub("", raw_title).strip()
        for _, pattern in self._compiled_patterns:
            match = pattern.search(cleaned_title)
            if match:
                groups = match.groupdict()
                show = groups.get("show", "").strip(" -:_")
                season_str = groups.get("season")
                episode_str = groups.get("episode")

                season = int(season_str) if season_str and season_str.isdigit() else None
                episode = int(episode_str) if episode_str and episode_str.isdigit() else None

                if show and episode is not None:
                    return {
                        "media_type": "video",
                        "show": show,
                        "season": season,
                        "episode": episode,
                        "artist": None,
                        "album": None,
                        "title": cleaned_title,
                        "position_seconds": pos_s,
                        "duration_seconds": dur_s,
                    }

        # Dropped entirely: non-allowlisted / raw web title
        self._titles_dropped += 1
        return None

    async def _query_active_players(self) -> list[tuple[str, dict[str, Any]]]:
        rc, out = await self._run_busctl("--user", "--json=short", "list")
        if rc != 0 or not out.strip():
            return []

        try:
            services = json.loads(out)
        except Exception:
            return []

        mpris_services = [
            s["name"]
            for s in services
            if isinstance(s, dict)
            and s.get("name", "").startswith("org.mpris.MediaPlayer2.")
            and s.get("pid") is not None
        ]

        active: list[tuple[str, dict[str, Any]]] = []
        for svc in mpris_services:
            rc_p, out_p = await self._run_busctl(
                "--user",
                "--json=short",
                "call",
                svc,
                "/org/mpris/MediaPlayer2",
                "org.freedesktop.DBus.Properties",
                "GetAll",
                "s",
                "org.mpris.MediaPlayer2.Player",
            )
            if rc_p == 0 and out_p.strip():
                try:
                    parsed = json.loads(out_p)
                    data_arr = parsed.get("data", [{}])
                    if data_arr:
                        active.append((svc, data_arr[0]))
                except Exception:
                    continue
        return active

    def _assert_position_fact(
        self,
        entity_id: str,
        extracted: dict[str, Any],
        now: datetime,
        *,
        force: bool = False,
    ) -> bool:
        """Write `EpisodeKind.MEDIA_POSITION_FACT` when the media identity
        changed since the last write, or — when `force` (span close: pause /
        stop / track-change / shutdown) — when the position has moved since
        the last write, so the recorded position is the real stopping point
        rather than frozen at first detection. A forced call whose reading is
        byte-identical to what's already recorded is a no-op: nothing new was
        observed since the last write (e.g. it closed on the very next tick),
        so writing again would just be a redundant duplicate."""
        media_type = extracted["media_type"]
        show = extracted["show"]
        season = extracted["season"]
        episode = extracted["episode"]
        artist = extracted["artist"]
        album = extracted["album"]
        title = extracted["title"]
        pos_s = extracted["position_seconds"]
        dur_s = extracted["duration_seconds"]
        label = extracted["label"]
        state_key: tuple[Any, ...] = (media_type, show, season, episode, artist, album, title)
        written_key: tuple[Any, ...] = (*state_key, round(pos_s))

        if force:
            if self._last_written.get(entity_id) == written_key:
                return False
        elif self._last_state.get(entity_id) == state_key:
            return False

        self._last_state[entity_id] = state_key
        self._last_written[entity_id] = written_key
        if self._store is None:
            return False

        summary_text = format_media_continuity(
            media_type=media_type,
            show=show,
            season=season,
            episode=episode,
            artist=artist,
            album=album,
            title=title,
        )
        self._store.assert_fact(
            EpisodeKind.MEDIA_POSITION_FACT,
            entity_id,
            str(episode if episode is not None else title or label),
            valid_from=now,
            source="mpris",
            attrs={
                "media_type": media_type,
                "show": show,
                "season": season,
                "episode": episode,
                "artist": artist,
                "album": album,
                "title": title,
                "position_seconds": pos_s,
                "duration_seconds": dur_s,
                "summary_text": summary_text,
            },
        )
        self._facts_written += 1
        return True

    async def poll_tick(self) -> int:
        """One scan across active MPRIS media players."""
        now = self._clock.now()
        self._last_poll = now

        if not self._available:
            return 0

        players = await self._query_active_players()
        active_player_names = {svc for svc, _ in players}

        # 1. Close open spans for players that are no longer active — refresh
        # the position fact from the last real observation before dropping it.
        for svc in list(self._open_spans.keys()):
            if svc not in active_player_names:
                entity_id, start_time, last_extracted = self._open_spans.pop(svc)
                if self._store is not None:
                    self._store.record_span(EpisodeKind.MEDIA_SPAN, entity_id, start_time, now)
                    self._spans_written += 1
                self._assert_position_fact(entity_id, last_extracted, now, force=True)

        facts_this_tick = 0

        for svc, props in players:
            playback_status = self._unwrap_variant(props.get("PlaybackStatus", "Stopped"))
            position_us = int(self._unwrap_variant(props.get("Position", 0)))
            metadata = self._unwrap_variant(props.get("Metadata", {}))

            extracted = self.extract_media(metadata, position_us=position_us)
            if extracted is None:
                # No valid media or dropped title; if player had open span, close
                # it and refresh its position fact from the last good reading.
                if svc in self._open_spans:
                    entity_id, start_time, last_extracted = self._open_spans.pop(svc)
                    if self._store is not None:
                        self._store.record_span(EpisodeKind.MEDIA_SPAN, entity_id, start_time, now)
                        self._spans_written += 1
                    if self._assert_position_fact(entity_id, last_extracted, now, force=True):
                        facts_this_tick += 1
                continue

            media_type = extracted["media_type"]
            show = extracted["show"]
            album = extracted["album"]
            artist = extracted["artist"]
            title = extracted["title"]

            # Generate entity ID and label
            if media_type == "video" and show:
                entity_id = f"series:{series_slug(show)}"
                label = show
            else:
                music_name = album or artist or title or "music"
                entity_id = f"series:{series_slug(music_name)}"
                label = music_name
            extracted["label"] = label

            # Span management
            if playback_status == "Playing":
                if svc in self._open_spans:
                    old_entity_id, start_time, old_extracted = self._open_spans[svc]
                    if old_entity_id != entity_id:
                        # Track/show changed: close previous span (refreshing its
                        # final position), start a new one.
                        if self._store is not None:
                            self._store.record_span(
                                EpisodeKind.MEDIA_SPAN, old_entity_id, start_time, now
                            )
                            self._spans_written += 1
                        closed = self._assert_position_fact(
                            old_entity_id, old_extracted, now, force=True
                        )
                        if closed:
                            facts_this_tick += 1
                        self._open_spans[svc] = (entity_id, now, extracted)
                    else:
                        # Same episode/track still playing — keep the cached
                        # reading fresh for whenever this span eventually closes.
                        self._open_spans[svc] = (entity_id, start_time, extracted)
                else:
                    self._open_spans[svc] = (entity_id, now, extracted)

                # Superseding fact, churn-suppressed on identity — the position
                # itself is refreshed for real when the span eventually closes.
                if self._assert_position_fact(entity_id, extracted, now):
                    facts_this_tick += 1
            else:
                # Paused or Stopped — this tick's reading is the real stopping
                # position, so refresh it here (forced) rather than waiting for
                # a possibly much later close-from-inactivity tick.
                if svc in self._open_spans:
                    old_entity_id, start_time, _old_extracted = self._open_spans.pop(svc)
                    if self._store is not None:
                        self._store.record_span(
                            EpisodeKind.MEDIA_SPAN, old_entity_id, start_time, now
                        )
                        self._spans_written += 1
                    if self._assert_position_fact(entity_id, extracted, now, force=True):
                        facts_this_tick += 1

            # GraphMemory integration
            if self._gm is not None:
                if not self._gm.has_node(entity_id):
                    await self._gm.upsert_node(
                        entity_id,
                        NodeType.SERIES,
                        attributes={
                            "label": label,
                            "media_type": media_type,
                        },
                    )
                    if self._gm.has_node("domain:media"):
                        await self._gm.add_edge(
                            entity_id,
                            "domain:media",
                            relation=RelationType.PART_OF,
                        )
                await self._gm.mark_seen(entity_id, now)

        return facts_this_tick

    async def forget(self, series: str) -> int:
        """Purge all facts, episodes, and graph nodes associated with this series."""
        entity_id = series if series.startswith("series:") else f"series:{series_slug(series)}"

        # Close and remove any active open spans
        for svc, (span_entity, _start_time, _extracted) in list(self._open_spans.items()):
            if span_entity == entity_id:
                del self._open_spans[svc]

        self._last_state.pop(entity_id, None)
        self._last_written.pop(entity_id, None)

        forgotten = 0
        if self._store is not None:
            forgotten = await self._store.forget(entity_id)

        if self._gm is not None and self._gm.has_node(entity_id):
            await self._gm.delete_node(entity_id)

        return forgotten


# gen-ref: 35aa005c
