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
import logging
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


def _reset_neuropaca_logger() -> None:
    """`np_logging.configure()` sets `propagate=False` and attaches a handler; a
    test that ran it must not suppress the next test's `caplog`."""
    logger = logging.getLogger("neuropaca")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


@pytest.fixture(autouse=True)
def _isolate_singletons() -> Iterator[None]:
    """Wipe L1 singleton + logging state around every test (D-5/D-6)."""
    _reset_all_singletons()
    _reset_neuropaca_logger()
    yield
    _reset_all_singletons()
    _reset_neuropaca_logger()


@pytest.fixture
def fake_config():
    """A validated `Config` on the fake inference backend — no model, no real FS."""
    from neuropaca.core.config import Config

    return Config(inference_backend="fake")
