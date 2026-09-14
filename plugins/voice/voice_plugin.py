# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Voice plugin (A6.1 · domain:voice — VISION_PHASES.md).

Voice as a *sense*, text-only: every line in `voice_utterances_path` is one
captured utterance, standing in for real speech-to-text until A6.3 wires up a
microphone. A pure, side-effect-free reader like every other S4 plugin — it
never calls a model itself (`rules.md §0`: "no module imports another module.
If you want a direct call, you want a new event") — it just turns a typed
utterance into a `PluginItem` and publishes `VOICE_UTTERANCE_CAPTURED` so
`learning/voice_intent.py`'s `VoiceIntentParser` can do the (model-touching)
intent classification on the other side of the bus.

Retention is deliberately "forever, for now" (VISION_PHASES.md A6.1): nothing
here expires an utterance by age. `forget()` is the only way one goes away,
and — unlike calendar/reading, which read data some *other* tool authored —
this plugin owns the only copy of the raw text, so `forget()` also rewrites
the file to actually remove the line, not just the graph/episode-store
records derived from it (`rules.md §8`: "the user can always see and wipe
what is stored").
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neuropaca.diagnosis.app_identity import AppIdentity

from neuropaca.core.enums import EpisodeKind, EventType, NodeType, RelationType
from neuropaca.core.models import Event
from neuropaca.sensing.plugin_host import (
    PluginDescriptor,
    PluginItem,
    PluginManifest,
)

_log = logging.getLogger(__name__)

_LABEL_MAX_CHARS = 60


def _utterance_entity_id(text: str, ts: datetime | None = None) -> str:
    """Content-addressed by normalized utterance text, so re-reading the same
    file across restarts or speaking the same utterance yields the same entity
    id (calendar keys off the VEVENT `UID`; reading off the title)."""
    digest = hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()
    return f"utterance:{digest[:16]}"


def _label(text: str) -> str:
    stripped = text.strip()
    if len(stripped) <= _LABEL_MAX_CHARS:
        return stripped
    return stripped[: _LABEL_MAX_CHARS - 3] + "..."


def append_utterance(path: Path | str, text: str, ts: datetime | None = None) -> None:
    """The one way to "type" an utterance today (B12: the terminal/CLI this
    would otherwise go through was removed). A0/S1-S4's plugins all read data
    some other tool wrote; this is the first one whose input has no other
    producer yet, so tests and manual use both go through this same helper
    rather than each hand-rolling the JSONL shape."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = {"text": text, "ts": (ts or datetime.now(UTC)).isoformat()}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


class VoicePlugin:
    """Side-effect-free plugin reading typed utterances from a local JSONL file."""

    def __init__(
        self,
        utterances_path: Path | str,
        *,
        name: str = "voice",
        poll_interval: float = 5.0,
        identity: AppIdentity | None = None,
    ) -> None:
        self.name = name
        self.utterances_path = Path(utterances_path).expanduser().resolve()
        self.poll_interval = poll_interval
        self._identity = identity
        # entity_id -> its exact source line (sans newline), so `forget()` can
        # rewrite the file without one. Not persisted across restarts — same
        # as reading/calendar's own in-memory `_entities`; a restart just
        # re-derives identical entity ids from the file's content.
        self._entity_lines: dict[str, str] = {}

    def describe(self) -> PluginDescriptor:
        return PluginDescriptor(
            name=self.name,
            node_type=NodeType.CONCEPT,
            domain_hub="domain:voice",
            poll_interval_seconds=self.poll_interval,
            span_kind=EpisodeKind.VOICE_UTTERANCE_SPAN,
            manifest=PluginManifest(
                allowed_read_paths=(self.utterances_path,),
                allowed_write_paths=(self.utterances_path,),
                allowed_episode_kinds=(
                    EpisodeKind.VOICE_UTTERANCE_SPAN,
                    EpisodeKind.PLUGIN_FACT,
                ),
                allow_network=False,
                allow_subprocesses=False,
            ),
            entity_prefixes=("utterance:", "app:"),
        )

    def _parse_lines(self) -> list[tuple[str, str, datetime]]:
        """Every well-formed `(line, text, ts)` in the file, in file order.
        A malformed line is skipped and logged, never raised (matches
        calendar/reading: bad input degrades, it doesn't crash the poll)."""
        if not self.utterances_path.is_file():
            return []
        try:
            content = self.utterances_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _log.warning("VoicePlugin failed to read %s: %s", self.utterances_path, exc)
            return []

        out: list[tuple[str, str, datetime]] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj: Any = json.loads(line)
            except json.JSONDecodeError:
                _log.warning("VoicePlugin: skipping malformed line: %r", line[:80])
                continue
            if not isinstance(obj, dict):
                continue
            text = obj.get("text")
            ts_raw = obj.get("ts")
            if not isinstance(text, str) or not text.strip():
                continue
            ts: datetime | None = None
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw)
                except ValueError:
                    ts = None
            if ts is None:
                # No parseable timestamp: skip rather than default to "now" —
                # "now" would change every poll, so the same line would look
                # like a brand-new utterance forever (see module docstring).
                _log.warning("VoicePlugin: skipping line with no valid ts: %r", line[:80])
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            out.append((line, text, ts))
        return out

    async def items(self, since: datetime) -> list[PluginItem]:
        items: list[PluginItem] = []
        for line, text, ts in self._parse_lines():
            if ts <= since:
                continue

            entity_id = _utterance_entity_id(text, ts)
            node_type = NodeType.CONCEPT
            label = _label(text)

            if self._identity is not None:
                from neuropaca.learning.voice_command import try_pattern_match

                cmd = try_pattern_match(text)
                if cmd is not None and cmd.action == "open" and cmd.target:
                    canon = self._identity.resolve(cmd.target)
                    if canon and not self._identity.is_non_app(canon):
                        entity_id = f"app:{canon}"
                        node_type = NodeType.APP
                        label = self._identity.pretty(canon) or _label(text)

            self._entity_lines[entity_id] = line

            items.append(
                PluginItem(
                    entity_id=entity_id,
                    label=label,
                    node_type=node_type,
                    node_attributes={"label": label},
                    span=(ts, ts),
                    span_kind=EpisodeKind.VOICE_UTTERANCE_SPAN,
                    fact=(EpisodeKind.PLUGIN_FACT, text, {"source": "typed"}),
                    state_key=(entity_id,),
                    edges=((entity_id, "domain:voice", RelationType.PART_OF),),
                    events=(
                        Event(
                            event_type=EventType.VOICE_UTTERANCE_CAPTURED,
                            source=self.name,
                            payload={"entity_id": entity_id, "text": text},
                        ),
                    ),
                )
            )
        return items

    def entities(self) -> frozenset[str]:
        return frozenset(self._entity_lines)

    def owns_entity(self, entity: str) -> bool:
        return entity in self._entity_lines or entity.startswith("utterance:")

    async def forget(self, entity: str) -> int:
        line = self._entity_lines.pop(entity, None)
        if line is None:
            return 0
        try:
            content = self.utterances_path.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning("VoicePlugin failed to rewrite %s: %s", self.utterances_path, exc)
            return 1  # forgotten from memory even if the file rewrite failed
        remaining = [raw for raw in content.splitlines() if raw.strip() != line]
        self.utterances_path.write_text("".join(f"{raw}\n" for raw in remaining), encoding="utf-8")
        return 1


# gen-ref: e0a5ee66
