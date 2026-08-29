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
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from neuropaca.core.errors import InferenceError

if TYPE_CHECKING:
    from neuropaca.core.config import Config


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
            return '{"insight": null, "cited_nodes": [], "confidence": 0.0}'
        digest = hashlib.sha256(f"{prompt}|{max_tokens}|{temperature}".encode()).hexdigest()
        return f"fake-response:{digest[:16]}"

    async def infer_async(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        return self.infer(prompt, max_tokens, temperature, grammar)

    def get_ram_usage_mb(self) -> float:
        return 0.0


class LlamaCppBackend:
    """Skeleton for the real BitNet b1.58 2B4T runtime (Architecture.md §11).

    B1 wires the seam; the llama.cpp binding lands in B4 once the B0 spike has
    confirmed the RAM/latency budget on the target machine.
    """

    def __init__(self, model_path: str, *, n_threads: int, max_context_tokens: int) -> None:
        self._model_path = model_path
        self._n_threads = n_threads
        self._max_context_tokens = max_context_tokens
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        raise InferenceError(
            "LlamaCppBackend is a B1 skeleton — the llama.cpp binding lands in B4. "
            "Use inference_backend='fake' until then."
        )

    def unload(self) -> None:
        self._loaded = False

    def infer(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        raise InferenceError("LlamaCppBackend.infer is not implemented before B4")

    async def infer_async(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        raise InferenceError("LlamaCppBackend.infer_async is not implemented before B4")

    def get_ram_usage_mb(self) -> float:
        return 0.0


def create_backend(config: Config) -> InferenceBackend:
    """Build the backend named by `config.inference_backend` (rules.md §4)."""
    if config.inference_backend == "fake":
        return FakeInferenceBackend()
    if config.inference_backend == "llama":
        return LlamaCppBackend(
            config.model_path,
            n_threads=config.n_threads,
            max_context_tokens=config.bitnet_max_tokens,
        )
    raise InferenceError(f"unknown inference_backend: {config.inference_backend!r}")
