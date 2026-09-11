# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""The graph view's node detail panel: the data a click shows, and the
plain-English "what the system is learning from this node" line.

The line is built in `scripts/neuropaca_graph.py` from the node's own recorded
data and edges — deterministic, no model — so it is tested here like any other
data shaping. The page only displays it.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "scripts" / "graph_view_template.html"


def _load():
    spec = importlib.util.spec_from_file_location(
        "neuropaca_graph_panel", REPO / "scripts" / "neuropaca_graph.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["neuropaca_graph_panel"] = mod
    spec.loader.exec_module(mod)
    return mod


gv = _load()
NOW = datetime(2026, 9, 11, 18, 0, tzinfo=UTC)
T = "2026-09-11T12:00:00+00:00"
RAW_ID = ("app:", "webapp:", "domain:", "insight:", "idle:", "ephemeral:")


def _n(nid, ntype, label, **extra):
    return {
        "id": nid,
        "node_type": ntype,
        "label": label,
        "created_at": T,
        "last_accessed": "2026-09-11T17:00:00+00:00",
        "access_count": 0,
        "relevance_score": 1.0,
        "priority": 0,
        **extra,
    }


def _e(s, t, rel="related_to", w=0.0):
    return {"source": s, "target": t, "relation": rel, "weight": w, "created_at": T}


def _graph():
    nodes = [
        _n("YOU", "concept", "YOU"),
        _n("domain:habits", "concept", "Habits"),
        _n("domain:system", "concept", "System"),
        _n(
            "app:brave",
            "app",
            "Brave",
            access_count=319,
            relevance_score=9.1,
            ram_mb=2557.1,
            resources_at="2026-09-11T16:00:00+00:00",
        ),
        _n("app:cosmic-term", "app", "Cosmic Term", access_count=80, relevance_score=8.0),
        _n("app:lonely", "app", "Lonely", access_count=1, relevance_score=0.5),
        _n("webapp:github", "webapp", "GitHub", access_count=12, relevance_score=4.0),
        _n(
            "insight:1",
            "insight",
            "Anomaly on Brave (idle)",
            spec={"kind": "insight", "refs": ["app:brave"], "facet": "anomaly/idle", "value": 0.81},
        ),
        _n(
            "idle:1",
            "idle_thought",
            "How does Cosmic Term affect Brave?",
            access_count=3,
            spec={
                "kind": "thought",
                "refs": ["app:cosmic-term", "app:brave"],
                "facet": "how_does_x_affect_y",
                "text": "How does Cosmic Term affect Brave?",
            },
        ),
        _n(
            "ephemeral:1",
            "concept",
            "Pressure 1.65 on Brave",
            spec={
                "kind": "probe",
                "refs": ["app:brave"],
                "facet": "summary/L4.anomaly",
                "value": 1.645,
            },
        ),
    ]
    edges = [
        _e("domain:habits", "YOU", "part_of"),
        _e("app:brave", "domain:habits", "part_of"),
        _e("webapp:github", "app:brave", "part_of"),
        _e("app:brave", "app:cosmic-term", "related_to", 0.62),
        _e("webapp:github", "app:brave", "related_to", 0.15),
        _e("app:lonely", "YOU", "related_to", 0.0),
        _e("insight:1", "app:brave"),
        _e("ephemeral:1", "app:brave"),
        _e("ephemeral:1", "insight:1", "caused_by"),
        _e("idle:1", "app:cosmic-term"),
        _e("idle:1", "app:brave"),
    ]
    return {"schema_version": 8, "nodes": nodes, "edges": edges}


@pytest.fixture(scope="module")
def about() -> dict[str, str]:
    payload = gv.build_payload(_graph(), now=NOW)
    return {n["id"]: n["about"] for n in payload["nodes"]}


# ================================================================ every node


def test_every_node_gets_a_description_with_no_raw_ids(about) -> None:
    for node_id, text in about.items():
        assert text.strip(), f"{node_id} has no description"
        assert not any(p in text for p in RAW_ID), f"{node_id} leaks a raw id: {text}"


def test_the_payload_carries_everything_the_panel_shows() -> None:
    node = next(n for n in gv.build_payload(_graph(), now=NOW)["nodes"] if n["id"] == "app:brave")
    for key in (
        "kind",
        "about",
        "activity",
        "created",
        "last_accessed",
        "first_seen",
        "last_seen",
        "resources_at",
        "spec",
        "score",
        "access",
        "ram",
    ):
        assert key in node, key
    assert node["kind"] == "app"


# ============================================================ what it says


def test_an_app_says_how_used_its_rank_its_topic_and_what_it_goes_with(about) -> None:
    text = about["app:brave"]
    assert "319 times" in text and "most recently 1 h ago" in text
    assert "#1 of your 4 apps and sites" in text
    assert "under Habits" in text
    # strongest learned partner first, with a plain strength word
    assert text.index("Cosmic Term (strongly)") < text.index("GitHub (sometimes)")
    assert "1 unusual pattern" in text
    assert "1 question" in text
    assert "2557 MiB" in text


def test_an_app_with_no_co_use_says_so(about) -> None:
    assert "not yet learned what Lonely goes with" in about["app:lonely"]
    assert "not filed under a topic yet" in about["app:lonely"]


def test_a_site_names_the_browser_it_lives_in(about) -> None:
    assert "inside Brave" in about["webapp:github"]


def test_a_topic_lists_what_belongs_to_it(about) -> None:
    assert "1 app or site belongs here: Brave" in about["domain:habits"]
    assert "Nothing is filed here yet" in about["domain:system"]


def test_the_person_node_explains_itself(about) -> None:
    assert about["YOU"].startswith("This is you")
    assert "Habits" in about["YOU"]


def test_an_insight_says_what_was_noticed_and_what_backs_it(about) -> None:
    text = about["insight:1"]
    assert "an anomaly involving Brave while you were away from the computer" in text
    assert "confidence 0.81" in text
    assert "1 follow-up note" in text


def test_an_idle_thought_quotes_the_question_it_asked(about) -> None:
    text = about["idle:1"]
    assert "“How does Cosmic Term affect Brave?”" in text
    assert "Cosmic Term and Brave" in text


def test_a_probe_explains_the_pressure_in_words(about) -> None:
    text = about["ephemeral:1"]
    assert "pressure of 1.65 built up on Brave" in text
    assert "learning layer spotted an anomaly" in text
    assert "14 days" in text


# ================================================================ the page


def test_the_panel_is_never_hidden_by_window_width() -> None:
    """Why the panel never showed: a narrow (tiled) Brave window force-hid it."""
    assert "#inspect{display:none!important}" not in TEMPLATE.read_text("utf-8")


def test_the_panel_has_the_learning_line_and_connection_list() -> None:
    html = TEMPLATE.read_text("utf-8")
    for element in ('id="iAbout"', 'id="iConns"', 'id="iNums"', 'id="iClose"'):
        assert element in html, element


def test_node_data_reaches_the_page_as_text_never_as_markup() -> None:
    """Labels are window titles and app names; the panel must not interpret them."""
    html = TEMPLATE.read_text("utf-8")
    panel = html[html.index("function showInspect") : html.index("// ---- chrome")]
    assert "innerHTML" not in panel
