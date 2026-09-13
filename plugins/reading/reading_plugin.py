# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Reading list plugin (S4 · domain:learning).

Pure reader plugin parsing local reading lists (JSON or Markdown).
Zero external dependencies, zero network calls.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from neuropaca.core.enums import EpisodeKind, NodeType, RelationType
from neuropaca.sensing.plugin_host import (
    PluginDescriptor,
    PluginItem,
    PluginManifest,
)

_log = logging.getLogger(__name__)


def _reading_slug(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]+", "-", raw.strip()).strip("-").lower()
    return cleaned or "reading-item"


class ReadingListPlugin:
    """Side-effect-free plugin reading learning/reading items from local files."""

    def __init__(
        self,
        reading_list_path: Path | str,
        *,
        name: str = "reading_list",
        poll_interval: float = 300.0,
    ) -> None:
        self.name = name
        self.reading_list_path = Path(reading_list_path).expanduser().resolve()
        self.poll_interval = poll_interval
        self._entities: set[str] = set()

    def describe(self) -> PluginDescriptor:
        return PluginDescriptor(
            name=self.name,
            node_type=NodeType.CONCEPT,
            domain_hub="domain:learning",
            poll_interval_seconds=self.poll_interval,
            span_kind=None,
            manifest=PluginManifest(
                allowed_read_paths=(self.reading_list_path,),
                allow_network=False,
                allow_subprocesses=False,
            ),
        )

    def _parse_file(self, path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _log.warning("ReadingListPlugin failed to read %s: %s", path, exc)
            return []

        # 1. Try JSON parsing
        if path.suffix.lower() == ".json" or content.strip().startswith(("[", "{")):
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    items: list[dict[str, Any]] = []
                    for entry in data:
                        if isinstance(entry, dict) and "title" in entry:
                            items.append(
                                {
                                    "id": str(entry.get("id") or entry["title"]),
                                    "title": str(entry["title"]),
                                    "author": str(entry.get("author") or ""),
                                    "status": str(entry.get("status") or "to-read"),
                                    "progress": entry.get("progress_pct", 0),
                                }
                            )
                    return items
            except json.JSONDecodeError:
                pass

        # 2. Markdown checklist parsing: - [x] Title by Author or - [ ] Title
        items_md: list[dict[str, Any]] = []
        for line in content.splitlines():
            line_str = line.strip()
            match = re.match(r"^[-*]\s*\[([ xX])\]\s*(.+)$", line_str)
            if match:
                checked = match.group(1).lower() == "x"
                raw_item = match.group(2).strip()
                author = ""
                title = raw_item
                if " by " in raw_item:
                    title, author = raw_item.split(" by ", 1)
                items_md.append(
                    {
                        "id": title.strip(),
                        "title": title.strip(),
                        "author": author.strip(),
                        "status": "completed" if checked else "reading",
                        "progress": 100 if checked else 0,
                    }
                )
        return items_md

    async def items(self, since: datetime) -> list[PluginItem]:
        parsed = self._parse_file(self.reading_list_path)
        items: list[PluginItem] = []

        for entry in parsed:
            slug = _reading_slug(entry["id"])
            entity_id = f"reading:{slug}"
            self._entities.add(entity_id)

            title = entry["title"]
            author = entry["author"]
            status = entry["status"]
            progress = entry["progress"]

            attrs = {
                "title": title,
                "author": author,
                "status": status,
                "progress": progress,
            }
            state_key = (entity_id, title, author, status, progress)

            items.append(
                PluginItem(
                    entity_id=entity_id,
                    label=title,
                    node_type=NodeType.CONCEPT,
                    node_attributes={"label": title, "author": author, "status": status},
                    fact=(EpisodeKind.TOPIC_FACT, title, attrs),
                    state_key=state_key,
                    edges=((entity_id, "domain:learning", RelationType.PART_OF),),
                )
            )

        return items

    def entities(self) -> frozenset[str]:
        return frozenset(self._entities)

    async def forget(self, entity: str) -> int:
        slug = _reading_slug(entity.removeprefix("reading:"))
        self._entities.discard(f"reading:{slug}")
        self._entities.discard(entity)
        return 1
