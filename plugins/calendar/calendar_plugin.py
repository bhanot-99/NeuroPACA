# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Calendar plugin (S4 · domain:meetings).

Pure standard-library reader for local RFC 5545 .ics calendar files.
Zero external dependencies (no icalendar library), zero network calls.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

from neuropaca.core.enums import EpisodeKind, NodeType, RelationType
from neuropaca.sensing.plugin_host import (
    PluginDescriptor,
    PluginItem,
    PluginManifest,
)

_log = logging.getLogger(__name__)


def _event_slug(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]+", "-", raw.strip()).strip("-").lower()
    return cleaned or "meeting"


def _parse_ics_datetime(val: str) -> datetime | None:
    """Parse RFC 5545 date or date-time values into UTC datetimes."""
    clean = val.strip().replace("-", "").replace(":", "")
    # Format: 20260913T100000Z
    if "T" in clean:
        parts = clean.split("T", 1)
        date_part = parts[0]
        time_part = parts[1].rstrip("Z")
        if len(date_part) == 8 and len(time_part) >= 6:
            year, month, day = int(date_part[:4]), int(date_part[4:6]), int(date_part[6:8])
            hour, minute, second = (
                int(time_part[:2]),
                int(time_part[2:4]),
                int(time_part[4:6]),
            )
            return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    elif len(clean) == 8 and clean.isdigit():
        # All-day date format: YYYYMMDD
        year, month, day = int(clean[:4]), int(clean[4:6]), int(clean[6:8])
        return datetime.combine(datetime(year, month, day, tzinfo=UTC).date(), time.min, tzinfo=UTC)
    return None


class CalendarPlugin:
    """Side-effect-free plugin reading meetings from local .ics files."""

    def __init__(
        self,
        calendar_path: Path | str,
        *,
        name: str = "calendar",
        poll_interval: float = 300.0,
    ) -> None:
        self.name = name
        self.calendar_path = Path(calendar_path).expanduser().resolve()
        self.poll_interval = poll_interval
        self._entities: set[str] = set()

    def describe(self) -> PluginDescriptor:
        return PluginDescriptor(
            name=self.name,
            node_type=NodeType.CONCEPT,
            domain_hub="domain:meetings",
            poll_interval_seconds=self.poll_interval,
            span_kind=EpisodeKind.MEETING_SPAN,
            manifest=PluginManifest(
                allowed_read_paths=(self.calendar_path,),
                allowed_episode_kinds=(EpisodeKind.MEETING_SPAN, EpisodeKind.PLUGIN_FACT),
                allow_network=False,
                allow_subprocesses=False,
            ),
            entity_prefixes=("event:", "calendar:", "meeting:"),
        )

    def _unfold_lines(self, text: str) -> list[str]:
        """RFC 5545 line unfolding: lines starting with space or tab continue previous line."""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        unfolded: list[str] = []
        for line in normalized.splitlines():
            if line.startswith((" ", "\t")) and unfolded:
                unfolded[-1] += line[1:]
            else:
                unfolded.append(line)
        return unfolded

    def _parse_ics(self, path: Path) -> list[dict[str, Any]]:
        """Parse VEVENT blocks from an .ics file into a list of event dictionaries."""
        if not path.is_file():
            return []

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _log.warning("CalendarPlugin failed to read %s: %s", path, exc)
            return []

        lines = self._unfold_lines(content)
        events: list[dict[str, Any]] = []
        in_vevent = False
        current: dict[str, str] = {}

        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue

            if line_str == "BEGIN:VEVENT":
                in_vevent = True
                current = {}
                continue
            if line_str == "END:VEVENT":
                if in_vevent and current:
                    events.append(dict(current))
                in_vevent = False
                current = {}
                continue

            if in_vevent:
                if ":" in line_str:
                    prop, val = line_str.split(":", 1)
                    # Property may have parameters, e.g. DTSTART;VALUE=DATE:20260913
                    prop_name = prop.split(";", 1)[0].upper()
                    current[prop_name] = val.strip()

        parsed: list[dict[str, Any]] = []
        for ev in events:
            uid = ev.get("UID")
            summary = ev.get("SUMMARY", "Meeting")
            dtstart_raw = ev.get("DTSTART")
            dtend_raw = ev.get("DTEND")
            location = ev.get("LOCATION", "")
            description = ev.get("DESCRIPTION", "")

            if not uid or not dtstart_raw:
                continue

            start_dt = _parse_ics_datetime(dtstart_raw)
            if start_dt is None:
                continue

            end_dt = _parse_ics_datetime(dtend_raw) if dtend_raw else start_dt
            if end_dt is None or end_dt < start_dt:
                end_dt = start_dt

            parsed.append(
                {
                    "uid": uid,
                    "summary": summary,
                    "start": start_dt,
                    "end": end_dt,
                    "location": location,
                    "description": description,
                }
            )

        return parsed

    async def items(self, since: datetime) -> list[PluginItem]:
        files: list[Path] = []
        if self.calendar_path.is_file():
            files.append(self.calendar_path)
        elif self.calendar_path.is_dir():
            files.extend(sorted(self.calendar_path.glob("*.ics")))

        items: list[PluginItem] = []
        for p in files:
            events = self._parse_ics(p)
            for ev in events:
                entity_id = f"event:{_event_slug(ev['uid'])}"
                self._entities.add(entity_id)

                summary = ev["summary"]
                start_dt: datetime = ev["start"]
                end_dt: datetime = ev["end"]
                location = ev["location"]
                description = ev["description"]

                attrs = {
                    "summary": summary,
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "location": location,
                    "description": description,
                }
                state_key = (
                    entity_id,
                    summary,
                    start_dt.isoformat(),
                    end_dt.isoformat(),
                    location,
                )

                items.append(
                    PluginItem(
                        entity_id=entity_id,
                        label=summary,
                        node_type=NodeType.CONCEPT,
                        node_attributes={"label": summary, "location": location},
                        span=(start_dt, end_dt),
                        span_kind=EpisodeKind.MEETING_SPAN,
                        fact=(EpisodeKind.PLUGIN_FACT, summary, attrs),
                        state_key=state_key,
                        edges=((entity_id, "domain:meetings", RelationType.PART_OF),),
                    )
                )

        return items

    def entities(self) -> frozenset[str]:
        return frozenset(self._entities)

    def owns_entity(self, entity: str) -> bool:
        return entity in self._entities or entity.startswith(("event:", "calendar:", "meeting:"))

    async def forget(self, entity: str) -> int:
        if not self.owns_entity(entity):
            return 0
        slug = _event_slug(entity.removeprefix("event:"))
        event_entity = f"event:{slug}"
        was_tracked = event_entity in self._entities or entity in self._entities
        self._entities.discard(event_entity)
        self._entities.discard(entity)
        return 1 if was_tracked else 0


# gen-ref: 9be7a9a3
