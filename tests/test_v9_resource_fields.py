# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""V-9 · `ram_mb` / `cpu_percent` are always 0.0 (VISION.md §5).

The premise turned out to be half right. The census path works: on the live
graph `ram_mb` is non-zero on 11 of 27 app nodes, every one of them an app over
`process_min_rss_mb`. What is real is that `_node_record` wrote `0.0` on **every**
node — concepts, hubs, probes, web-apps, apps under the census threshold — so a
node that was never measured was indistinguishable from one measured at zero,
and every record carried two dead floats.

Now `None` means never measured, and the record omits the keys. A reading also
gains `resources_at`, its own timestamp: the census used to be the ONLY writer
of `last_seen_at`, so that doubled as the reading's time, and the B13 merge rule
took ram/cpu from "whichever side was seen last". The moment anything else
counts as a sighting (V-10), that rule would hand a merge the numbers of the
side that was never measured. `resources_at` decouples the two.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from neuropaca.core.enums import NodeType
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Node
from neuropaca.diagnosis.patterns import _heavy_app_specs

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


async def _graph(tmp_path, name: str = "g.json") -> GraphMemory:
    GraphMemory._reset_for_tests()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / name))
    await gm.load()
    return gm


async def _reload(tmp_path, name: str = "g.json") -> GraphMemory:
    return await _graph(tmp_path, name)


def _saved(tmp_path, name: str = "g.json") -> dict[str, dict]:
    raw = json.loads((tmp_path / name).read_text())
    return {n["id"]: n for n in raw["nodes"]}


def _legacy_record(node_id: str, **extra) -> dict:
    now = T0.isoformat()
    return {
        "id": node_id,
        "node_type": "app",
        "label": node_id,
        "created_at": now,
        "last_accessed": now,
        "access_count": 3,
        "relevance_score": 1.0,
        "priority": 0,
        **extra,
    }


async def _load_legacy(tmp_path, version: int, *records: dict) -> GraphMemory:
    (tmp_path / "old.json").write_text(
        json.dumps({"schema_version": version, "nodes": list(records), "edges": []})
    )
    return await _graph(tmp_path, "old.json")


# ================================================= 1 · unmeasured means None


def test_a_node_has_no_reading_until_one_is_taken() -> None:
    node = Node(id="app:x", node_type=NodeType.APP, label="x")
    assert node.ram_mb is None and node.cpu_percent is None and node.resources_at is None


