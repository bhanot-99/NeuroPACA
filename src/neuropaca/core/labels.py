# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B18 · one labeling system — labels are rendered, never stored.

In plain words: a generated node (an L4 insight, an L6 idle thought, an L8
probe) stores *what it is about* — a `LabelSpec` naming other nodes **by id** —
and its readable name is computed from that on demand. Three rules follow:

- **Names heal themselves.** A spec refers to `app:brave`, never to the text
  "brave-browser", so a B17 rename changes every label that points at it.
- **Identity is the facts.** `fingerprint(spec)` is derived from kind, facet and
  (canonical) refs only, and the node id is derived from the fingerprint
  (`fact_id`). The same fact written twice — even across a daemon restart — is
  the same id, so it is reinforced instead of duplicated.
- **Text never flows between nodes.** A spec may point at another node only
  through `refs`; nothing copies another node's label into its own.

Every drawn caption, panel title, terminal line and prompt context goes through
`display()`: `render()` for spec'd nodes, `leaf_name()` for everything else.
This module is pure **stdlib** — no graph, no I/O, no `neuropaca` imports — so
the stand-alone graph window (system python, never imports the daemon package)
loads this one file by path and renders exactly what the daemon renders.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal

Mode = Literal["short", "full"]
NameOf = Callable[[str], str]
#: `ref id -> (label, spec)` of a node, or None when it is not in the graph.
Lookup = Callable[[str], "tuple[str, LabelSpec | None] | None"]

#: A drawn caption never exceeds this — the window's label budget. Inside a
#: short caption each *name* is clipped first, so the distinguishing tail
#: ("· via learning") always survives a long app name.
SHORT_MAX = 34
SHORT_NAME_MAX = 16


class LabelKind(StrEnum):
    INSIGHT = "insight"  # L4 — facet "<category>/<signal>", refs (cited,)
    THOUGHT = "thought"  # L6 — facet "<template key>", refs (x,) or (x, y)
    PROBE = "probe"  # L8 — facet "summary/<L#>.<cause>" or "source/<layer>", refs (trigger,)


#: The durable id prefix per kind — apoptosis, TTL pruning and L9 surfacing all
#: key on these (see memory: graph drops ad-hoc attributes, ids persist).
KIND_PREFIX: dict[LabelKind, str] = {
    LabelKind.INSIGHT: "insight:",
    LabelKind.THOUGHT: "idle:",
    LabelKind.PROBE: "ephemeral:",
}

#: L6's closed question set (D-13). The model picks a key; the text is ours.
THOUGHT_TEMPLATES: dict[str, str] = {
    "how_does_x_affect_y": "How does {x} affect {y}?",
    "what_connects_x_and_y": "What connects {x} and {y}?",
    "what_changed_in_x": "What changed in {x} recently?",
    "why_is_x_active": "Why has {x} been active so often?",
}
_THOUGHT_SHORT: dict[str, str] = {
    "how_does_x_affect_y": "{x} → {y}?",
    "what_connects_x_and_y": "{x} ↔ {y}?",
    "what_changed_in_x": "{x} · changed?",
    "why_is_x_active": "{x} · why active?",
}


#: V-7 · hard cap on a spec's stored `text`. Every node record carries its spec,
#: so an unbounded payload would bloat the whole graph file; a question or a
#: briefing line is a sentence, not a document.
TEXT_MAX = 512


@dataclass(frozen=True, slots=True)
class LabelSpec:
    """What a generated node is about. `refs` are node ids, in meaning order
    (subject first). `value` is the one number worth showing and is **not**
    part of identity: pressure 1.43 and 1.59 on Brave are the same probe.

    `text` (V-7) is an optional free-text payload — likewise **not** part of
    identity, and never what the label renders from. B18's rule stands: a
    label is rendered from `{kind, facet, refs}` so a rename heals every label
    at once. `text` is the complementary record B18 left no room for: what was
    actually said *at the time*, which a later rename must NOT rewrite, plus
    somewhere for a thought to carry a payload the closed facet vocabulary
    cannot express (the briefing will need this). Clipped to `TEXT_MAX`;
    blank is normalised to `None` so "no text" has one representation.
    """

    kind: LabelKind
    refs: tuple[str, ...]
    facet: str = ""
    value: float | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        cleaned = _clean_text(self.text)
        if cleaned != self.text:
            object.__setattr__(self, "text", cleaned)  # frozen: normalise in place

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "kind": str(self.kind),
            "refs": list(self.refs),
            "facet": self.facet,
            "value": self.value,
        }
        # Only when there is one: an extra null on every probe/insight/thought
        # record is pure weight in a file that holds thousands of them.
        if self.text is not None:
            record["text"] = self.text
        return record

    @classmethod
    def from_record(cls, raw: Any) -> LabelSpec | None:
        """Tolerant reader: anything that is not a well-formed spec is `None`
        (the node then renders by its label, like any leaf)."""
        if not isinstance(raw, Mapping):
            return None
        try:
            kind = LabelKind(raw["kind"])
            refs = tuple(str(r) for r in raw.get("refs", ()))
            value = raw.get("value")
            text = raw.get("text")
            return cls(
                kind=kind,
                refs=refs,
                facet=str(raw.get("facet", "")),
                value=None if value is None else float(value),
                text=None if text is None else str(text),
            )
        except (KeyError, TypeError, ValueError):
            return None


