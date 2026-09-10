# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B17 · `scripts/neuropaca_graph_window.py` — the pure half.

`pretty_label`, `Layout.hub_position` / `pin_hubs`, `GraphData.reload`. The GTK
`_run()` is verified live (it lazy-imports `gi`, which the project .venv does not
carry — same split as `scripts/soak_tray.py`).
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "neuropaca_graph_window.py"
_spec = importlib.util.spec_from_file_location("neuropaca_graph_window", _MODULE_PATH)
assert _spec and _spec.loader
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)


# ----------------------------------------------------------------- pretty_label
def test_pretty_label_hub_and_root() -> None:
    assert gw.pretty_label("YOU", {}) == "You"
    assert gw.pretty_label("domain:engineering", {"label": "engineering"}) == "Engineering"
    assert gw.pretty_label("domain:mental_models", {}) == "Mental Models"


def test_pretty_label_apps_use_the_canonical_slug() -> None:
    assert gw.pretty_label("app:cosmic-files", {"label": "cosmic-files"}) == "Cosmic Files"
    assert gw.pretty_label("app:brave", {"label": "brave"}) == "Brave"
    assert gw.pretty_label("webapp:github", {"label": "github"}) == "GitHub"
    assert gw.pretty_label("webapp:google-gemini", {"label": "google-gemini"}) == "Google Gemini"


def test_pretty_label_legacy_insight_uses_first_line() -> None:
    """A node with no spec (unparsed legacy text) renders its label's first line."""
    assert gw.pretty_label("insight:abc", {"label": "You focus best mid-morning\nmore"}).startswith(
        "You focus best"
    )
    assert gw.pretty_label("insight:abc", {}) == "insight:abc"


def test_pretty_label_renders_the_spec_with_current_ref_names() -> None:
    """B18 issue 3: two probes on different apps no longer both read "Learning"."""
    nodes = {"app:brave": {"label": "brave"}, "app:code": {"label": "code"}}

    def probe(ref: str) -> dict:
        return {"spec": {"kind": "probe", "refs": [ref], "facet": "source/learning"}}

    assert gw.pretty_label("ephemeral:1", probe("app:brave"), nodes, "short") == (
        "Brave · via learning"
    )
    assert gw.pretty_label("ephemeral:2", probe("app:code"), nodes, "short") == (
        "VS Code · via learning"
    )


def test_captions_disambiguate_only_real_collisions() -> None:
    data = gw.GraphData()
    spec = {"kind": "probe", "refs": ["app:brave"], "facet": "summary/L4.anomaly"}
    data.nodes = {
        "app:brave": {"label": "brave"},
        "ephemeral:a": {"spec": {**spec, "value": 1.43}, "created_at": "2026-09-10T10:05:00"},
        "ephemeral:b": {"spec": {**spec, "value": 1.44}, "created_at": "2026-09-10T11:30:00"},
    }
    data.mtime = 1.0
    caps = data.captions()
    assert caps["app:brave"] == "Brave"
    assert caps["ephemeral:a"] != caps["ephemeral:b"]
    assert caps["ephemeral:a"].endswith("10:05")


# ----------------------------------------------------------------- hub geometry
def test_hub_position_places_you_at_origin() -> None:
    assert gw.Layout.hub_position("YOU") == (0.0, 0.0)


def test_hub_position_is_a_deterministic_ring() -> None:
    pts = [gw.Layout.hub_position(f"domain:{s}") for s in gw.DOMAIN_ORDER]
    assert all(p is not None for p in pts)
    # every hub is exactly HUB_RING from the origin
    for x, y in pts:  # type: ignore[misc]
        assert math.isclose(math.hypot(x, y), gw.HUB_RING, rel_tol=1e-6)
    # 10 distinct evenly-spaced angles, first at 12 o'clock
    assert math.isclose(pts[0][1], -gw.HUB_RING, rel_tol=1e-6)  # type: ignore[index]
    assert len({(round(x, 3), round(y, 3)) for x, y in pts}) == 10  # type: ignore[misc]


def test_hub_position_none_for_non_hub() -> None:
    assert gw.Layout.hub_position("app:brave") is None
    assert gw.Layout.hub_position("domain:not_a_real_slug") is None


def test_pin_hubs_fixes_and_pins_every_master_node(tmp_path: Path) -> None:
    graph = {
        "schema_version": 4,
        "nodes": [{"id": "YOU", "node_type": "person", "label": "YOU"}]
        + [{"id": f"domain:{s}", "node_type": "concept", "label": s} for s in gw.DOMAIN_ORDER]
        + [{"id": "app:brave", "node_type": "app", "label": "brave"}],
        "edges": [],
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph))
    data = gw.GraphData()
    data.reload(p)

    layout = gw.Layout()
    layout.sync(data)  # calls pin_hubs

    for s in gw.DOMAIN_ORDER:
        hid = f"domain:{s}"
        assert hid in layout.pinned
        assert tuple(layout.pos[hid]) == gw.Layout.hub_position(hid)
    assert "YOU" in layout.pinned
    assert "app:brave" not in layout.pinned  # a normal node still floats


def test_pin_hubs_overrides_drift(tmp_path: Path) -> None:
    graph = {
        "schema_version": 4,
        "nodes": [{"id": "domain:tools", "node_type": "concept", "label": "tools"}],
        "edges": [],
    }
    p = tmp_path / "g.json"
    p.write_text(json.dumps(graph))
    data = gw.GraphData()
    data.reload(p)
    layout = gw.Layout()
    layout.sync(data)
    layout.pos["domain:tools"] = [999.0, -42.0]  # simulate a stale drag
    layout.pin_hubs(data)
    assert tuple(layout.pos["domain:tools"]) == gw.Layout.hub_position("domain:tools")

# gen-ref: 1b6620f2
