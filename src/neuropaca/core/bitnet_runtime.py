"""L1 · `BitNetRuntime` — the single inference gate (Architecture.md §3.3, §11).

Invariants (rules.md §0, §4):
- singleton; one inference at a time system-wide, held by `_inference_lock`
- `infer()` is blocking; `infer_async()` offloads it to a dedicated
  single-worker executor so the event loop never freezes
- the backend is injected (rules.md §4) — this class never imports a concrete one

B1 ships the skeleton: lock + executor + delegation are real; the real llama.cpp
backend lands in B4.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import TYPE_CHECKING, ClassVar

from neuropaca.core.inference import FakeInferenceBackend

if TYPE_CHECKING:
    from neuropaca.core.inference import InferenceBackend
    from neuropaca.core.models import Node


class BitNetRuntime:
    """Serialises every model call and keeps the blocking work off the loop."""

    _instance: ClassVar[BitNetRuntime | None] = None

    def __init__(self, backend: InferenceBackend | None = None) -> None:
        self._backend: InferenceBackend = backend or FakeInferenceBackend()
        self._inference_lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bitnet")
        self._busy = False

    # ------------------------------------------------------------------ singleton
    @classmethod
    def get_instance(cls, backend: InferenceBackend | None = None) -> BitNetRuntime:
        if cls._instance is None:
            cls._instance = cls(backend)
        return cls._instance

    @classmethod
    def _reset_for_tests(cls) -> None:
        inst = cls._instance
        if inst is not None:
            inst._executor.shutdown(wait=False, cancel_futures=True)
        cls._instance = None

    # ---------------------------------------------------------------- properties
    @property
    def is_loaded(self) -> bool:
        return self._backend.is_loaded

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def backend_unavailable(self) -> bool:
        """True once a load has been attempted and the backend could not come up
        (no llama-cpp-python, missing model) — L4 gating stops trying (D-11)."""
        return getattr(self._backend, "unavailable_reason", None) is not None

    # ------------------------------------------------------------------ lifecycle
    def load_model(self) -> None:
        """BLOCKING — startup / shutdown / tests only. Coroutines call
        `load_model_async` so the ~1 s model init never touches the loop."""
        self._backend.load()

    async def load_model_async(self) -> bool:
        """Lazy load, offloaded to the dedicated single-worker executor (rules.md
        §1 — the shared `asyncio.to_thread` pool is reserved for other work).
        Returns whether the model is loaded afterwards."""
        if self._backend.is_loaded:
            return True
        loop = asyncio.get_running_loop()
        async with self._inference_lock:
            await loop.run_in_executor(self._executor, self._backend.load)
        return self._backend.is_loaded

    def unload_model(self) -> None:
        self._backend.unload()

    # ------------------------------------------------------------------ inference
    def infer(
        self, prompt: str, max_tokens: int, temperature: float = 0.0, grammar: str | None = None
    ) -> str:
        """BLOCKING. Never call this from a coroutine — use `infer_async` (rules.md §0.3)."""
        return self._backend.infer(prompt, max_tokens, temperature, grammar)

    async def infer_async(
        self, prompt: str, max_tokens: int, temperature: float = 0.0, grammar: str | None = None
    ) -> str:
        loop = asyncio.get_running_loop()
        async with self._inference_lock:
            self._busy = True
            try:
                call = partial(self._backend.infer, prompt, max_tokens, temperature, grammar)
                return str(await loop.run_in_executor(self._executor, call))
            finally:
                self._busy = False

    # ------------------------------------------------------------------ context
    def build_context_from_nodes(self, nodes: list[Node]) -> str:
        """One terse line per node — the only serialiser for model context
        (rules.md §4.1). Distillation to top-K happens in the caller."""
        return "\n".join(
            f"[{node.id}] {node.label} · {node.node_type} · score {node.relevance_score:.1f}"
            for node in nodes
        )

    def get_ram_usage_mb(self) -> float:
        return self._backend.get_ram_usage_mb()
