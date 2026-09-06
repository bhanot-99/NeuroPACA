"""B11 · the project-knowledge corpus behind the `chat` op (interface/knowledge.py).

Zero inference, zero embeddings — same "deliberately dumb lexical match" discipline
as `GraphMemory.search_by_label`. No test reads a real model.
"""

from __future__ import annotations

from neuropaca.interface.knowledge import KnowledgeIndex, default_doc_paths, repo_root

_DOC = """\
# Project

Intro paragraph, no heading of its own.

## Graph storage

The behavioural graph is one JSON file, written atomically via os.replace.
GraphMemory serialises every write behind a single asyncio.Lock.

### Pruning

Low-score nodes are dropped on a schedule so the graph stays bounded.

## Monitoring

The sensing layer polls cold system metrics every 60 seconds.

```python
# a fenced block whose "# heading" must NOT split the chunk
def collect(): ...
```
"""


def _index(tmp_path, body: str = _DOC) -> KnowledgeIndex:
    p = tmp_path / "notes.md"
    p.write_text(body)
    return KnowledgeIndex.from_paths([str(p)])


def test_chunks_split_on_headings_and_carry_a_breadcrumb(tmp_path) -> None:
    idx = _index(tmp_path)
    crumbs = {c.crumb for c in idx._chunks}
    assert "Project → Graph storage" in crumbs
    assert "Project → Graph storage → Pruning" in crumbs
    assert "Project → Monitoring" in crumbs


def test_a_hash_inside_a_fence_does_not_start_a_chunk(tmp_path) -> None:
    idx = _index(tmp_path)
    monitoring = next(c for c in idx._chunks if c.crumb == "Project → Monitoring")
    assert "def collect()" in monitoring.text  # the fenced "# heading" stayed put


def test_search_ranks_a_heading_hit_over_a_body_hit(tmp_path) -> None:
    idx = _index(tmp_path)
    hits = idx.search("how is the graph storage handled")
    assert hits
    assert hits[0].crumb.endswith("Graph storage")
    assert "os.replace" in hits[0].text


def test_search_empty_or_unmatched_query_returns_nothing(tmp_path) -> None:
    idx = _index(tmp_path)
    assert idx.search("   ") == []
    assert idx.search("quantum chromodynamics zzz") == []


def test_search_respects_the_char_budget(tmp_path) -> None:
    body = "# Doc\n\n" + "".join(
        f"## Section {i}\n\nwidget widget widget filler text here.\n\n" for i in range(20)
    )
    idx = _index(tmp_path, body)  # each matching chunk is ~37 chars
    hits = idx.search("widget", limit=20, max_chars=90)
    assert 1 <= len(hits) <= 3  # stopped by the budget, not the limit of 20


def test_a_missing_file_is_skipped_not_fatal(tmp_path) -> None:
    good = tmp_path / "ok.md"
    good.write_text("# Ok\n\nreal content about widgets\n")
    idx = KnowledgeIndex.from_paths([str(tmp_path / "nope.md"), str(good)])
    assert len(idx) >= 1
    assert idx.search("widgets")


def test_an_over_long_section_is_split(tmp_path) -> None:
    huge = "# Doc\n\n## Big\n\n" + "\n\n".join(f"paragraph number {i} " * 40 for i in range(40))
    idx = _index(tmp_path, huge)
    big_chunks = [c for c in idx._chunks if c.crumb == "Doc → Big"]
    assert len(big_chunks) > 1
    assert all(len(c.text) <= 1700 for c in big_chunks)


def test_default_doc_paths_point_at_real_repo_files() -> None:
    root = repo_root()
    assert (root / "Architecture.md").is_file()
    paths = default_doc_paths()
    assert any(p.endswith("Architecture.md") for p in paths)


def test_the_real_repo_corpus_answers_a_project_question() -> None:
    idx = KnowledgeIndex.from_paths(default_doc_paths())
    assert len(idx) > 50
    hits = idx.search("how does the drive layer accumulate pressure")
    assert hits
    assert any("drive" in c.cite.lower() or "pressure" in c.text.lower() for c in hits)