async def test_a_node_the_census_never_saw_has_no_reading(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:foot", NodeType.APP, {"label": "foot"})
    node = gm.get_node("app:foot")
    assert node.ram_mb is None and node.cpu_percent is None


async def test_an_unmeasured_node_writes_no_resource_keys(tmp_path) -> None:
    """The dead floats: every concept, hub, probe and web-app used to carry them."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:foot", NodeType.APP, None)
    await gm.save()

    for node_id, record in _saved(tmp_path).items():
        for key in ("ram_mb", "cpu_percent", "resources_at"):
            assert key not in record, f"{node_id} still writes {key}"


# ============================================== 2 · a real reading is kept whole


async def test_a_measured_node_writes_its_reading_and_when(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node(
        "app:brave",
        NodeType.APP,
        {"label": "brave", "ram_mb": 2557.1, "cpu_percent": 9.2, "resources_at": T0},
    )
    await gm.save()

    record = _saved(tmp_path)["app:brave"]
    assert record["ram_mb"] == 2557.1
    assert record["cpu_percent"] == 9.2
    assert record["resources_at"] == T0.isoformat()


async def test_a_measured_zero_is_not_mistaken_for_unmeasured(tmp_path) -> None:
    """`app:code` on the live graph: 2337.7 MB, 0.0 % CPU. A real idle zero must
    survive — only *never measured* becomes absent."""
    gm = await _graph(tmp_path)
    await gm.add_node(
        "app:code",
        NodeType.APP,
        {"label": "code", "ram_mb": 2337.7, "cpu_percent": 0.0, "resources_at": T0},
    )
    await gm.save()
    assert _saved(tmp_path)["app:code"]["cpu_percent"] == 0.0

    reloaded = await _reload(tmp_path)
    assert reloaded.get_node("app:code").cpu_percent == 0.0


async def test_a_reading_round_trips(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node(
        "app:brave", NodeType.APP, {"ram_mb": 812.0, "cpu_percent": 4.5, "resources_at": T0}
    )
    await gm.add_node("app:foot", NodeType.APP, None)
    await gm.save()

    reloaded = await _reload(tmp_path)
    brave, foot = reloaded.get_node("app:brave"), reloaded.get_node("app:foot")
    assert (brave.ram_mb, brave.cpu_percent, brave.resources_at) == (812.0, 4.5, T0)
    assert (foot.ram_mb, foot.cpu_percent, foot.resources_at) == (None, None, None)


def test_the_census_stamps_when_it_measured() -> None:
    (spec,) = _heavy_app_specs(
        [{"name": "brave", "rss_mb": 2048.0, "cpu_percent": 3.0, "running_seconds": 60}], T0
    )
    attrs = dict(spec.attributes)
    assert attrs["resources_at"] == T0.isoformat()
    assert attrs["ram_mb"] == 2048.0


async def test_an_upserted_resources_at_string_becomes_a_datetime(tmp_path) -> None:
    """The census hands attributes as ISO strings; the upsert must parse this
    key like the other timestamps or a save would write a str where a datetime
    is expected."""
    gm = await _graph(tmp_path)
    await gm.add_node("app:brave", NodeType.APP, None)
    await gm.upsert_node(
        "app:brave",
        NodeType.APP,
        {"ram_mb": 900.0, "cpu_percent": 1.0, "resources_at": T0.isoformat()},
    )
    assert gm.get_node("app:brave").resources_at == T0


# ============================== 3 · reading an older file loses nothing


async def test_a_v7_zero_reading_becomes_never_measured(tmp_path) -> None:
    gm = await _load_legacy(tmp_path, 7, _legacy_record("app:foot", ram_mb=0.0, cpu_percent=0.0))
    node = gm.get_node("app:foot")
    assert (node.ram_mb, node.cpu_percent, node.resources_at) == (None, None, None)


async def test_a_v7_real_reading_keeps_its_numbers_and_gains_its_time(tmp_path) -> None:
    """Before v8 only the census wrote `last_seen_at`, and it wrote it in the
    same NodeSpec as the reading — so it IS the reading's timestamp."""
    seen = T0 - timedelta(hours=5)
    gm = await _load_legacy(
        tmp_path,
        7,
        _legacy_record(
            "app:brave",
            ram_mb=2557.1,
            cpu_percent=9.2,
            first_seen_at=seen.isoformat(),
            last_seen_at=seen.isoformat(),
        ),
    )
    node = gm.get_node("app:brave")
    assert (node.ram_mb, node.cpu_percent) == (2557.1, 9.2)
    assert node.resources_at == seen


async def test_a_v7_measured_idle_cpu_survives_conversion(tmp_path) -> None:
    gm = await _load_legacy(
        tmp_path,
        7,
        _legacy_record("app:code", ram_mb=2337.7, cpu_percent=0.0, last_seen_at=T0.isoformat()),
    )
    assert gm.get_node("app:code").cpu_percent == 0.0


async def test_a_v2_file_with_no_resource_keys_at_all_reads_as_unmeasured(tmp_path) -> None:
    gm = await _load_legacy(tmp_path, 2, _legacy_record("app:old"))
    assert gm.get_node("app:old").ram_mb is None


# ============= 4 · the merge takes the reading from whoever MEASURED, not was SEEN


async def test_a_merge_keeps_the_reading_when_the_other_side_was_only_seen(tmp_path) -> None:
    """The regression V-9 exists to prevent. Keyed on `last_seen_at`, a merge
    would take the more-recently-*seen* side's numbers — and once a focus event
    counts as a sighting (V-10) that is usually the side never measured, so a
    3.8 GB reading would be replaced by nothing."""
    gm = await _graph(tmp_path)
    await gm.add_node(
        "app:brave",
        NodeType.APP,
        {
            "ram_mb": 3811.0,
            "cpu_percent": 4.0,
            "resources_at": T0,
            "last_seen_at": T0,
        },
    )
    await gm.add_node("app:brave-browser", NodeType.APP, {"last_seen_at": T0 + timedelta(hours=2)})

    assert gm._merge_nodes_unsafe("app:brave", "app:brave-browser")

    merged = gm.get_node("app:brave")
    assert merged.ram_mb == 3811.0 and merged.resources_at == T0
    assert merged.last_seen_at == T0 + timedelta(hours=2)  # the sighting still moves


async def test_a_merge_takes_the_newer_reading(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node(
        "app:a", NodeType.APP, {"ram_mb": 100.0, "cpu_percent": 1.0, "resources_at": T0}
    )
    later = T0 + timedelta(minutes=10)
    await gm.add_node(
        "app:b", NodeType.APP, {"ram_mb": 250.0, "cpu_percent": 7.0, "resources_at": later}
    )

    assert gm._merge_nodes_unsafe("app:a", "app:b")

    merged = gm.get_node("app:a")
    assert (merged.ram_mb, merged.cpu_percent, merged.resources_at) == (250.0, 7.0, later)


async def test_a_merge_of_two_unmeasured_nodes_stays_unmeasured(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:a", NodeType.APP, None)
    await gm.add_node("app:b", NodeType.APP, None)
    assert gm._merge_nodes_unsafe("app:a", "app:b")
    assert gm.get_node("app:a").ram_mb is None
