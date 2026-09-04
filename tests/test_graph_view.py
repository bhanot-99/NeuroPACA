"""B9 companion · the graph viewer's data shaping (`scripts/neuropaca_graph.py`).

The force simulation and the canvas live in `graph_view_template.html` and are
not exercised here -- the same split `tests/test_b9_soak_tray.py` accepts for the
GTK glue. What *is* tested is everything that decides what the page receives:
domain derivation, the time-offset conversion, the escaping, and the zero-egress
guarantee the output has to carry (rules.md §6).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "scripts" / "neuropaca_graph.py"
TEMPLATE = REPO / "scripts" / "graph_view_template.html"


def _load():
    spec = importlib.util.spec_from_file_location("neuropaca_graph", MODULE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["neuropaca_graph"] = mod
    spec.loader.exec_module(mod)
    return mod


gv = _load()

T0 = "2026-09-04T12:00:00+00:00"
T1 = "2026-09-04T13:00:00+00:00"
T2 = "2026-09-05T12:00:00+00:00"


def _node(nid, ntype="file", created=T0, score=1.0, access=0, label=None):
    return {
        "id": nid,
        "node_type": ntype,
        "label": label or nid,
        "created_at": created,
        "last_accessed": created,
        "access_count": access,
        "relevance_score": score,
        "priority": 0,
        "surfaced_at": None,
    }


def _edge(s, t, created=T0, weight=0.0, relation="related_to"):
    return {
        "source": s,
        "target": t,
        "relation": relation,
        "weight": weight,
        "created_at": created,
    }


def _graph(nodes, edges):
    return {"schema_version": 2, "nodes": nodes, "edges": edges}


# ------------------------------------------------------------------- domains
def test_a_node_is_clustered_with_the_domain_hub_it_is_wired_to() -> None:
    nodes = [_node("YOU", "concept"), _node("domain:engineering", "concept"), _node("file:/a.py")]
    edges = [_edge("file:/a.py", "domain:engineering")]
    assert gv.derive_domains(nodes, edges) == {"file:/a.py": "domain:engineering"}


def test_domain_derivation_works_in_either_edge_direction() -> None:
    nodes = [_node("domain:tools", "concept"), _node("app:code", "app")]
    assert gv.derive_domains(nodes, [_edge("domain:tools", "app:code")]) == {
        "app:code": "domain:tools"
    }


def test_a_bridge_node_takes_the_first_domain_deterministically() -> None:
    """A node wired to several domains is a bridge. The layout only needs
    somewhere to put it; `bridge_value` is what actually scores the reach."""
    nodes = [_node("domain:a", "concept"), _node("domain:b", "concept"), _node("file:/x.py")]
    edges = [_edge("file:/x.py", "domain:b"), _edge("file:/x.py", "domain:a")]
    once = gv.derive_domains(nodes, edges)
    assert once == {"file:/x.py": "domain:b"}
    assert gv.derive_domains(nodes, edges) == once, "must not depend on set ordering"


def test_hubs_are_never_given_a_domain_of_their_own() -> None:
    nodes = [_node("domain:a", "concept"), _node("domain:b", "concept")]
    assert gv.derive_domains(nodes, [_edge("domain:a", "domain:b")]) == {}


def test_an_unconnected_node_simply_has_no_domain() -> None:
    nodes = [_node("domain:a", "concept"), _node("file:/lonely.py")]
    assert gv.derive_domains(nodes, []) == {}


# ------------------------------------------------------------------- payload
def test_times_become_seconds_from_the_earliest_record() -> None:
    g = _graph([_node("a", created=T0), _node("b", created=T1)], [])
    p = gv.build_payload(g)
    assert p["t0"].startswith("2026-09-04T12:00:00")
    assert p["span_seconds"] == 3600.0
    assert {n["id"]: n["t"] for n in p["nodes"]} == {"a": 0.0, "b": 3600.0}


def test_an_edge_created_after_its_nodes_keeps_its_own_timestamp() -> None:
    """The replay has to show the edge forming later than the nodes it joins."""
    g = _graph([_node("a", created=T0), _node("b", created=T0)], [_edge("a", "b", created=T1)])
    assert gv.build_payload(g)["edges"][0]["at"] == 3600.0


def test_the_span_covers_edges_that_outlast_every_node() -> None:
    g = _graph([_node("a", created=T0), _node("b", created=T0)], [_edge("a", "b", created=T2)])
    assert gv.build_payload(g)["span_seconds"] == 86400.0


def test_hub_and_domain_ids_are_reported_for_the_layout() -> None:
    g = _graph(
        [
            _node("YOU", "concept"),
            _node("domain:tools", "concept"),
            _node("domain:comms", "concept"),
            _node("file:/a.py"),
        ],
        [],
    )
    p = gv.build_payload(g)
    assert set(p["hub_ids"]) == {"YOU", "domain:tools", "domain:comms"}
    assert p["domain_ids"] == ["domain:comms", "domain:tools"], "sorted, so slots are stable"
    assert p["root_id"] == "YOU"


def test_max_weight_drives_the_min_weight_slider_range() -> None:
    g = _graph([_node("a"), _node("b")], [_edge("a", "b", weight=1.75)])
    assert gv.build_payload(g)["max_weight"] == 1.75


def test_a_graph_with_no_edges_still_builds() -> None:
    p = gv.build_payload(_graph([_node("YOU", "concept")], []))
    assert p["max_weight"] == 0.0
    assert p["edges"] == []
    assert p["span_seconds"] == 0.0


def test_a_graph_with_no_nodes_is_refused_with_a_message() -> None:
    with pytest.raises(SystemExit, match="no nodes"):
        gv.build_payload(_graph([], []))


def test_a_zero_span_graph_still_produces_a_tick_label() -> None:
    assert len(gv.build_ticks(gv._parse_dt(T0), 0.0)) == 1


def test_a_multi_day_window_gets_date_ticks_not_clock_ticks() -> None:
    ticks = gv.build_ticks(gv._parse_dt(T0), 604800.0)
    assert len(ticks) == 7
    assert ":" not in ticks[0], "a week-long window is labelled by date, not time of day"


# -------------------------------------------------------------------- render
def test_render_splices_the_payload_into_the_template() -> None:
    g = _graph([_node("YOU", "concept"), _node("file:/a.py")], [_edge("file:/a.py", "YOU")])
    html = gv.render(gv.build_payload(g))
    assert gv.PLACEHOLDER not in html
    assert "file:/a.py" in html


def test_render_escapes_a_closing_script_tag_hiding_in_a_label() -> None:
    """A label is user-derived (a filename, a window title). `</script>` inside
    one would close the host element and spill the payload into the document."""
    g = _graph([_node("a", label="</script><b>x")], [])
    html = gv.render(gv.build_payload(g))
    assert "</script><b>x" not in html
    assert "<\\/script>" in html


def test_render_refuses_a_template_without_the_placeholder(tmp_path: Path) -> None:
    bad = tmp_path / "nope.html"
    bad.write_text("<html>no placeholder here</html>", encoding="utf-8")
    with pytest.raises(SystemExit, match="placeholder"):
        gv.render(gv.build_payload(_graph([_node("a")], [])), template=bad)


def test_load_graph_says_what_to_do_when_the_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no graph at"):
        gv.load_graph(tmp_path / "absent.json")


def test_load_graph_rejects_a_file_that_is_not_a_graph(tmp_path: Path) -> None:
    p = tmp_path / "other.json"
    p.write_text(json.dumps({"something": "else"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="not a NeuroPACA graph"):
        gv.load_graph(p)


# --------------------------------------------------------------- zero egress
def test_the_rendered_page_makes_no_outbound_reference() -> None:
    """rules.md §6 — a viewer for a local-first daemon must not be the one thing
    in the project that phones home. Asserted on the rendered output rather than
    the template, because the payload is what changes between runs."""
    g = _graph([_node("YOU", "concept"), _node("file:/a.py")], [_edge("file:/a.py", "YOU")])
    html = gv.render(gv.build_payload(g))
    body = "\n".join(
        line for line in html.splitlines() if "ZERO EGRESS" not in line and "no <script" not in line
    )
    for forbidden in (
        "http://",
        "https://",
        "<script src",
        "<link ",
        "fetch(",
        "XMLHttpRequest",
        "@import",
        "WebSocket",
    ):
        assert forbidden not in body, f"{forbidden!r} would reach the network"


def test_the_template_ships_alongside_the_script() -> None:
    assert TEMPLATE.is_file(), "neuropaca_graph.py is useless without its template"
    assert gv.PLACEHOLDER in TEMPLATE.read_text(encoding="utf-8")
