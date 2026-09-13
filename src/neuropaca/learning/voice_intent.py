# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""A6.1 · the `VoiceIntent` data model (VISION_PHASES.md).

An extractive classification of one captured utterance — never a sentence,
never an open-ended "action"/"target"/"slots" the model free-generates.
`rules.md §4.1`: "the model is a decision gate, not a log parser." This
project's tiny loop model already proved (`problems.md` 1.13, `learning/
insight.py`) that free-form extraction over graph context fails; there is no
reason to expect an open-ended "what does the user want" schema would fare
better, so A6.1 asks the same two kinds of question L4/L6 already ask:

    {"cited_node_id": "n2" | null, "voice_intent": "action_request" | ...}

- `cited_node_id` — the one known graph node this utterance is most likely
  about, by local alias; `null` when it isn't clearly about any candidate
  offered (a genuine "don't know", not a discard — see below).
- `voice_intent` — one of `VOICE_INTENT_CATEGORIES`.

Unlike an L4 `Insight`, a null `cited_node_id` here is *not* a full abstain:
an utterance always genuinely happened, so there is nothing to abstain from
the way L4 abstains from "was this signal worth an insight at all". What can
legitimately be uncertain is *which* known node it relates to (node -> null)
and *how confidently* it classifies (category -> `"other"`, the catch-all
that plays the same role a null would for a forced-choice field — `rules.md
§4.1`'s "every schema has an abstain path" is satisfied by these two, not by
discarding the classification outright).
"""

from __future__ import annotations

from dataclasses import dataclass

# The full closed set A6.1's grammar may emit. `"other"` is the practical
# abstain for a field that (unlike `cited_node_id`) cannot itself be null —
# see the module docstring.
VOICE_INTENT_CATEGORIES: tuple[str, ...] = (
    "action_request",
    "reminder",
    "question",
    "observation",
    "other",
)


@dataclass(frozen=True, slots=True)
class VoiceIntent:
    """One utterance's extractive classification, ready to enrich the
    episode `plugins/voice/voice_plugin.py` already wrote for it."""

    category: str
    cited_node_id: str | None
    raw_text: str

    def __post_init__(self) -> None:
        if self.category not in VOICE_INTENT_CATEGORIES:
            raise ValueError(f"unknown voice intent category: {self.category!r}")


# gen-ref: 5d00ef8a
