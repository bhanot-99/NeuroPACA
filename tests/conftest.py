"""Shared test fixtures.

State isolation (D-5/D-6): `EventBus`, `GraphMemory`, and `BitNetRuntime` are
`get_instance()` singletons. Without a wipe between tests, a queue bound to a
dead event loop or a graph from a previous test leaks forward. The `autouse`
fixture below calls each singleton's `_reset_for_tests()` before and after every
test.

The reset is tolerant of a singleton module not existing yet — during B1 the
modules land one at a time, and a test that `importorskip`s its target still gets
a clean reset of whatever *is* built.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest

_SINGLETONS: tuple[tuple[str, str], ...] = (
    ("neuropaca.core.event_bus", "EventBus"),
    ("neuropaca.core.graph_memory", "GraphMemory"),
    ("neuropaca.core.bitnet_runtime", "BitNetRuntime"),
)


def _reset_all_singletons() -> None:
    for module_name, class_name in _SINGLETONS:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        cls = getattr(module, class_name, None)
        reset = getattr(cls, "_reset_for_tests", None)
        if callable(reset):
            reset()


@pytest.fixture(autouse=True)
def _isolate_singletons() -> Iterator[None]:
    """Wipe L1 singleton state around every test (D-5/D-6)."""
    _reset_all_singletons()
    yield
    _reset_all_singletons()


@pytest.fixture
def fake_config():
    """A validated `Config` on the fake inference backend — no model, no real FS."""
    from neuropaca.core.config import Config

    return Config(inference_backend="fake")
