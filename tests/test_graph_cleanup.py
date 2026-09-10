# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B17 · `scripts/graph_cleanup.py` — the one-off manual graph tidy."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "graph_cleanup.py"
_spec = importlib.util.spec_from_file_location("graph_cleanup", _MODULE_PATH)
assert _spec and _spec.loader
gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc)

_ALIAS = {"brave-browser": "brave", "com.system76.cosmicfiles": "cosmic-files"}
_NON_APP = {"mainthread"}


def _node(nid: str, **kw: object) -> dict[str, object]:
    return {
        "id": nid,
        "node_type": "app",
        "label": nid.split(":", 1)[-1],
        "access_count": 0,
        "relevance_score": 0.0,
        "ram_mb": 0.0,
        **kw,
    }


def test_normalise() -> None:
    assert gc._normalise("Brave-Browser") == "brave"
    assert gc._normalise("A.B_C") == "a-b-c"


def test_clean_folds_dupes_keeps_edges_and_resources() -> None:
    payload = {
        "nodes": [
            _node("app:brave-browser", access_count=5),
            _node("app:brave", ram_mb=3811.0, cpu_percent=12.0),
            _node("webapp:github", node_type="webapp"),
            _node("app:MainThread"),
            {"id": "YOU", "node_type": "person", "label": "YOU"},
        ],
        "edges": [
            {"source": "webapp:github", "target": "app:brave-browser", "relation": "part_of"},
        ],
    }
    cleaned, log = gc.clean(payload, {"brave-browser": "brave"}, {"mainthread"})
    ids = {n["id"] for n in cleaned["nodes"]}
    assert "app:brave-browser" not in ids
    assert "app:MainThread" not in ids
    assert "app:brave" in ids
    brave = next(n for n in cleaned["nodes"] if n["id"] == "app:brave")
    assert brave["access_count"] == 5 and brave["ram_mb"] == 3811.0
    # the edge followed the merge
    assert cleaned["edges"] == [
        {"source": "webapp:github", "target": "app:brave", "relation": "part_of"}
    ]
    assert any("merge" in line for line in log)


def test_clean_drops_edgeless_non_hub_but_spares_hubs() -> None:
    payload = {
        "nodes": [
            _node("app:lonely"),
            {"id": "YOU", "node_type": "person", "label": "YOU"},
            {"id": "domain:tools", "node_type": "concept", "label": "tools"},
        ],
        "edges": [],
    }
    cleaned, _log = gc.clean(payload, {}, set())
    ids = {n["id"] for n in cleaned["nodes"]}
    assert ids == {"YOU", "domain:tools"}


def test_clean_is_idempotent() -> None:
    payload = {
        "nodes": [_node("app:brave-browser", access_count=1), _node("app:brave", ram_mb=100.0)],
        "edges": [{"source": "app:brave", "target": "app:brave-browser", "relation": "x"}],
    }
    once, _ = gc.clean(json.loads(json.dumps(payload)), _ALIAS, _NON_APP)
    twice, log2 = gc.clean(json.loads(json.dumps(once)), _ALIAS, _NON_APP)
    assert {n["id"] for n in once["nodes"]} == {n["id"] for n in twice["nodes"]}
    assert log2 == []


def test_main_dry_run_does_not_write(tmp_path: Path, capsys) -> None:
    g = tmp_path / "graph.json"
    g.write_text(
        json.dumps(
            {
                "nodes": [_node("app:brave-browser", access_count=1), _node("app:brave")],
                "edges": [{"source": "app:brave", "target": "app:brave-browser", "relation": "x"}],
            }
        )
    )
    before = g.read_text()
    rc = gc.main(["--graph", str(g), "--identity", str(tmp_path / "none.toml")])
    assert rc == 0
    assert g.read_text() == before  # dry run
    assert "dry run" in capsys.readouterr().out


def test_main_apply_writes_valid_json(tmp_path: Path) -> None:
    g = tmp_path / "graph.json"
    g.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "nodes": [
                    _node("app:brave-browser", access_count=1),
                    _node("app:brave", ram_mb=99.0),
                    {"id": "YOU", "node_type": "person", "label": "YOU"},
                ],
                "edges": [
                    {"source": "app:brave", "target": "YOU", "relation": "related_to"},
                    {"source": "app:brave-browser", "target": "YOU", "relation": "related_to"},
                ],
            }
        )
    )
    ident = tmp_path / "id.toml"
    ident.write_text('[alias]\n"brave-browser" = "brave"\n')
    rc = gc.main(["--graph", str(g), "--identity", str(ident), "--apply"])
    assert rc == 0
    out = json.loads(g.read_text())
    assert {n["id"] for n in out["nodes"]} == {"app:brave", "YOU"}

# gen-ref: 58d9d9e3
