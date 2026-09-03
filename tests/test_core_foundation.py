"""B1 · the pure-data core: enums, models, Config, health.

Grouped because these four modules carry no behaviour beyond construction and
validation. The stateful singletons (EventBus, GraphMemory, BitNetRuntime) get
their own files.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from neuropaca.core.config import Config
from neuropaca.core.enums import (
    EventType,
    InterfaceChannel,
    MessageRole,
    NodeType,
    RelationType,
    SignalType,
)
from neuropaca.core.errors import ConfigError
from neuropaca.core.health import ModuleHealth, SystemHealth, current_rss_mb
from neuropaca.core.models import Edge, Event, Node, system_error_event

# --------------------------------------------------------------------------- enums


def test_enum_members_match_the_blueprint() -> None:
    # +APP_SWITCH (B2.5b) +SYSTEM_HEALTH_{REQUEST,REPORT} (B5)
    # +ACTION_CONFIRMATION_{REQUEST,RESPONSE} (B7, D-14)
    # +ACTION_PROPOSAL{,_RESULT} (B8, D-16 — the L7/L8 decoupling)
    assert len(EventType) == 20
    # NodeType is unchanged at B8: an ephemeral agent node is a CONCEPT marked by
    # its id prefix, so structural plasticity costs no enum member and no schema
    # bump (D-16).
    assert len(NodeType) == 11  # +IDLE_THOUGHT (B6, D-13)
    assert len(RelationType) == 8
    assert len(SignalType) == 7
    assert len(InterfaceChannel) == 3
    assert len(MessageRole) == 3  # B5


def test_strenum_member_is_its_wire_string() -> None:
    assert NodeType.FILE == "file"
    assert NodeType("file") is NodeType.FILE
    assert RelationType.FOLLOWED_BY == "followed_by"


# -------------------------------------------------------------------------- models


def test_event_defaults_are_unique_and_tz_aware() -> None:
    a = Event(event_type=EventType.IDLE_DETECTED)
    b = Event(event_type=EventType.IDLE_DETECTED)
    assert a.id != b.id
    assert a.timestamp.tzinfo is not None
    assert a.payload == {}


def test_event_is_frozen() -> None:
    ev = Event(event_type=EventType.USER_MESSAGE)
    with pytest.raises((AttributeError, TypeError)):
        ev.source = "mutated"  # type: ignore[misc]


def test_node_and_edge_defaults() -> None:
    n = Node(id="app:code", node_type=NodeType.APP, label="VS Code")
    assert n.relevance_score == 0.0
    assert n.access_count == 0
    assert n.created_at.tzinfo is UTC

    e = Edge(source_id="app:code", target_id="file:/x", relation=RelationType.MODIFIED)
    assert e.weight == 0.0
    assert e.relation == "modified"


def test_system_error_event_shape() -> None:
    ev = system_error_event(module="graph_memory", exception="boom", severity="fatal")
    assert ev.event_type is EventType.SYSTEM_ERROR
    assert ev.priority == 10
    assert ev.payload == {"module": "graph_memory", "exception": "boom", "severity": "fatal"}


# -------------------------------------------------------------------------- Config


def test_fake_backend_config_needs_no_model_path() -> None:
    cfg = Config(inference_backend="fake")
    assert cfg.inference_backend == "fake"
    assert cfg.poll_intervals["system"] == 60.0


def test_llama_backend_requires_existing_model_path() -> None:
    with pytest.raises(ConfigError, match="model_path"):
        Config(inference_backend="llama", model_path="")
    with pytest.raises(ConfigError, match="does not exist"):
        Config(inference_backend="llama", model_path="/no/such/model.gguf")


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        ({"inference_backend": "ollama"}, "inference_backend"),
        ({"inference_backend": "fake", "log_level": "LOUD"}, "log_level"),
        ({"inference_backend": "fake", "idle_threshold_seconds": 0}, "idle_threshold_seconds"),
        ({"inference_backend": "fake", "pressure_low_threshold": -1.0}, "pressure_low_threshold"),
        (
            # B7: the high tier must sit strictly above the low one, or "a single
            # signal never crosses the high threshold" is unenforceable.
            {"inference_backend": "fake", "pressure_high_threshold": 0.5},
            "pressure_high_threshold",
        ),
        (
            {"inference_backend": "fake", "action_enabled_tiers": ["safe", "nuclear"]},
            "action_enabled_tiers",
        ),
        ({"inference_backend": "fake", "api_call_enabled": True}, "api_allowlist"),
        (
            {"inference_backend": "fake", "pressure_decay_half_life_seconds": 0},
            "pressure_decay_half_life_seconds",
        ),
        ({"inference_backend": "fake", "max_concurrent_agents": -1}, "max_concurrent_agents"),
        ({"inference_backend": "fake", "poll_intervals": {"system": 0.0}}, "poll_intervals"),
        ({"inference_backend": "fake", "max_context_tokens": 0}, "max_context_tokens"),
        (
            {"inference_backend": "fake", "interactive_model_context_tokens": -1},
            "interactive_model_context_tokens",
        ),
    ],
)
def test_config_validation_rejects_bad_values(kwargs: dict[str, object], needle: str) -> None:
    with pytest.raises(ConfigError, match=needle):
        Config(**kwargs)  # type: ignore[arg-type]


def test_config_from_file_round_trip(tmp_path) -> None:
    p = tmp_path / "neuropaca.toml"
    p.write_text(
        'inference_backend = "fake"\n'
        'log_level = "DEBUG"\n'
        "bitnet_max_tokens = 512\n"
        "\n[poll_intervals]\nsystem = 30.0\nfilesystem = 5.0\n",
        encoding="utf-8",
    )
    cfg = Config.from_file(p)
    assert cfg.log_level == "DEBUG"
    assert cfg.bitnet_max_tokens == 512
    assert cfg.poll_intervals == {"system": 30.0, "filesystem": 5.0}


def test_config_from_file_rejects_unknown_key(tmp_path) -> None:
    p = tmp_path / "bad.toml"
    p.write_text('inference_backend = "fake"\ngpu_layers = 20\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config keys"):
        Config.from_file(p)


def test_config_from_file_rejects_malformed_toml(tmp_path) -> None:
    p = tmp_path / "broken.toml"
    p.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(ConfigError, match="malformed TOML"):
        Config.from_file(p)


def test_config_from_file_missing(tmp_path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        Config.from_file(tmp_path / "absent.toml")


# -------------------------------------------------------------------------- health


def test_current_rss_mb_is_positive_on_this_platform() -> None:
    rss = current_rss_mb()
    assert rss is not None
    assert rss > 0


def test_system_health_summary_is_one_line() -> None:
    sh = SystemHealth(
        ok=True,
        uptime_seconds=42.0,
        modules=(ModuleHealth(name="l2", ok=True),),
        graph_nodes=11,
        graph_edges=3,
        queue_depth=0,
        rss_mb=128.0,
    )
    line = sh.summary
    assert "\n" not in line
    assert line.startswith("[ok]")
    assert "11n/3e" in line


def test_degraded_system_health_reports_state() -> None:
    sh = SystemHealth(ok=False, uptime_seconds=1.0, events_dropped=4)
    assert sh.summary.startswith("[DEGRADED]")
    assert "drop4" in sh.summary


def test_module_health_last_event_default_none() -> None:
    mh = ModuleHealth(name="l3", ok=True)
    assert mh.last_event_at is None
    mh2 = ModuleHealth(name="l3", ok=True, last_event_at=datetime.now(UTC))
    assert mh2.last_event_at is not None
