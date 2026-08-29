"""Logging setup: idempotent handler, root namespacing, redaction."""

from __future__ import annotations

import io
import logging

import pytest

from neuropaca.core import logging as np_logging


@pytest.fixture(autouse=True)
def _reset_root_logger() -> None:
    """Each test starts from a clean ``neuropaca`` logger."""
    root = logging.getLogger("neuropaca")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.NOTSET)


def test_configure_is_idempotent_and_does_not_stack_handlers() -> None:
    stream = io.StringIO()
    np_logging.configure("INFO", stream=stream)
    np_logging.configure("INFO", stream=stream)

    root = logging.getLogger("neuropaca")
    assert len(root.handlers) == 1
    assert np_logging.is_configured()


def test_configure_respects_level() -> None:
    stream = io.StringIO()
    np_logging.configure("WARNING", stream=stream)

    logger = np_logging.get_logger("sensing.system")
    logger.info("suppressed")
    logger.warning("emitted")

    out = stream.getvalue()
    assert "suppressed" not in out
    assert "emitted" in out
    assert "neuropaca.sensing.system" in out


def test_unknown_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown log level"):
        np_logging.configure("CHATTY")


def test_get_logger_does_not_double_prefix_a_dotted_name() -> None:
    logger = np_logging.get_logger("neuropaca.core.logging")
    assert logger.name == "neuropaca.core.logging"


def test_child_loggers_do_not_propagate_to_the_python_root(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = io.StringIO()
    np_logging.configure("INFO", stream=stream)
    np_logging.get_logger("interface").info("only in our stream")

    assert "only in our stream" in stream.getvalue()
    assert capsys.readouterr().err == ""


def test_redact_hides_the_value_but_reports_its_length() -> None:
    secret = "/home/user/projects/private/notes.md"
    assert np_logging.redact(secret) == f"<redacted {len(secret)} chars>"
    assert np_logging.redact(secret, keep=6).startswith("/home/")
    assert secret not in np_logging.redact(secret, keep=6)