# --------------------------------------------------------------------------- #
# identity                                                                     #
# --------------------------------------------------------------------------- #
def _clean_text(raw: str | None) -> str | None:
    """Normalise a spec's free text: collapse whitespace, clip to `TEXT_MAX`,
    and turn anything empty into `None`."""
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    if not text:
        return None
    return text[:TEXT_MAX] if len(text) > TEXT_MAX else text


def fingerprint(spec: LabelSpec) -> str:
    """64-bit hex identity of a fact: kind + facet + refs. A thought's refs are
    unordered ("how does A affect B" and "… B affect A" are one open question);
    the others keep order. `value` and `text` are excluded on purpose — the same
    question asked twice in different words is one open question, and keeping
    them out means V-7 added no new fingerprint input, so every id in an
    existing graph is unchanged."""
    refs = sorted(spec.refs) if spec.kind is LabelKind.THOUGHT else list(spec.refs)
    key = f"{spec.kind}|{spec.facet}|{','.join(refs)}"
    return hashlib.blake2b(key.encode("utf-8"), digest_size=8).hexdigest()


#: Thought templates meaningless without two distinct refs.
RELATIONAL_THOUGHTS: frozenset[str] = frozenset({"how_does_x_affect_y", "what_connects_x_and_y"})


def is_well_formed(spec: LabelSpec) -> bool:
    """A fact must name at least one node; a relational thought needs two
    distinct ones (a rename can collapse "brave -> brave-browser" to one)."""
    if not spec.refs:
        return False
    if spec.kind is LabelKind.THOUGHT and spec.facet in RELATIONAL_THOUGHTS:
        return len(set(spec.refs)) >= 2
    return True


def fact_id(spec: LabelSpec) -> str:
    """The node id for a fact. Derived, so a duplicate is impossible by
    construction — no side index to lose or corrupt."""
    return f"{KIND_PREFIX[spec.kind]}{fingerprint(spec)}"


# --------------------------------------------------------------------------- #
# names                                                                        #
# --------------------------------------------------------------------------- #
_ACRONYMS: frozenset[str] = frozenset(
    {"ai", "cli", "db", "ide", "mcp", "os", "vs", "ui", "ux", "ram", "cpu"}
)
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


def pretty_slug(slug: str) -> str:
    """`cosmic-files` -> "Cosmic Files", `mental_models` -> "Mental Models",
    `code` -> "VS Code". Word by word; known acronyms upper-cased."""
    key = slug.strip().lower()
    if key in _PRETTY_WORDS:
        return _PRETTY_WORDS[key]
    out: list[str] = []
    for w in (w for w in re.split(r"[-_\s]+", key) if w):
        if w in _ACRONYMS:
            out.append(w.upper())
        else:
            out.append(_PRETTY_WORDS.get(w, w[:1].upper() + w[1:]))
    return " ".join(out)


def leaf_name(node_id: str, label: str) -> str:
    """The name of a node that has no spec — a hub, an app, a file, a legacy
    generated node whose label could not be parsed."""
    if node_id == "YOU":
        return "You"
    prefix, _, bare = node_id.partition(":")
    if prefix == "domain":
        return pretty_slug(bare)
    if prefix in ("app", "webapp"):
        return pretty_slug(label or bare)
    first = label.strip().splitlines()[0].strip() if label.strip() else ""
    return first or node_id


def _words(token: str) -> str:
    return token.replace("_", " ").replace(".", " ")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render(spec: LabelSpec, name_of: NameOf, mode: Mode = "full") -> str:
    """The one template table. `name_of(ref_id)` returns the ref's *current*
    display name — which is what makes a rename heal every label at once."""
    names = [name_of(r) for r in spec.refs] or ["?"]
    if mode == "short":
        names = [_clip(n, SHORT_NAME_MAX) for n in names]
    x = names[0]
    y = names[1] if len(names) > 1 else ""
    v = spec.value

    text: str
    if spec.kind is LabelKind.INSIGHT:
        category, _, signal = spec.facet.partition("/")
        if mode == "short":
            text = f"{x} · {category}"
        else:
            text = f"{category.capitalize()} on {x} ({_words(signal) or 'signal'})"
            if v is not None:
                text += f" · confidence {v:.2f}"
    elif spec.kind is LabelKind.THOUGHT:
        table = _THOUGHT_SHORT if mode == "short" else THOUGHT_TEMPLATES
        template = table.get(spec.facet, "{x} ↔ {y}?" if y else "{x}?")
        text = template.format(x=x, y=y)
    else:  # PROBE
        group, _, detail = spec.facet.partition("/")
        if group == "summary":
            cause = _words(detail)
            if mode == "short":
                text = f"{x} · pressure {v:.1f}" if v is not None else f"{x} · pressure"
            else:
                amount = f" {v:.2f}" if v is not None else ""
                text = f"Pressure{amount} on {x} ({cause})" if cause else f"Pressure{amount} on {x}"
        elif group == "source":
            text = (
                f"{x} · via {_words(detail)}"
                if mode == "short"
                else f"{_words(detail).capitalize()} corroborated the pressure on {x}"
            )
        else:
            text = f"{x} · {_words(spec.facet) or 'probe'}"
    return _clip(text, SHORT_MAX) if mode == "short" else text


