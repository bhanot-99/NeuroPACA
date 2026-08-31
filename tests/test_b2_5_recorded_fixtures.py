"""B2.5b exit criterion · recorded-fixture replay (phases.md B2.5b, D-10).

    "the two patterns fire on fixtures and stay silent on negatives"

The three traces under ``tests/fixtures/`` (built by ``generate_b2_5_traces.py``)
are ordered lists of mixed ``metric`` / ``app_switch`` events. Here we rebuild
each as a real ``Event`` at its recorded timestamp and feed it through a real
``SignalCorrelator`` (real ``EventBus`` + ``GraphMemory`` + the shipped
``data/app_map.default.toml``), draining the bus after each event and spying on
``SIGNAL_CORRELATED`` / ``SYSTEM_ERROR``. No sleeping — L3 is purely event-driven.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, RelationType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.diagnosis.correlator import SignalCorrelator
from neuropaca.diagnosis.signal import Signal
from neuropaca.sensing.snapshot import MetricSnapshot
from tests.fixtures import generate_b2_5_traces

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_MAP = _REPO_ROOT / "data" / "app_map.default.toml"


def _ensure_traces() -> None:
    paths = (
        generate_b2_5_traces.TRACE_FOCUS,
        generate_b2_5_traces.TRACE_DISTRACTION,
        generate_b2_5_traces.TRACE_CALM,
    )
    if not all(p.exists() for p in paths):
        generate_b2_5_traces.write_all()


def _events(path: Path) -> list[Event]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    base = datetime.fromisoformat(raw["base"])
    out: list[Event] = []
    for item in sorted(raw["events"], key=lambda e: e["offset"]):
        ts = base + timedelta(seconds=int(item["offset"]))
        if item["kind"] == "metric":
            snapshot = MetricSnapshot(
                collector_name=str(item["collector_name"]),
                timestamp=ts,
                data=dict(item["data"]),
            )
            out.append(
                Event(
                    event_type=EventType.METRIC_COLLECTED,
                    source=f"sensing.{snapshot.collector_name}",
                    payload={"snapshot": snapshot},
                    timestamp=ts,
                )
            )
        else:
            out.append(
                Event(
                    event_type=EventType.APP_SWITCH,
                    source="sensing.activity",
                    payload={
                        "app_id": item["app_id"],
                        "title": item.get("title", ""),
                        "previous_app_id": None,
                    },
                    timestamp=ts,
                )
            )
    return out


class _Spy:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    @property
    def signals(self) -> list[Signal]:
        return [e.payload["signal"] for e in self.events]


async def _replay(path: Path, tmp_path: Path) -> tuple[SignalCorrelator, _Spy, _Spy]:
    _ensure_traces()
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await graph.load()
    correlator = SignalCorrelator(
        bus, Config(inference_backend="fake", app_map_path=str(_APP_MAP)), graph
    )
    await correlator.initialize()
    await correlator.start()

    signals, errors = _Spy(), _Spy()
    bus.subscribe(EventType.SIGNAL_CORRELATED, signals)
    bus.subscribe(EventType.SYSTEM_ERROR, errors)

    for event in _events(path):
        if event.event_type is EventType.METRIC_COLLECTED:
            await correlator.on_metric_event(event)
        else:
            await correlator.on_app_switch(event)
        await bus.join()

    await correlator.stop()
    await bus.stop()
    return correlator, signals, errors


def _types(signals: _Spy) -> list[SignalType]:
    return [s.signal_type for s in signals.signals]


# --------------------------------------------------------------------- Focus


async def test_focus_trace_fires_focus_session_once_and_wires_the_domain(tmp_path: Path) -> None:
    correlator, signals, errors = await _replay(generate_b2_5_traces.TRACE_FOCUS, tmp_path)

    assert _types(signals) == [SignalType.FOCUS_SESSION]
    signal = signals.signals[0]
    assert signal.related_node_ids == ("app:dev.zed.Zed",)

    node = correlator._graph.get_node("app:dev.zed.Zed")
    assert node is not None
    edges = correlator._graph.get_edges("app:dev.zed.Zed")
    assert any(
        e.target_id == "domain:engineering" and e.relation is RelationType.PART_OF for e in edges
    )
    assert errors.events == []
    assert correlator._errors == 0


# ----------------------------------------------------------------- Distraction


async def test_distraction_trace_fires_distraction_once_and_no_focus(tmp_path: Path) -> None:
    correlator, signals, errors = await _replay(generate_b2_5_traces.TRACE_DISTRACTION, tmp_path)

    assert _types(signals) == [SignalType.DISTRACTION]
    assert signals.signals[0].related_node_ids == ()
    assert errors.events == []
    assert correlator._errors == 0


# ------------------------------------------------------------------- Negative


async def test_calm_trace_fires_nothing(tmp_path: Path) -> None:
    correlator, signals, errors = await _replay(generate_b2_5_traces.TRACE_CALM, tmp_path)

    assert signals.events == []
    assert errors.events == []
    assert correlator._errors == 0
