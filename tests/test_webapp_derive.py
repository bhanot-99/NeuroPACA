"""B14 · derive_webapp — the title membrane.

Literal window titles in, an allowlisted label (or None) out. No Wayland, no
daemon. The privacy assertion — that nothing but the label crosses — lives in
test_webapp_privacy.py; here we check the matching itself.
"""

from __future__ import annotations

import pytest

from neuropaca.sensing.activity.webapp import WebAppMap, derive_webapp

_BROWSERS = frozenset({"brave-browser"})
_MAP = WebAppMap.from_dict(
    {
        "webapp": {
            "gmail": "comms",
            "youtube": "habits",
            "github": "engineering",
            "google gemini": "tools",
            "google docs": "projects",
            "crunchyroll": "habits",
            "google search": "tools",
        }
    }
)


def _derive(app_id: str, title: str, *, enabled: bool = True):
    return derive_webapp(
        app_id, title, browsers=_BROWSERS, webapp_map=_MAP, enabled=enabled
    )


@pytest.mark.parametrize(
    ("title", "label"),
    [
        ("Inbox (351) - bhanot1054@gmail.com - Gmail - Brave", "gmail"),
        ("Rick Astley - Never Gonna Give You Up - YouTube - Brave", "youtube"),
        ("bhanot-99/NeuroPACA: local-first agent · GitHub - Brave", "github"),
        ("Google Gemini - Brave", "google-gemini"),
        ("B14 plan - Google Docs - Brave", "google-docs"),
        ("Gmail - Brave", "gmail"),  # no unread suffix
        ("Inbox — bhanot1054@gmail.com — Gmail — Mozilla Firefox", "gmail"),  # em-dash browser
        # site name embedded in the last segment, not a segment of its own
        ("Frieren Episode 5 - Watch on Crunchyroll - Brave", "crunchyroll"),
        ("neuropaca asyncio - Google Search - Brave", "google-search"),
    ],
)
def test_real_titles_match_their_site(title: str, label: str) -> None:
    hit = _derive("brave-browser", title)
    assert hit is not None and hit.label == label


def test_site_word_in_last_segment_needs_a_real_delimiter() -> None:
    # single-segment title that merely contains the word must NOT match
    assert _derive("brave-browser", "watch on crunchyroll without ads - Brave") is None


def test_non_browser_app_id_is_never_matched() -> None:
    assert _derive("md.obsidian.Obsidian", "phases - NeuroPaca - Obsidian") is None
    assert _derive("com.system76.CosmicTerm", "gmail - COSMIC Terminal") is None


def test_disabled_short_circuits() -> None:
    assert _derive("brave-browser", "Gmail - Brave", enabled=False) is None


def test_unrecognised_site_returns_none() -> None:
    assert _derive("brave-browser", "MyBank - Account Summary - Brave") is None


def test_site_name_only_matches_as_a_whole_segment() -> None:
    # an article that merely mentions Gmail must not classify as Gmail
    assert _derive("brave-browser", "How I quit Gmail forever - Some Blog - Brave") is None


def test_empty_title_is_none() -> None:
    assert _derive("brave-browser", "") is None