def ref_namer(lookup: Lookup) -> NameOf:
    """How a ref is named *inside* another label — one level deep, never
    recursive: a spec'd ref renders its short form over leaf names only. The
    graph and the window both build their `name_of` here."""

    def leaf(ref: str) -> str:
        hit = lookup(ref)
        return leaf_name(ref, hit[0] if hit else "")

    def name_of(ref: str) -> str:
        hit = lookup(ref)
        if hit is None:
            return leaf_name(ref, "")
        label, spec = hit
        return render(spec, leaf, "short") if spec is not None else leaf_name(ref, label)

    return name_of


def display(
    node_id: str, label: str, spec: LabelSpec | None, name_of: NameOf, mode: Mode = "full"
) -> str:
    """The single entry point every surface uses for any node."""
    if spec is not None:
        return render(spec, name_of, mode)
    name = leaf_name(node_id, label)
    return _clip(name, SHORT_MAX + 16) if mode == "short" else name


def disambiguate(captions: Mapping[str, str], hints: Mapping[str, str]) -> dict[str, str]:
    """Make on-screen captions distinct. Only where two captions are still equal,
    append the caller's hint (a time, a value); a residual tie gets `#n`. Same
    idea as an editor showing the parent folder for two tabs that share a name."""
    out = dict(captions)
    groups: dict[str, list[str]] = {}
    for node_id, caption in captions.items():
        groups.setdefault(caption, []).append(node_id)
    for caption, members in groups.items():
        if len(members) < 2:
            continue
        for node_id in members:
            hint = hints.get(node_id, "")
            out[node_id] = f"{caption} · {hint}" if hint else caption
        seen: dict[str, int] = {}
        for node_id in sorted(members):
            n = seen.get(out[node_id], 0) + 1
            seen[out[node_id]] = n
            if n > 1:
                out[node_id] = f"{out[node_id]} #{n}"
    return out


# --------------------------------------------------------------------------- #
# schema v4 -> v5: the four legacy templates                                   #
# --------------------------------------------------------------------------- #
DROP: Final[Literal["drop"]] = "drop"
_LEGACY_INSIGHT = re.compile(r"^(\w+): (\w+) implicates (\S+)$")
_LEGACY_SUMMARY = re.compile(r"^pressure ([\d.]+) on (\S+): (L\d) (\w+)")
_LEGACY_SOURCE = re.compile(r"^(\w+) corroborated (\S+)$")
_LEGACY_THOUGHT = [
    (key, re.compile("^" + re.escape(t).replace(r"\{x\}", "(.+)").replace(r"\{y\}", "(.+)") + "$"))
    for key, t in THOUGHT_TEMPLATES.items()
]


def parse_legacy(
    node_id: str, label: str, ref_for_name: Callable[[str], str | None]
) -> LabelSpec | Literal["drop"] | None:
    """Recover the spec a pre-B18 node would have had from its frozen label.

    `None` — not a generated node, or text we do not recognise (kept verbatim);
    `"drop"` — a thought that quoted *another node's label* instead of naming a
    node (issue 4); it cannot be expressed as refs and is noise by construction.
    `ref_for_name` maps a display name in a thought back to a node id, or `None`.
    """
    if node_id.startswith("insight:"):
        if m := _LEGACY_INSIGHT.match(label):
            return LabelSpec(LabelKind.INSIGHT, (m[3],), f"{m[1]}/{m[2]}")
        return None
    if node_id.startswith("ephemeral:"):
        if m := _LEGACY_SUMMARY.match(label):
            return LabelSpec(LabelKind.PROBE, (m[2],), f"summary/{m[3]}.{m[4]}", float(m[1]))
        if m := _LEGACY_SOURCE.match(label):
            return LabelSpec(LabelKind.PROBE, (m[2],), f"source/{m[1]}")
        return None
    if node_id.startswith("idle:"):
        for key, pattern in _LEGACY_THOUGHT:
            if m := pattern.match(label):
                refs = [ref_for_name(g.strip()) for g in m.groups()]
                if any(r is None for r in refs):
                    return DROP
                return LabelSpec(LabelKind.THOUGHT, tuple(r for r in refs if r), key)
        return None
    return None


# gen-ref: b18-labels
