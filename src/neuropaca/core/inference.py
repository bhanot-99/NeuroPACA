"""The inference backend seam (Architecture.md §3.3, §11; rules.md §4, D-6).

Module code never imports a concrete backend — it depends on the
`InferenceBackend` protocol and receives an instance. `create_backend()` maps
`Config.inference_backend` to the implementation:

- `"fake"` -> `FakeInferenceBackend`, deterministic, no model, no I/O (rules.md §8)
- `"llama"` -> `LlamaCppBackend`, the real in-process llama.cpp runtime

Every entry point carries `grammar: str | None` from B1 (D-6) so B4's
GBNF-constrained path needs no signature change. `grammar` is a pre-assembled
GBNF string; `None` means free decoding.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from neuropaca.core.errors import InferenceError
from neuropaca.core.health import current_rss_mb

if TYPE_CHECKING:
    from neuropaca.core.config import Config

_log = logging.getLogger(__name__)

# A well-formed abstain the insight grammar would also allow — `parse_insight`
# turns it into `None` (discard), so a missing backend degrades silently to
# "L4 generates nothing" rather than crashing (D-11).
_GRACEFUL_ABSTAIN = '{"cited_node_id": null, "insight_category": "routine"}'


@runtime_checkable
class InferenceBackend(Protocol):
    """What `BitNetRuntime` drives. One inference at a time is enforced above
    this layer, not here."""

    @property
    def is_loaded(self) -> bool: ...

    def load(self) -> None: ...

    def unload(self) -> None: ...

    def infer(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str: ...

    async def infer_async(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str: ...

    def get_ram_usage_mb(self) -> float: ...


class FakeInferenceBackend:
    """Deterministic stand-in for tests and `inference_backend="fake"`.

    Output is a pure function of the arguments — no randomness, no model, no
    clock. When a grammar is supplied it returns the schema's abstain form so a
    validation gate has something well-formed to parse.
    """

    def __init__(self) -> None:
        self._loaded = False
        self.calls: list[tuple[str, int, float, str | None]] = []

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def infer(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        self.calls.append((prompt, max_tokens, temperature, grammar))
        if grammar is not None:
            # Deterministic, and shaped for whichever grammar is in play: the
            # D-11 extractive insight schema cites the first alias (`n1` is
            # always present) and picks a category; anything else abstains.
            if "cited_node_id" in grammar:
                return '{"cited_node_id": "n1", "insight_category": "anomaly"}'
            return '{"cited_node_id": null, "insight_category": "routine"}'
        digest = hashlib.sha256(f"{prompt}|{max_tokens}|{temperature}".encode()).hexdigest()
        return f"fake-response:{digest[:16]}"

    async def infer_async(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        return self.infer(prompt, max_tokens, temperature, grammar)

    def get_ram_usage_mb(self) -> float:
        return 0.0


class LlamaCppBackend:
    """The real in-process BitNet b1.58 2B4T runtime via llama.cpp (D-11).

    `load()` is the ONLY method that touches `llama_cpp`, and it is defensive:
    a missing wheel (CI runners have no C toolchain) or a missing model file is
    logged, leaves `is_loaded` False, and every later `infer()` returns a
    graceful abstain instead of raising (`rules.md §2`). The blocking work —
    the `Llama(...)` constructor and `create_completion` — is offloaded by the
    caller (`BitNetRuntime`), never run on the event loop.
    """

    def __init__(self, model_path: str, *, n_threads: int, n_ctx: int) -> None:
        self._model_path = model_path
        self._n_threads = n_threads
        self._n_ctx = n_ctx
        self._llama: Any = None
        self._grammar_cls: Any = None
        self.unavailable_reason: str | None = None
        self._rss_load_delta_mb = 0.0

    @property
    def is_loaded(self) -> bool:
        return self._llama is not None

    def load(self) -> None:
        if self._llama is not None:
            return
        try:
            from llama_cpp import Llama, LlamaGrammar
        except ImportError as exc:
            self.unavailable_reason = f"llama-cpp-python not installed ({exc})"
            _log.error("L4 inference disabled — %s", self.unavailable_reason)
            return
        if not Path(self._model_path).is_file():
            self.unavailable_reason = f"model file not found: {self._model_path}"
            _log.error("L4 inference disabled — %s", self.unavailable_reason)
            return
        rss_before = current_rss_mb() or 0.0
        try:
            self._llama = Llama(
                model_path=self._model_path,
                n_ctx=self._n_ctx,
                n_threads=self._n_threads,
                verbose=False,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            self.unavailable_reason = f"llama.cpp load failed: {exc}"
            _log.error("L4 inference disabled — %s", self.unavailable_reason)
            return
        self._grammar_cls = LlamaGrammar
        self._rss_load_delta_mb = max(0.0, (current_rss_mb() or 0.0) - rss_before)
        self.unavailable_reason = None

    def unload(self) -> None:
        llama, self._llama = self._llama, None
        close = getattr(llama, "close", None)
        if callable(close):
            close()
        self._grammar_cls = None

    def infer(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        """BLOCKING. Runs in `BitNetRuntime`'s dedicated executor, never the loop."""
        if self._llama is None:
            return _GRACEFUL_ABSTAIN
        compiled = None
        if grammar is not None and self._grammar_cls is not None:
            compiled = self._grammar_cls.from_string(grammar, verbose=False)
        out = self._llama.create_completion(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            grammar=compiled,
            stream=False,
        )
        try:
            return str(out["choices"][0]["text"])
        except (KeyError, IndexError, TypeError):
            return _GRACEFUL_ABSTAIN

    async def infer_async(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        # BitNetRuntime.infer_async is the real offload path (rules.md §1). This
        # exists only to satisfy the protocol; it must not be called on the loop.
        return self.infer(prompt, max_tokens, temperature, grammar)

    def get_ram_usage_mb(self) -> float:
        return self._rss_load_delta_mb


def create_backend(config: Config) -> InferenceBackend:
    """Build the backend named by `config.inference_backend` (rules.md §4)."""
    if config.inference_backend == "fake":
        return FakeInferenceBackend()
    if config.inference_backend == "llama":
        return LlamaCppBackend(
            config.model_path,
            n_threads=config.n_threads,
            n_ctx=config.model_context_tokens,
        )
    raise InferenceError(f"unknown inference_backend: {config.inference_backend!r}")
