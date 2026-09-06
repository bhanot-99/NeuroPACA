"""B11 · the project-knowledge corpus behind the `chat` op (interface/knowledge.py).

Zero inference, zero embeddings — a lexical index. No test reads a real model.
"""

from __future__ import annotations

from neuropaca.interface.knowledge import (
    KnowledgeIndex,
    _split_long,
    _stem,
    default_doc_paths,
    repo_root,
)

_DOC = """\
# Project

Intro paragraph, no heading of its own.

## Graph storage

The behavioural graph is one JSON file, written atomically via os.replace.
GraphMemory serialises every write behind a single asyncio.Lock. The graph
is stored on disk; the graph is loaded once at start.

### Pruning

Low-score nodes are pruned on a schedule so the graph stays bounded. Pruning
runs during idle. The pruner drops the weakest nodes.

## Monitoring

The sensing layer polls cold system metrics every 60 seconds.

```python
# a fenced block whose "# heading" must NOT split the chunk
def collect(): ...
```
"""


def _index(tmp_path, body: str = _DOC, name: str = "notes.md") -> KnowledgeIndex:
    p = tmp_path / name
    p.write_text(body)
    return KnowledgeIndex.from_paths([str(p)])


# --------------------------------------------------------------------------- chunking


def test_chunks_split_on_headings_and_carry_a_breadcrumb(tmp_path) -> None:
    idx = _index(tmp_path)
    crumbs = {c.crumb for c in idx._chunks}
    assert "Project → Graph storage" in crumbs
    assert "Project → Graph storage → Pruning" in crumbs
    assert "Project → Monitoring" in crumbs


def test_a_hash_inside_a_fence_does_not_start_a_chunk(tmp_path) -> None:
    idx = _index(tmp_path)
    monitoring = next(c for c in idx._chunks if c.crumb == "Project → Monitoring")
    assert "def collect()" in monitoring.text


def test_an_over_long_paragraph_is_hard_sliced(tmp_path) -> None:
    huge_para = "word " * 1000  # ~5000 chars, no blank lines
    idx = _index(tmp_path, f"# Doc\n\n## Big\n\n{huge_para}\n")
    big = [c for c in idx._chunks if c.crumb == "Doc → Big"]
    assert len(big) > 1
    assert all(len(c.text) <= 1700 for c in big)


def test_split_long_is_a_noop_under_the_cap() -> None:
    assert _split_long("short") == ["short"]


def test_a_missing_file_is_skipped_not_fatal(tmp_path) -> None:
    good = tmp_path / "ok.md"
    good.write_text(
        "# Ok\n\n## Widgets\n\nThe widget subsystem builds a widget from a gizmo.\n\n"
        "## Other\n\nUnrelated filler paragraph about nothing in particular.\n"
    )
    idx = KnowledgeIndex.from_paths([str(tmp_path / "nope.md"), str(good)])
    assert len(idx) >= 1
    assert idx.search("how does the widget subsystem work")


def test_an_oversized_file_is_skipped(tmp_path) -> None:
    big = tmp_path / "big.md"
    big.write_text("# Big\n\n" + "data " * 300_000)  # > 1 MB
    small = tmp_path / "small.md"
    small.write_text("# Small\n\nthe quarantine directory holds quarantine backups\n")
    idx = KnowledgeIndex.from_paths([str(big), str(small)])
    assert idx.docs == ["small.md"]


# --------------------------------------------------------------------------- search


def test_search_ranks_a_heading_hit_over_a_body_only_hit(tmp_path) -> None:
    body = (
        "# Doc\n\n"
        "## Persistence\n\nEverything about how the disk file is written and replaced.\n\n"
        "## Networking\n\nA passing mention of persistence in one sentence here.\n\n"
        "## Rendering\n\nUnrelated section about drawing pixels on a screen.\n"
    )
    idx = _index(tmp_path, body)
    hits = idx.search("how does persistence work")
    assert hits and hits[0].crumb.endswith("Persistence")


def test_search_matches_on_word_boundaries_not_substrings(tmp_path) -> None:
    # "bread" must not match "breadcrumb"; "cat" must not match "concatenate".
    idx = _index(tmp_path, "# Doc\n\n## X\n\nThe breadcrumb concatenates the path.\n")
    assert idx.search("how do I bake bread") == []
    assert idx.search("tell me about cats") == []


def test_search_is_stemmed(tmp_path) -> None:
    idx = _index(tmp_path)
    # doc says "pruned" / "pruning" / "pruner"; the query says "prune".
    hits = idx.search("how does the graph get pruned")
    assert hits and "Pruning" in hits[0].crumb


def test_stem_collapses_common_suffixes() -> None:
    assert _stem("pruned") == _stem("pruning") == _stem("prune")
    assert _stem("stores") == _stem("stored") == _stem("store")
    assert _stem("is") == "is"  # too short to touch
    assert _stem("bus") == "bus"  # a 3-letter word ending in "s" is left alone


def test_search_empty_or_pure_stopword_query_returns_nothing(tmp_path) -> None:
    idx = _index(tmp_path)
    assert idx.search("   ") == []
    assert idx.search("what is the how of it") == []


def test_search_rejects_an_off_topic_question(tmp_path) -> None:
    idx = _index(tmp_path)
    assert idx.search("what is the capital of France") == []
    assert idx.search("who won the world cup") == []


def test_search_respects_the_char_budget(tmp_path) -> None:
    # Six sections that each mention "kestrel" twice; unique filler otherwise so
    # "kestrel" stays distinctive.
    body = "# Doc\n\n" + "".join(
        f"## Section {i}\n\nThe kestrel {i} hunts. A kestrel {i} soars over field {i}.\n\n"
        for i in range(6)
    )
    idx = _index(tmp_path, body)
    hits = idx.search("kestrel", limit=6, max_chars=120)
    assert 1 <= len(hits) < 6  # stopped by the budget, not the limit


# --------------------------------------------------------------------------- real corpus


def test_default_doc_paths_point_at_real_repo_files() -> None:
    assert (repo_root() / "Architecture.md").is_file()
    assert any(p.endswith("Architecture.md") for p in default_doc_paths())


def test_the_real_corpus_answers_project_questions_and_rejects_off_topic() -> None:
    idx = KnowledgeIndex.from_paths(default_doc_paths())
    assert len(idx) > 100
    project = [
        "how is the behavioural graph stored",
        "what does the drive layer do",
        "how does NeuroPaca decide to take an action",
        "what is quarantine",
        "what model does the interactive path use",
        "does NeuroPaca send data to the cloud",
        "what are the four invariants",
        "how does the graph get pruned",
        "what is BitNet",
        "how does the daemon recover from a crash",
    ]
    off_topic = [
        "what is the capital of France",
        "who painted the mona lisa",
        "explain photosynthesis",
        "how tall is mount everest",
    ]
    missed = [q for q in project if not idx.search(q)]
    leaked = [q for q in off_topic if idx.search(q)]
    assert not missed, f"project questions with no retrieval: {missed}"
    assert not leaked, f"off-topic questions that retrieved a chunk: {leaked}"
