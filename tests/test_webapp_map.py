# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B14 · WebAppMap — site-name token -> routing domain allowlist.

Same contract as AppMap (test_app_map.py): pure lookup, the one I/O path is a
TOML read under tmp_path, and the shipped default file is exercised so a broken
default fails here rather than silently in the daemon.
"""

from __future__ import annotations

from pathlib import Path

from neuropaca.sensing.activity.webapp import WebAppMap

_DEFAULT = Path(__file__).resolve().parents[1] / "data" / "webapp_map.default.toml"


def test_token_maps_to_label_and_domain() -> None:
    m = WebAppMap.from_dict({"webapp": {"gmail": "comms"}})
    hit = m.match_segments(["inbox (351)", "x@gmail.com", "gmail"])
    assert hit is not None
    assert hit.label == "gmail"
    assert hit.domain == "domain:comms"


def test_multi_word_token_is_slugified_for_the_label() -> None:
    m = WebAppMap.from_dict({"webapp": {"Google Docs": "projects"}})
    hit = m.match_segments(["some doc", "google docs"])
    assert hit is not None
    assert hit.label == "google-docs"
    assert hit.domain == "domain:projects"


def test_first_matching_segment_wins() -> None:
    m = WebAppMap.from_dict({"webapp": {"gmail": "comms", "youtube": "habits"}})
    hit = m.match_segments(["youtube", "gmail"])
    assert hit is not None and hit.label == "youtube"


def test_no_match_returns_none() -> None:
    m = WebAppMap.from_dict({"webapp": {"gmail": "comms"}})
    assert m.match_segments(["some bank", "account summary"]) is None
    assert m.match_segments([]) is None


def test_unknown_domain_row_is_dropped_not_raised(caplog) -> None:
    m = WebAppMap.from_dict({"webapp": {"gmail": "comms", "evil": "not_a_domain"}})
    assert m.rule_count == 1
    assert m.match_segments(["gmail"]) is not None
    assert m.match_segments(["evil"]) is None
    assert "not one of the 10 domains" in caplog.text


def test_unknown_section_is_ignored(caplog) -> None:
    m = WebAppMap.from_dict({"tabs": {"gmail": "comms"}})
    assert m.rule_count == 0
    assert "unknown section" in caplog.text


def test_missing_file_yields_empty_map(tmp_path: Path, caplog) -> None:
    m = WebAppMap.from_file(tmp_path / "nope.toml")
    assert m.rule_count == 0
    assert "not found" in caplog.text


def test_malformed_toml_yields_empty_map(tmp_path: Path, caplog) -> None:
    bad = tmp_path / "webapp_map.toml"
    bad.write_text("this = = not toml", encoding="utf-8")
    m = WebAppMap.from_file(bad)
    assert m.rule_count == 0
    assert "cannot read" in caplog.text


def test_shipped_default_file_is_valid() -> None:
    m = WebAppMap.from_file(_DEFAULT)
    assert m.rule_count > 0
    assert m.match_segments(["gmail"]).domain == "domain:comms"  # type: ignore[union-attr]
    assert m.match_segments(["github"]).domain == "domain:engineering"  # type: ignore[union-attr]
    assert m.match_segments(["youtube"]).domain == "domain:habits"  # type: ignore[union-attr]


# gen-ref: b5ae6ff8
