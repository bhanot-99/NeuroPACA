"""L1 · `BitNetRuntime` — the single inference gate (Architecture.md §3.3, §11).

Invariants (rules.md §0, §4):
- singleton; one inference at a time system-wide, held by `_inference_lock`
- `infer()` is blocking; `infer_async()` offloads it to a dedicated
  single-worker executor so the event loop never freezes
- the backend is injected (rules.md §4) — this class never imports a concrete one

B5 (D-12) adds a **second, optional backend** for the interactive `$` / `$?`
path: a larger Qwen2.5-3B Q4 model that can write a grounded sentence where the
2B4T loop model cannot (`problems.md` 1.13). It is lazy-loaded on the first
interactive request, resident concurrently with the loop model at peak
(PRD §9), and — crucially — still serialised by the *same* `_inference_lock`:
one inference at a time system-wide, whichever model.
"""

from __future__ import annotations

import asyncio
import gc
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

    def __init__(
        self,
        backend: InferenceBackend | None = None,
        interactive_backend: InferenceBackend | None = None,
    ) -> None:
        self._backend: InferenceBackend = backend or FakeInferenceBackend()
        self._interactive_backend: InferenceBackend | None = interactive_backend
        self._inference_lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bitnet")
        self._busy = False

    # ------------------------------------------------------------------ singleton
    @classmethod
    def get_instance(
        cls,
        backend: InferenceBackend | None = None,
        interactive_backend: InferenceBackend | None = None,
    ) -> BitNetRuntime:
        if cls._instance is None:
            cls._instance = cls(backend, interactive_backend)
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

    # -- interactive (L9, D-12) ------------------------------------------------
    @property
    def interactive_configured(self) -> bool:
        """An interactive backend was injected (a model path is set / fake)."""
        return self._interactive_backend is not None

    @property
    def interactive_loaded(self) -> bool:
        return self._interactive_backend is not None and self._interactive_backend.is_loaded

    @property
    def interactive_unavailable(self) -> bool:
        """A load was attempted and failed — L9 stops trying and uses templates."""
        return getattr(self._interactive_backend, "unavailable_reason", None) is not None

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

    async def load_interactive_model_async(self) -> bool:
        """Lazy-load the interactive model on the first `$` / `$?` (B5, D-12).
        Same executor + `_inference_lock` as the loop model — the two never load
        or infer concurrently. Returns False (never raises) when no interactive
        backend is configured or it self-disables."""
        be = self._interactive_backend
        if be is None:
            return False
        if be.is_loaded:
            return True
        loop = asyncio.get_running_loop()
        async with self._inference_lock:
            # Reclaim transient garbage before the ~2 GB Qwen allocation — the
            # concurrent footprint (~3.4 GB, PRD §9) leaves little headroom under
            # the B5 3.5 GB budget. Cheap: this path runs once per process.
            gc.collect()
            await loop.run_in_executor(self._executor, be.load)
        return be.is_loaded

    def unload_model(self) -> None:
        self._backend.unload()
        if self._interactive_backend is not None:
            self._interactive_backend.unload()

    # ------------------------------------------------------------------ inference
    def infer(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float = 0.0,
        grammar: str | None = None,
        *,
        interactive: bool = False,
    ) -> str:
        """BLOCKING. Never call this from a coroutine — use `infer_async` (rules.md §0.3)."""
        return self._select(interactive).infer(prompt, max_tokens, temperature, grammar)

    async def infer_async(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float = 0.0,
        grammar: str | None = None,
        *,
        interactive: bool = False,
    ) -> str:
        be = self._select(interactive)
        loop = asyncio.get_running_loop()
        async with self._inference_lock:
            self._busy = True
            try:
                call = partial(be.infer, prompt, max_tokens, temperature, grammar)
                return str(await loop.run_in_executor(self._executor, call))
            finally:
                self._busy = False

    def _select(self, interactive: bool) -> InferenceBackend:
        if interactive and self._interactive_backend is not None:
            return self._interactive_backend
        return self._backend

    # ------------------------------------------------------------------ context
    def build_context_from_nodes(self, nodes: list[Node]) -> str:
        """One terse line per node — thin pass-through to the shared serialiser
        (`core/context.py`, D-13/A8). Distillation to top-K is the caller's job."""
        from neuropaca.core.context import build_context_from_nodes

        return build_context_from_nodes(nodes)

    def get_ram_usage_mb(self) -> float:
        total = self._backend.get_ram_usage_mb()
        if self._interactive_backend is not None:
            total += self._interactive_backend.get_ram_usage_mb()
        return total
