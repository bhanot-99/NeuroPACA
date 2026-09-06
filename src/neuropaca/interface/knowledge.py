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
import math
import re
from collections import Counter
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

# Crude suffix stripping so "prune" / "pruned" / "pruning" collapse to one token
# on both sides of the match. Not a real stemmer — order matters (longest
# first), short words are left alone, and a trailing "e" is trimmed last so the
# bare-verb form lands on the same stem as its inflections.
_SUFFIXES = ("ings", "ing", "edly", "ers", "ed", "es", "er", "s")


def _stem(word: str) -> str:
    for suf in _SUFFIXES:
        if len(word) - len(suf) >= 3 and word.endswith(suf):
            word = word[: -len(suf)]
            break
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(w) for w in _WORD_RE.findall(text.lower()) if len(w) >= 3]


# Common words carry no retrieval signal — a question is mostly these, and
# letting "what" / "how" match every chunk that happens to contain them turns
# an unrelated query into a false "grounded" hit.
_STOPWORDS = frozenset(
    "the a an and or but for nor yet so of to in on at by with from into over "
    "is are was were be been being do does did done has have had how what why "
    "when where which who whom whose that this these those it its as if then "
    "than about can could should would may might will just not no does "
    "tell explain show give list describe say said talk about here there "
    "like today now want need get got make made use used know think see "
    "please you your yours our ours their they them his her hers "
    "some any many much more most less few all each every".split()
)
# Words that appear in more than this fraction of all chunks carry no
# discriminating signal for THIS corpus (e.g. "graph", "layer", "system") and
# are dropped from the query — a corpus-adaptive stoplist on top of the fixed
# one above. If every query word is this generic the query is too vague to
# ground, and `search` returns nothing.
_GENERIC_DF_FRACTION = 0.33
# A chunk must clear this score to count as a real match. IDF weights are
# normalised to [0, 1] (see `_idf`), so a body hit on a distinctive word is
# ~1.0, a heading hit ~3.0, and the threshold is stable regardless of how many
# docs are in the corpus. Tuned against a battery of real-vs-off-topic questions
# (tests/test_knowledge.py); the retriever biases toward recall, since a
# tangential hit only costs a missing "general knowledge" flag on the answer
# while a miss makes a real question unanswerable.
_MIN_SCORE = 1.0

# A single chunk over this many characters is split on paragraph boundaries so
# one giant section cannot crowd out everything else in the context budget. A
# lone paragraph past this is hard-sliced — a runaway chunk must not blow the
# model's context window.
_MAX_CHUNK_CHARS = 1600
# A doc file larger than this is almost certainly not prose — skip it rather than
# chunk a data dump into the corpus.
_MAX_DOC_BYTES = 1_000_000
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
        # A single paragraph longer than the cap is hard-sliced on whitespace —
        # rare in real docs, but it must not reach the model whole.
        while len(para) > _MAX_CHUNK_CHARS:
            head = para[:_MAX_CHUNK_CHARS].rsplit(" ", 1)[0] or para[:_MAX_CHUNK_CHARS]
            if buf:
                out.append(buf)
                buf = ""
            out.append(head)
            para = para[len(head) :].lstrip()
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
        # Per-chunk word bag + heading word set, tokenised once. `search` scores
        # from these — a *word-boundary* match, so "bread" no longer hits
        # "breadcrumb" and "cat" no longer hits "concatenate".
        self._body_words: list[Counter[str]] = []
        self._crumb_words: list[frozenset[str]] = []
        df: Counter[str] = Counter()
        for chunk in self._chunks:
            body = Counter(_tokens(chunk.text))
            crumb = frozenset(_tokens(chunk.crumb))
            self._body_words.append(body)
            self._crumb_words.append(crumb)
            df.update(set(body) | crumb)
        self._df = df

    def _idf(self, word: str) -> float:
        """Inverse document frequency normalised to ~(0, 1] — corpus-size
        independent, so `_MIN_SCORE` means the same thing for a 10-doc set and a
        1000-doc set. ~1.0 for a word absent or nearly so. A word that saturates
        the corpus (e.g. "graph", "layer") drops to a near-zero weight: it still
        breaks ties toward the chunk actually about it, but cannot ground a match
        on its own. A small floor otherwise, so a common-but-not-saturating word
        still counts and a one-chunk index is not dead."""
        n = len(self._chunks) or 1
        df = self._df.get(word, 0)
        if n >= 15 and df > _GENERIC_DF_FRACTION * n:
            return 0.08
        return max(0.34, 1.0 - math.log1p(df) / math.log1p(n + 1))

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
                size = p.stat().st_size
                if size > _MAX_DOC_BYTES:
                    _log.warning("knowledge: skipping %s (%d bytes — not prose)", p, size)
                    continue
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
        """Rank chunks by IDF-weighted, **word-boundary** term overlap on the
        stemmed non-stopword query terms — a heading hit weighs 3x a body hit
        (body hits capped at 3), a whole-phrase substring adds 2·(mean IDF), a
        filename hit 1x. A match must also clear `_MIN_SCORE` *and* be specific
        (≥2 distinct query words, or a heading hit, or a phrase hit, or one word
        the chunk repeats). Results fill `max_chars` and cap at `limit`. Empty
        list on no real match."""
        q = query.strip().lower()
        raw = [w for w in _WORD_RE.findall(q) if len(w) >= 3 and w not in _STOPWORDS]
        words = {_stem(w) for w in raw}
        if not words or not self._chunks:
            return []
        idf = {w: self._idf(w) for w in words}
        # If *every* query word saturates the corpus, the question is too vague
        # to ground (`_idf` gives those ~0.08).
        if all(v <= 0.1 for v in idf.values()):
            return []

        scored: list[tuple[float, int, DocChunk]] = []
        for order, chunk in enumerate(self._chunks):
            body = self._body_words[order]
            crumb = self._crumb_words[order]
            doc_l = chunk.doc.lower()
            score = 0.0
            hit_words: set[str] = set()  # only *discriminating* words (idf > 0.1)
            crumb_hit = False
            about = False  # a discriminating hit word appears more than once
            for w in words:
                real = idf[w] > 0.1
                if w in crumb:
                    score += 3 * idf[w]
                    if real:
                        crumb_hit = True
                        hit_words.add(w)
                if w in doc_l:
                    score += idf[w]
                tf = body.get(w, 0)
                if tf:
                    score += min(tf, 3) * idf[w]
                    if real:
                        hit_words.add(w)
                        if tf >= 2:
                            about = True
            phrase = len(q) >= 4 and q in chunk.text.lower()
            if phrase:
                score += 2 * (sum(idf.values()) / len(idf))
            # A real match is: ≥2 distinct query words hitting the chunk, OR a
            # heading hit, OR the exact phrase, OR one word repeated in the body.
            # A single word appearing once is incidental — dropped. The bias here
            # is toward recall: a tangential hit costs only a missing "general
            # knowledge" flag, whereas a miss makes a real question unanswerable.
            specific = len(hit_words) >= 2 or crumb_hit or phrase or about
            if score >= _MIN_SCORE and specific:
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
