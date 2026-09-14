# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.2 Step 4 · Tier 2 app resolution (action/app_registry.py)."""

from __future__ import annotations

from pathlib import Path

from neuropaca.action.app_registry import (
    list_installed_apps,
    resolve_app_name,
)

_FIXTURE_APPS = {
    "Brave Browser": "brave-browser",
    "Calculator": "gnome-calculator",
    "Google Chrome": "google-chrome",
    "Terminal": "cosmic-term",
    "Text Editor": "gnome-text-editor",
    "Visual Studio Code": "code",
    "Files": "nautilus",
}


def test_resolve_app_name_exact_match() -> None:
    matches = resolve_app_name("Calculator", _FIXTURE_APPS)
    assert len(matches) >= 1
    assert matches[0][0] == "Calculator"
    assert matches[0][1] == 1.0


def test_resolve_app_name_close_word_match() -> None:
    # "brave" -> "Brave Browser"
    matches = resolve_app_name("brave", _FIXTURE_APPS)
    assert len(matches) == 1
    assert matches[0][0] == "Brave Browser"
    assert matches[0][1] >= 0.6


def test_resolve_app_name_prefix_match() -> None:
    # "calc" -> "Calculator"
    matches = resolve_app_name("calc", _FIXTURE_APPS)
    assert len(matches) >= 1
    assert matches[0][0] == "Calculator"


def test_resolve_app_name_zero_matches() -> None:
    matches = resolve_app_name("nonexistent_app_xyz", _FIXTURE_APPS)
    assert matches == []


def test_resolve_app_name_empty_query() -> None:
    matches = resolve_app_name("", _FIXTURE_APPS)
    assert matches == []


def test_resolve_app_name_multiple_close_matches() -> None:
    # "Browser" -> both "Brave Browser" and something with Browser if present
    multi_apps = {
        "Brave Browser": "brave",
        "Firefox Web Browser": "firefox",
        "Calculator": "calc",
    }
    matches = resolve_app_name("Browser", multi_apps)
    assert len(matches) == 2
    names = [m[0] for m in matches]
    assert "Brave Browser" in names
    assert "Firefox Web Browser" in names


def test_parse_desktop_file_and_list_installed_apps(tmp_path: Path) -> None:
    app_dir = tmp_path / "applications"
    app_dir.mkdir()

    # Valid desktop file
    (app_dir / "calc.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Mock Calculator\nExec=mock-calc %U\n",
        encoding="utf-8",
    )

    # Hidden desktop file (should be skipped)
    (app_dir / "hidden.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Hidden App\nExec=hidden-cmd\nHidden=true\n",
        encoding="utf-8",
    )

    # NoDisplay desktop file (should be skipped)
    (app_dir / "nodisplay.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=NoDisplay App\n"
        "Exec=nodisplay-cmd\n"
        "NoDisplay=true\n",
        encoding="utf-8",
    )

    # Non-Application desktop file (should be skipped)
    (app_dir / "link.desktop").write_text(
        "[Desktop Entry]\nType=Link\nName=Web Link\nURL=https://example.com\n",
        encoding="utf-8",
    )

    apps = list_installed_apps(search_dirs=[app_dir])
    assert "Mock Calculator" in apps
    assert apps["Mock Calculator"] == "mock-calc"
    assert "Hidden App" not in apps
    assert "NoDisplay App" not in apps
    assert "Web Link" not in apps


# gen-ref: b3d01b64
