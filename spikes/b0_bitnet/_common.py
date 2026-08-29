"""Shared helpers for the B0 BitNet de-risking spike.

THROWAWAY SPIKE CODE. Not shipped, never imported by ``src/neuropaca/``
(phases.md B0, rules.md §15). Its only jobs: load BitNet b1.58 2B4T through
llama.cpp, assemble a per-call GBNF grammar exactly as rules.md §4.1 describes,
run the model, and score the parsed output.

If the spike's answers are good enough, the grammar-assembly and validation
logic here is the reference the real ``learning/prompts.py`` +
``BitNetRuntime.infer_async`` are built against in B4.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPIKE_DIR = Path(__file__).parent
GRAMMAR_TEMPLATE = SPIKE_DIR / "grammars" / "insight.gbnf.template"
ALIAS_MARKER = "__ALIASES__"

# A local alias is n1..nN — never a raw node id in the grammar (rules.md §4.1).
ALIAS_RE = re.compile(r"^n[1-9][0-9]*$")


@dataclass(frozen=True)
class ContextNode:
    """One distilled graph node, as ``build_context_from_nodes`` will emit it."""

    alias: str
    label: str
    node_type: str
    score: float
    attrs: dict[str, str] = field(default_factory=dict)

    def as_line(self) -> str:
        """`[n1] webpack · APP · score 8.1 · cpu_avg 94%` (rules.md §4.1)."""
        tail = "".join(f" · {k} {v}" for k, v in self.attrs.items())
        return f"  [{self.alias}] {self.label} · {self.node_type} · score {self.score:.1f}{tail}"


@dataclass(frozen=True)
class Fixture:
    """One synthetic (signal + K nodes) test case."""

    fixture_id: str
    signal: str
    confidence: float
    nodes: tuple[ContextNode, ...]
    # Aliases a correct answer should cite; empty tuple => the model should abstain.
    expected_citations: tuple[str, ...]
    weak: bool = False

    @property
    def k(self) -> int:
        return len(self.nodes)


@dataclass(frozen=True)
class Completion:
    """One raw model response — text plus the numbers the benchmark needs."""

    text: str
    latency_s: float
    completion_tokens: int

    @property
    def tokens_per_second(self) -> float:
        return self.completion_tokens / self.latency_s if self.latency_s > 0 else 0.0


@dataclass
class GenerationResult:
    fixture_id: str
    raw_output: str
    latency_s: float
    parsed: dict[str, Any] | None
    parse_ok: bool
    citations_valid: bool
    grounded: bool
    abstained: bool


# --------------------------------------------------------------------------- #
# Prompt + grammar assembly  (pure string work — must run BEFORE any lock)
# --------------------------------------------------------------------------- #

_FEW_SHOT = (
    "Example:\n"
    "Signal: HIGH_LOAD (conf 0.80)\n"
    "Facts:\n"
    "  [n1] eslint · APP · score 7.2 · cpu_avg 91%\n"
    "  [n2] ~/work/site · FILE · score 6.0\n"
    'Answer: {"insight": "eslint has been pinning a core at 91% while you edit '
    '~/work/site.", "cited_nodes": ["n1", "n2"], "confidence": 0.78}\n\n'
)


def build_prompt(fixture: Fixture) -> str:
    """One prompt per task, few-shot first, the signal LAST (problems.md 1.13)."""
    facts = "\n".join(node.as_line() for node in fixture.nodes)
    return (
        _FEW_SHOT
        + "Facts:\n"
        + facts
        + "\n"
        + f"Signal: {fixture.signal} (conf {fixture.confidence:.2f})\n"
        + "Write one sentence about this signal, grounded in the facts. "
        + "If they do not support one, abstain.\n"
        + "Answer: "
    )


def build_grammar(aliases: Sequence[str]) -> str:
    """Splice the alias enum into the static GBNF skeleton.

    ``aliases`` must be the exact aliases present in *this* prompt, so the model
    cannot cite a node it was not given.
    """
    for alias in aliases:
        if not ALIAS_RE.match(alias):
            raise ValueError(f"not a local alias: {alias!r}")
    if len(set(aliases)) != len(aliases):
        raise ValueError(f"duplicate aliases: {aliases!r}")

    enum = " | ".join(f'"\\"{alias}\\""' for alias in aliases)
    template = GRAMMAR_TEMPLATE.read_text(encoding="utf-8")
    if ALIAS_MARKER not in template:
        raise ValueError(f"{GRAMMAR_TEMPLATE} is missing {ALIAS_MARKER}")
    return template.replace(ALIAS_MARKER, enum)


# --------------------------------------------------------------------------- #
# Post-generation validation  (the hard gate — rules.md §4.1 item 6)
# --------------------------------------------------------------------------- #


def score_output(fixture: Fixture, raw: str, latency_s: float) -> GenerationResult:
    parsed = _try_parse(raw)
    result = GenerationResult(
        fixture_id=fixture.fixture_id,
        raw_output=raw,
        latency_s=latency_s,
        parsed=parsed,
        parse_ok=parsed is not None,
        citations_valid=False,
        grounded=False,
        abstained=False,
    )
    if parsed is None:
        return result

    insight = parsed.get("insight")
    cited = parsed.get("cited_nodes") or []
    result.abstained = insight is None or (
        isinstance(insight, str) and insight.strip().lower() in {"", "insufficient evidence"}
    )

    prompt_aliases = {node.alias for node in fixture.nodes}
    result.citations_valid = all(alias in prompt_aliases for alias in cited)

    if result.abstained:
        # An abstention is "grounded" iff the fixture wanted one.
        result.grounded = not fixture.expected_citations
        return result

    if isinstance(insight, str) and result.citations_valid and cited:
        labels = {node.alias: node.label.lower() for node in fixture.nodes}
        result.grounded = any(labels.get(a, "\0") in insight.lower() for a in cited)
    return result


def citation_accuracy(fixture: Fixture, result: GenerationResult) -> float | None:
    """Fraction of the model's citations that are in the expected set.

    ``None`` for a weak fixture (there is no positive citation set to score) —
    those feed the correct-abstain metric instead.
    """
    if fixture.weak or not fixture.expected_citations:
        return None
    if result.parsed is None or result.abstained:
        return 0.0
    cited = result.parsed.get("cited_nodes") or []
    if not cited:
        return 0.0
    hits = sum(1 for a in cited if a in fixture.expected_citations)
    return hits / len(cited)


def _try_parse(raw: str) -> dict[str, Any] | None:
    """The grammar makes this near-certain, but parse defensively anyway."""
    raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# --------------------------------------------------------------------------- #
# Fixtures I/O
# --------------------------------------------------------------------------- #


def load_fixtures(path: Path) -> list[Fixture]:
    fixtures: list[Fixture] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        obj = json.loads(line)
        fixtures.append(
            Fixture(
                fixture_id=obj["fixture_id"],
                signal=obj["signal"],
                confidence=float(obj["confidence"]),
                nodes=tuple(
                    ContextNode(
                        alias=n["alias"],
                        label=n["label"],
                        node_type=n["node_type"],
                        score=float(n["score"]),
                        attrs=dict(n.get("attrs", {})),
                    )
                    for n in obj["nodes"]
                ),
                expected_citations=tuple(obj.get("expected_citations", [])),
                weak=bool(obj.get("weak", False)),
            )
        )
    return fixtures


def iter_fixtures_by_k(fixtures: Sequence[Fixture]) -> Iterator[tuple[int, list[Fixture]]]:
    for k in sorted({f.k for f in fixtures}):
        yield k, [f for f in fixtures if f.k == k]


# --------------------------------------------------------------------------- #
# llama.cpp binding  (imported lazily so `gen_fixtures.py` needs no model)
# --------------------------------------------------------------------------- #


@dataclass
class LlamaRunner:
    model_path: Path
    n_ctx: int = 2048
    n_threads: int | None = None
    _llm: Any = None

    def load(self) -> None:
        from llama_cpp import Llama

        t0 = time.perf_counter()
        self._llm = Llama(
            model_path=str(self.model_path),
            n_ctx=self.n_ctx,
            n_threads=self.n_threads,
            logits_all=False,
            verbose=False,
        )
        print(f"[load] {self.model_path.name} in {time.perf_counter() - t0:.1f}s")

    def generate(self, prompt: str, grammar_gbnf: str, *, max_tokens: int = 96) -> Completion:
        from llama_cpp import LlamaGrammar

        if self._llm is None:
            raise RuntimeError("call load() first")
        grammar = LlamaGrammar.from_string(grammar_gbnf, verbose=False)
        t0 = time.perf_counter()
        out = self._llm(
            prompt,
            max_tokens=max_tokens,
            temperature=0.0,  # greedy — enums/citations/confidence (rules.md §4.1)
            grammar=grammar,
        )
        latency = time.perf_counter() - t0
        usage = out.get("usage") or {}
        return Completion(
            text=out["choices"][0]["text"],
            latency_s=latency,
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )
