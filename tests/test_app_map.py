"""B2.5b · AppMap — activity -> routing domain (D-10).

Pure dict + glob lookup, no I/O beyond reading a TOML file under `tmp_path`.
The shipped `data/app_map.default.toml` is exercised too, so a broken default
rules file fails here rather than silently in the daemon.
"""

from __future__ import annotations

from pathlib import Path

from neuropaca.diagnosis.app_map import AppMap

_DEFAULT_RULES = Path(__file__).resolve().parents[1] / "data" / "app_map.default.toml"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "app_map.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_exact_app_id_wins() -> None:
    m = AppMap.from_dict({"app_id": {"dev.zed.Zed": "engineering"}})
    assert m.classify("dev.zed.Zed") == "domain:engineering"


def test_wm_class_is_the_xwayland_fallback() -> None:
    m = AppMap.from_dict({"wm_class": {"code": "engineering"}})
    assert m.classify(None, wm_class="code") == "domain:engineering"
    assert m.classify("unknown.app", wm_class="code") == "domain:engineering"


def test_path_glob_matches_last() -> None:
    m = AppMap.from_dict({"path_glob": {"*/papers/*": "research"}})
    assert m.classify("org.pwmt.zathura", path="/home/u/papers/attention.pdf") == "domain:research"
    assert m.classify("org.pwmt.zathura", path="/home/u/code/x.py") is None


def test_lookup_order_app_id_before_wm_class_before_glob() -> None:
    m = AppMap.from_dict(
        {
            "app_id": {"a": "engineering"},
            "wm_class": {"w": "research"},
            "path_glob": {"*.md": "learning"},
        }
    )
    assert m.classify("a", wm_class="w", path="notes.md") == "domain:engineering"
    assert m.classify(None, wm_class="w", path="notes.md") == "domain:research"
    assert m.classify(None, path="notes.md") == "domain:learning"


def test_a_miss_returns_none() -> None:
    m = AppMap.from_dict({"app_id": {"a": "engineering"}})
    assert m.classify("z") is None
    assert m.classify(None) is None
    assert m.classify("") is None


def test_unknown_domain_is_dropped_not_raised(caplog) -> None:
    m = AppMap.from_dict({"app_id": {"a": "engineering", "b": "not_a_domain"}})
    assert m.classify("a") == "domain:engineering"
    assert m.classify("b") is None
    assert m.rule_count == 1
    assert "not one of the 10 domains" in caplog.text


def test_unknown_section_is_ignored(caplog) -> None:
    m = AppMap.from_dict({"process_name": {"python": "engineering"}})
    assert m.rule_count == 0
    assert "unknown section" in caplog.text


def test_missing_file_yields_empty_map(tmp_path: Path, caplog) -> None:
    m = AppMap.from_file(tmp_path / "nope.toml")
    assert m.rule_count == 0
    assert m.classify("anything") is None
    assert "not found" in caplog.text


def test_malformed_toml_yields_empty_map(tmp_path: Path, caplog) -> None:
    m = AppMap.from_file(_write(tmp_path, "this is = = not toml"))
    assert m.rule_count == 0
    assert "cannot read" in caplog.text


def test_shipped_default_rules_file_is_valid() -> None:
    m = AppMap.from_file(_DEFAULT_RULES)
    assert m.rule_count > 0
    # a couple of anchors the daemon relies on
    assert m.classify("dev.zed.Zed") == "domain:engineering"
    assert m.classify("md.obsidian.Obsidian") == "domain:research"
    assert m.classify("com.slack.Slack") == "domain:comms"
