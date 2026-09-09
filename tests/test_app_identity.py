# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B17 · `AppIdentity` — one canonical key per real application."""

from __future__ import annotations

from pathlib import Path

import pytest

from neuropaca.diagnosis.app_identity import AppIdentity, normalise

_DEFAULT = Path(__file__).resolve().parents[1] / "data" / "app_identity.default.toml"


@pytest.fixture
def ident() -> AppIdentity:
    return AppIdentity.from_file(_DEFAULT)


# ------------------------------------------------------------------ normalise
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Brave-Browser", "brave"),
        ("brave-browser", "brave"),
        ("firefox", "firefox"),
        ("foot", "foot"),
        ("code-desktop", "code"),
        ("Some_App.Name", "some-app-name"),
        ("  spaced  name ", "spaced-name"),
        ("---weird---", "weird"),
        ("zed-bin", "zed"),
    ],
)
def test_normalise(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


# ------------------------------------------------------------------ resolve
def test_alias_collapses_the_two_naming_schemes(ident: AppIdentity) -> None:
    # the exact bug: Wayland app_id and process name -> one slug
    assert ident.resolve("com.system76.CosmicFiles") == "cosmic-files"
    assert ident.resolve("cosmic-files") == "cosmic-files"
    assert ident.resolve("com.anthropic.Claude") == "claude"
    assert ident.resolve("claude") == "claude"
    assert ident.resolve("claude-desktop") == "claude"
    assert ident.resolve("brave-browser") == "brave"
    assert ident.resolve("brave") == "brave"
    assert ident.resolve("md.obsidian.Obsidian") == "obsidian"
    assert ident.resolve("obsidian") == "obsidian"


def test_alias_is_case_insensitive(ident: AppIdentity) -> None:
    assert ident.resolve("BRAVE-BROWSER") == "brave"
    assert ident.resolve("Claude") == "claude"


def test_unknown_app_passes_through_normalised(ident: AppIdentity) -> None:
    assert ident.resolve("my-weird-tool") == "my-weird-tool"
    assert ident.resolve("Totally.New.App") == "totally-new-app"


def test_empty_input_is_empty(ident: AppIdentity) -> None:
    assert ident.resolve("") == ""
    assert ident.resolve(None) == ""


# ------------------------------------------------------------------ is_non_app
@pytest.mark.parametrize(
    "name",
    ["MainThread", "Thread-7", "Thread-1 (worker)", "asyncio_0", "sh", "bash", "gmain", "?"],
)
def test_is_non_app_true(ident: AppIdentity, name: str) -> None:
    assert ident.is_non_app(name) is True


@pytest.mark.parametrize("name", ["brave", "zed", "cosmic-files", "neuropacad", "python3"])
def test_is_non_app_false_for_real_processes(ident: AppIdentity, name: str) -> None:
    # neuropacad / python3 are *excluded* elsewhere (config list) but they ARE
    # processes — is_non_app is only about thread labels / shells
    assert ident.is_non_app(name) is False


def test_is_non_app_none_is_true(ident: AppIdentity) -> None:
    assert ident.is_non_app(None) is True


# ------------------------------------------------------------------ pretty
@pytest.mark.parametrize(
    ("canon", "expected"),
    [
        ("cosmic-files", "Cosmic Files"),
        ("brave", "Brave"),
        ("vscode", "VS Code"),
        ("github", "GitHub"),
        ("google-gemini", "Google Gemini"),
        ("mental-models", "Mental Models"),
        ("", ""),
    ],
)
def test_pretty(ident: AppIdentity, canon: str, expected: str) -> None:
    assert ident.pretty(canon) == expected


# ------------------------------------------------------------------ degradation
def test_missing_file_degrades_to_normalise_only(tmp_path: Path) -> None:
    ident = AppIdentity.from_file(tmp_path / "nope.toml")
    assert ident.resolve("brave-browser") == "brave"  # normaliser still works
    assert ident.resolve("com.anthropic.Claude") == "com-anthropic-claude"  # no alias


def test_malformed_sections_are_skipped() -> None:
    ident = AppIdentity.from_dict({"alias": "not a table", "non_app": "not a list", "bogus": 1})
    assert ident.resolve("x") == "x"


def test_default_file_has_the_known_dupes(ident: AppIdentity) -> None:
    # guards against someone trimming the shipped table below usefulness
    assert ident.alias_count >= 20
