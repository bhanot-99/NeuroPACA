"""L9 · the project-knowledge corpus behind the conversational ``chat`` op (B11).

``ask`` / ``diagnose`` retrieve from the behavioural graph and nothing else, so a
question *about NeuroPaca itself* — how the graph is stored, what the drive layer
does, where conversation turns live — has nowhere to match. This module is the
other half of ``chat`` retrieval: the repo's own Markdown docs, chunked by
heading, searched with the **same deliberately dumb lexical match** the graph
path uses (`GraphMemory.search_by_label`) — zero embeddings, zero inference
(rules.md §4, problems.md 1.6 spirit).

It is an L9-internal helper, not a `BaseModule`: `InterfaceLayer` builds one at
`start()` from `config.knowledge_paths` (or the default repo-doc set) and holds
it. A missing or unreadable file is skipped, never fatal — like the interactive
model, the corpus is optional and never blocks daemon startup.

The index is a snapshot taken at daemon start; editing a doc needs a restart to
take effect (Architecture.md §9).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import neuropaca

_log = logging.getLogger(__name__)

# The repo-root docs used when `config.knowledge_paths` is empty. Order is the
# tie-break order in a search result, so the most architectural docs lead.
_DEFAULT_DOCS = (
    "Architecture.md",
    "design.md",
    "PRD.md",
    "rules.md",
    "phases.md",
    "RESEARCH_DOSSIER.md",
    "problems.md",
    "pruning.md",
    "README.md",
)

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_WORD_RE = re.compile(r"[a-z0-9]+")

# Common words carry no retrieval signal — a question is mostly these, and
# letting "what" / "how" match every chunk that happens to contain them turns
# an unrelated query into a false "grounded" hit.
_STOPWORDS = frozenset(
    "the a an and or but for nor yet so of to in on at by with from into over "
    "is are was were be been being do does did done has have had how what why "
    "when where which who whom whose that this these those it its as if then "
    "than about can could should would may might will just not no does".split()
)
# A chunk must clear this to count as a real match — one incidental word is not
# enough to say the docs cover the question.
_MIN_SCORE = 2

# A single chunk over this many characters is split on paragraph boundaries so
# one giant section cannot crowd out everything else in the context budget.
_MAX_CHUNK_CHARS = 1600
# Retrieval defaults; `InterfaceLayer` passes `max_chars` from config.
_DEFAULT_LIMIT = 4


def repo_root() -> Path:
    """The installed package's repo root — ``src/neuropaca/__init__.py`` -> repo.
    Matches the daemon's systemd ``WorkingDirectory`` but does not depend on the
    process cwd."""
    return Path(neuropaca.__file__).resolve().parents[2]


def default_doc_paths() -> list[str]:
    root = repo_root()
    return [str(root / name) for name in _DEFAULT_DOCS]


@dataclass(frozen=True, slots=True)
class DocChunk:
    """One heading-delimited slice of a doc. ``crumb`` is the ``H1 → H2`` trail
    to this section; ``text`` is the section body (heading line excluded)."""

    doc: str
    crumb: str
    text: str

    @property
    def cite(self) -> str:
        return f"{self.doc} → {self.crumb}" if self.crumb else self.doc

    def snippet(self, limit: int = 240) -> str:
        flat = " ".join(self.text.split())
        return flat if len(flat) <= limit else flat[:limit].rsplit(" ", 1)[0] + "…"


def _split_long(text: str) -> list[str]:
    """Break a chunk that exceeds `_MAX_CHUNK_CHARS` on blank lines, packing
    paragraphs greedily. Short chunks pass straight through."""
    if len(text) <= _MAX_CHUNK_CHARS:
        return [text]
    out: list[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if buf and len(buf) + len(para) + 2 > _MAX_CHUNK_CHARS:
            out.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out


def _chunk_markdown(doc: str, body: str) -> list[DocChunk]:
    """Split one doc into `(crumb, text)` chunks at heading lines, ignoring
    headings inside fenced code blocks."""
    crumbs: dict[int, str] = {}
    cur_crumb = ""
    lines: list[str] = []
    chunks: list[DocChunk] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(lines).strip()
        lines.clear()
        if not text:
            return
        for piece in _split_long(text):
            chunks.append(DocChunk(doc=doc, crumb=cur_crumb, text=piece))

    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            lines.append(line)
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if m is None:
            lines.append(line)
            continue
        flush()
        level = len(m.group(1))
        title = m.group(2).strip()
        crumbs[level] = title
        for deeper in [lv for lv in crumbs if lv > level]:
            del crumbs[deeper]
        cur_crumb = " → ".join(crumbs[lv] for lv in sorted(crumbs))
    flush()
    return chunks


class KnowledgeIndex:
    """A flat list of `DocChunk`s with a lexical `search`. Immutable after build."""

    def __init__(self, chunks: Sequence[DocChunk]) -> None:
        self._chunks: tuple[DocChunk, ...] = tuple(chunks)

    def __len__(self) -> int:
        return len(self._chunks)

    @property
    def docs(self) -> list[str]:
        return sorted({c.doc for c in self._chunks})

    @classmethod
    def from_paths(cls, paths: Iterable[str]) -> KnowledgeIndex:
        chunks: list[DocChunk] = []
        seen = 0
        for raw in paths:
            p = Path(raw)
            try:
                body = p.read_text("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                _log.warning("knowledge: skipping %s (%s)", p, exc)
                continue
            seen += 1
            chunks.extend(_chunk_markdown(p.name, body))
        _log.info("knowledge index: %d chunks from %d docs", len(chunks), seen)
        return cls(chunks)

    def search(
        self, query: str, *, limit: int = _DEFAULT_LIMIT, max_chars: int = 2400
    ) -> list[DocChunk]:
        """Rank chunks by term overlap on the non-stopword query terms — heading
        hits weigh 3x, body hits 1x (capped), a whole-phrase substring +2, a
        filename hit +1. A chunk under `_MIN_SCORE` is dropped (one incidental
        word does not mean the docs cover the question); results fill `max_chars`
        and cap at `limit`. Empty list on no real match."""
        q = query.strip().lower()
        words = {w for w in _WORD_RE.findall(q) if len(w) >= 3 and w not in _STOPWORDS}
        if not words or not self._chunks:
            return []

        scored: list[tuple[int, int, DocChunk]] = []
        for order, chunk in enumerate(self._chunks):
            crumb_l = chunk.crumb.lower()
            text_l = chunk.text.lower()
            doc_l = chunk.doc.lower()
            score = 0
            for w in words:
                if w in crumb_l:
                    score += 3
                if w in doc_l:
                    score += 1
                score += min(text_l.count(w), 3)
            if len(q) >= 4 and (q in text_l or q in crumb_l):
                score += 2
            if score >= _MIN_SCORE:
                scored.append((-score, order, chunk))

        scored.sort()
        picked: list[DocChunk] = []
        used = 0
        for _, _, chunk in scored:
            if picked and (used + len(chunk.text) > max_chars or len(picked) >= limit):
                break
            picked.append(chunk)
            used += len(chunk.text)
        return picked
