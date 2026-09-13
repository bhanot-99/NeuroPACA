# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S3/S4 · `MediaIngest` & `MediaPlugin` — media and continuity collector (VISION_PHASES.md).

Migrated under S4 Plugin Contract:
- `MediaPlugin` implements the pure `Plugin` reader protocol over MPRIS D-Bus.
- `MediaIngest` hosts `MediaPlugin` via `PluginHost`.

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
from neuropaca.sensing.plugin_host import (
    PluginDescriptor,
    PluginHost,
    PluginItem,
    PluginManifest,
)

_log = logging.getLogger(__name__)

__all__ = ["MediaIngest", "MediaPlugin", "format_media_continuity", "series_slug"]

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


class MediaPlugin:
    """Pure MPRIS reader plugin extracting media state."""

    def __init__(
        self,
        config: Config,
        clock: Clock | None = None,
        parent: MediaIngest | None = None,
    ) -> None:
        self.config = config
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._parent = parent
        self._patterns_path = Path(config.media_patterns_path)
        self._titles_dropped = 0
        self._open_spans: dict[str, tuple[str, datetime, dict[str, Any]]] = {}
        self._entities: set[str] = set()

        self._cleanup_regex: re.Pattern[str] = re.compile(_FALLBACK_SUFFIXES, re.IGNORECASE)
        self._compiled_patterns: list[tuple[str, re.Pattern[str]]] = [
            (name, re.compile(pat, re.IGNORECASE)) for name, pat in _FALLBACK_PATTERNS
        ]

    async def initialize(self) -> None:
        self._load_patterns()

    def describe(self) -> PluginDescriptor:
        patterns_p = self._patterns_path.expanduser().resolve()
        return PluginDescriptor(
            name="media",
            node_type=NodeType.SERIES,
            domain_hub="domain:media",
            poll_interval_seconds=self.config.media_poll_interval_seconds,
            span_kind=EpisodeKind.MEDIA_SPAN,
            manifest=PluginManifest(
                allowed_read_paths=(patterns_p,),
                allow_network=False,
                allow_subprocesses=True,
            ),
            entity_prefixes=("series:", "media:", "track:"),
        )

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

    async def _run_busctl(self, *args: str, timeout_seconds: float = 5.0) -> tuple[int, str]:
        if self._parent is not None:
            try:
                return await self._parent._run_busctl(*args, timeout_seconds=timeout_seconds)
            except TypeError:
                return await self._parent._run_busctl(*args)
        return await self.run_busctl_default(*args, timeout_seconds=timeout_seconds)

    async def run_busctl_default(self, *args: str, timeout_seconds: float = 5.0) -> tuple[int, str]:
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

    def _build_item(
        self,
        entity_id: str,
        label: str,
        extracted: dict[str, Any],
        active: bool,
    ) -> PluginItem:
        media_type = extracted["media_type"]
        show = extracted["show"]
        season = extracted["season"]
        episode = extracted["episode"]
        artist = extracted["artist"]
        album = extracted["album"]
        title = extracted["title"]
        pos_s = extracted["position_seconds"]
        dur_s = extracted["duration_seconds"]

        summary_text = format_media_continuity(
            media_type=media_type,
            show=show,
            season=season,
            episode=episode,
            artist=artist,
            album=album,
            title=title,
        )
        fact_obj = str(episode if episode is not None else title or label)
        attrs = {
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
        }
        state_key = (media_type, show, season, episode, artist, album, title)

        return PluginItem(
            entity_id=entity_id,
            label=label,
            node_type=NodeType.SERIES,
            node_attributes={"label": label, "media_type": media_type},
            active=active,
            span_kind=EpisodeKind.MEDIA_SPAN,
            fact=(EpisodeKind.MEDIA_POSITION_FACT, fact_obj, attrs),
            state_key=state_key,
            edges=((entity_id, "domain:media", RelationType.PART_OF),),
        )

    async def items(self, since: datetime) -> list[PluginItem]:
        now = self._clock.now()
        players = await self._query_active_players()
        active_player_names = {svc for svc, _ in players}
        items: list[PluginItem] = []

        # 1. Detect players that vanished
        for svc in list(self._open_spans.keys()):
            if svc not in active_player_names:
                entity_id, _start_time, last_extracted = self._open_spans.pop(svc)
                label = last_extracted.get("label", entity_id)
                items.append(self._build_item(entity_id, label, last_extracted, active=False))

        # 2. Process active players
        for svc, props in players:
            playback_status = self._unwrap_variant(props.get("PlaybackStatus", "Stopped"))
            position_us = int(self._unwrap_variant(props.get("Position", 0)))
            metadata = self._unwrap_variant(props.get("Metadata", {}))

            extracted = self.extract_media(metadata, position_us=position_us)
            if extracted is None:
                if svc in self._open_spans:
                    entity_id, _start_time, last_extracted = self._open_spans.pop(svc)
                    label = last_extracted.get("label", entity_id)
                    items.append(self._build_item(entity_id, label, last_extracted, active=False))
                continue

            media_type = extracted["media_type"]
            show = extracted["show"]
            album = extracted["album"]
            artist = extracted["artist"]
            title = extracted["title"]

            if media_type == "video" and show:
                entity_id = f"series:{series_slug(show)}"
                label = show
            else:
                music_name = album or artist or title or "music"
                entity_id = f"series:{series_slug(music_name)}"
                label = music_name
            extracted["label"] = label
            self._entities.add(entity_id)

            if playback_status == "Playing":
                if svc in self._open_spans:
                    old_entity_id, start_time, old_extracted = self._open_spans[svc]
                    if old_entity_id != entity_id:
                        # Different show/track: close previous span
                        old_label = old_extracted.get("label", old_entity_id)
                        items.append(
                            self._build_item(old_entity_id, old_label, old_extracted, active=False)
                        )
                        self._open_spans[svc] = (entity_id, now, extracted)
                    else:
                        self._open_spans[svc] = (entity_id, start_time, extracted)
                else:
                    self._open_spans[svc] = (entity_id, now, extracted)

                items.append(self._build_item(entity_id, label, extracted, active=True))
            else:
                # Paused / Stopped
                if svc in self._open_spans:
                    self._open_spans.pop(svc)
                    items.append(self._build_item(entity_id, label, extracted, active=False))

        return items

    @property
    def open_spans(self) -> dict[str, tuple[str, datetime, dict[str, Any]]]:
        return dict(self._open_spans)

    def clear_open_spans(self) -> None:
        self._open_spans.clear()

    @property
    def titles_dropped(self) -> int:
        return self._titles_dropped

    def entities(self) -> frozenset[str]:
        return frozenset(self._entities)

    def owns_entity(self, entity: str) -> bool:
        return entity in self._entities or entity.startswith(("series:", "media:", "track:"))

    async def forget(self, entity: str) -> int:
        if not self.owns_entity(entity):
            return 0
        entity_id = entity if entity.startswith("series:") else f"series:{series_slug(entity)}"
        for svc, (span_entity, _start_time, _extracted) in list(self._open_spans.items()):
            if span_entity == entity_id:
                del self._open_spans[svc]
        was_tracked = entity_id in self._entities
        self._entities.discard(entity_id)
        return 1 if was_tracked else 0


