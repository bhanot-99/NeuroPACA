"""The exception tree is a contract other layers catch against — pin its shape."""

from __future__ import annotations

import asyncio

import pytest

from neuropaca.core import errors

ALL_SUBCLASSES = [
    errors.ConfigError,
    errors.EventBusError,
    errors.GraphMemoryError,
    errors.InferenceError,
    errors.InferenceTimeout,
    errors.SafetyGateError,
    errors.ModuleLifecycleError,
]


@pytest.mark.parametrize("exc_type", ALL_SUBCLASSES)
def test_every_error_descends_from_the_one_root(exc_type: type[Exception]) -> None:
    assert issubclass(exc_type, errors.NeuroPACAError)


def test_root_does_not_catch_cancellation_or_exit() -> None:
    # rules.md §1 / §9 — CancelledError, KeyboardInterrupt and SystemExit must
    # never be swallowed by `except NeuroPACAError`.
    for control_flow in (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        assert not issubclass(control_flow, errors.NeuroPACAError)


def test_inference_timeout_is_an_inference_error() -> None:
    assert issubclass(errors.InferenceTimeout, errors.InferenceError)
    with pytest.raises(errors.InferenceError):
        raise errors.InferenceTimeout("budget exceeded")


def test_errors_carry_their_message() -> None:
    err = errors.GraphMemoryError("atomic save failed")
    assert str(err) == "atomic save failed"
