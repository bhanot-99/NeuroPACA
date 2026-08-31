"""Stress · B4 — L4 structural gating + bounded buffer under 1 000 signals (D-11).

`BitNetPlasticity` must shed most of a signal storm before inference. This fires
1 000 `SIGNAL_CORRELATED` events with randomised confidence and heavily
overlapping `related_node_ids`, plus a mock runtime that reports `is_busy` on a
fixed cadence, and proves:

- **> 50 % dropped**, from a genuine mix of all three gates — `confidence < 0.7`,
  `is_busy`, Jaccard novelty `> 0.8`.
- the `adaptation_buffer` deque clamps at `maxlen == 64` — `len` never exceeds
  it however many insights are generated (no unbounded growth).
- every signal is accounted for: `generated + dropped == 1000`, and the
  per-reason counters sum to `dropped`.
"""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime

import pytest

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.signal import Signal
from neuropaca.learning.plasticity import BitNetPlasticity
from neuropaca.sensing.snapshot import MetricSnapshot

pytestmark = pytest.mark.stress

_N = 1_000
_BUFFER_MAX = 64
_SEED = 20260901
_SNAP = MetricSnapshot(collector_name="system", timestamp=datetime(2026, 1, 1, tzinfo=UTC), data={})
_NODE_POOL = [f"file:/proj/mod{i}.py" for i in range(12)]  # small pool -> heavy overlap


class _CadencedRuntime:
    """`is_busy` True every 4th check; otherwise a loaded model that returns a
    valid extractive insight instantly."""

    def __init__(self) -> None:
        self._busy_cycle = itertools.cycle([False, False, False, True])

    @property
    def is_loaded(self) -> bool:
        return True

    @property
    def is_busy(self) -> bool:
        return next(self._busy_cycle)

    @property
    def backend_unavailable(self) -> bool:
        return False

    async def load_model_async(self) -> bool:
        return True

    async def infer_async(
        self, prompt: str, max_tokens: int, temperature: float, grammar: str | None = None
    ) -> str:
        return '{"cited_node_id": "n1", "insight_category": "anomaly"}'


def _signals(rng: random.Random) -> list[Signal]:
    out: list[Signal] = []
    for _ in range(_N):
        conf = round(rng.uniform(0.1, 1.0), 3)
        k = rng.randint(1, 3)
        nodes = tuple(rng.sample(_NODE_POOL, k))  # drawn from the same 12 -> Jaccard high
        out.append(
            Signal(
                signal_type=SignalType.HIGH_LOAD,
                confidence=conf,
                related_node_ids=nodes,
                source_snapshots=(_SNAP,),
                reason="storm",
            )
        )
    return out


async def test_gating_sheds_most_of_a_1000_signal_storm(tmp_path) -> None:
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "g.json"))
    await graph.load()
    for nid in _NODE_POOL:
        await graph.upsert_node(nid, NodeType.FILE, {"label": nid.rsplit("/", 1)[-1]})

    module = BitNetPlasticity(
        bus,
        Config(inference_backend="fake"),
        graph,
        _CadencedRuntime(),  # type: ignore[arg-type]
    )
    await module.initialize()
    await module.start()
    assert module._buffer.maxlen == _BUFFER_MAX

    rng = random.Random(_SEED)
    for sig in _signals(rng):
        await module.on_signal_event(
            Event(
                event_type=EventType.SIGNAL_CORRELATED,
                source="diagnosis",
                payload={"signal": sig},
            )
        )
    await bus.join()

    # accounting — nothing vanished
    assert module._generated + module._dropped == _N
    assert sum(module._drops.values()) == module._dropped
    assert module._errors == 0

    # > 50 % shed
    assert module._dropped / _N > 0.5, f"only {module._dropped}/{_N} dropped"

    # a real mix of all three gates fired
    assert module._drops["confidence"] > 0, module._drops
    assert module._drops["busy"] > 0, module._drops
    assert module._drops["novelty"] > 0, module._drops

    # the buffer is mathematically clamped — no unbounded growth
    assert len(module._buffer) <= _BUFFER_MAX
    assert module._buffer.maxlen == _BUFFER_MAX
    if module._generated > _BUFFER_MAX:
        assert len(module._buffer) == _BUFFER_MAX

    await module.stop()
    await bus.stop()