class MediaIngest(BaseModule):
    """MPRIS media collector and continuity tracker.

    Hosts MediaPlugin within PluginHost under the S4 plugin contract.
    """

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
        self._patterns_path = Path(config.media_patterns_path)
        self._available = True

        self._plugin = MediaPlugin(config=config, clock=self._clock, parent=self)
        self._host = PluginHost(
            bus,
            config,
            gm,
            episode_store,
            plugins=[self._plugin],
            clock=self._clock,
        )

    @property
    def _open_spans(self) -> dict[str, tuple[str, datetime, dict[str, Any]]]:
        return self._plugin.open_spans

    @property
    def _titles_dropped(self) -> int:
        return self._plugin.titles_dropped

    @property
    def _facts_written(self) -> int:
        return self._host.facts_written

    @property
    def _spans_written(self) -> int:
        return self._host.spans_written

    @property
    def _last_poll(self) -> datetime | None:
        return self._host.last_poll_for("media")

    async def initialize(self) -> None:
        if not shutil.which("busctl"):
            _log.warning("busctl not found in PATH; MediaIngest disabling itself")
            self._available = False
            return

        await self._plugin.initialize()
        await self._host.initialize()

    async def start(self) -> None:
        if not self._available or not self.config.media_tracking_enabled:
            return
        if self.is_running:
            return
        self.is_running = True
        await self._host.start()

    async def stop(self) -> None:
        self.is_running = False
        await self._host.stop()
        self._plugin.clear_open_spans()

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

    async def _run_busctl(self, *args: str, timeout_seconds: float = 5.0) -> tuple[int, str]:
        return await self._plugin.run_busctl_default(*args, timeout_seconds=timeout_seconds)

    def extract_media(
        self, metadata: dict[str, Any], position_us: int = 0
    ) -> dict[str, Any] | None:
        return self._plugin.extract_media(metadata, position_us=position_us)

    async def poll_tick(self) -> int:
        if not self._available:
            return 0
        return await self._host.poll_tick("media")

    async def forget(self, series: str) -> int:
        entity_id = series if series.startswith("series:") else f"series:{series_slug(series)}"
        await self._plugin.forget(entity_id)
        return await self._host.forget(entity_id)
