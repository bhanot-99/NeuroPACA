# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.2 Step 5 · Safe-tier actions
(OpenAppAction, AdjustVolumeAction, AdjustBrightnessAction).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from neuropaca.action.actions import (
    AdjustBrightnessAction,
    AdjustVolumeAction,
    OpenAppAction,
)
from neuropaca.action.base import ActionTier
from neuropaca.action.executor import ActionExecutor
from neuropaca.action.sandbox import Sandbox
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType
from neuropaca.core.errors import SafetyGateError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event


def _make_mock_tool(tmp_path: Path, name: str) -> Path:
    script = tmp_path / name
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    return script


async def test_open_app_action_success(tmp_path: Path) -> None:
    sandbox = Sandbox([tmp_path])
    mock_app = _make_mock_tool(tmp_path, "mock-app")

    action = OpenAppAction(
        sandbox,
        reason="user requested",
        app_name="Mock App",
        launch_command=str(mock_app),
    )
    assert action.tier == ActionTier.SAFE
    assert action.name == "open_app"

    await action.validate()
    dry = await action.dry_run()
    assert "open app Mock App" in dry

    result = await action.execute()
    assert result == "opened Mock App"
    assert await action.rollback() is False


async def test_open_app_action_validation_failures(tmp_path: Path) -> None:
    sandbox = Sandbox([tmp_path])

    with pytest.raises(SafetyGateError, match="empty app_name"):
        act = OpenAppAction(sandbox, reason="r", app_name="", launch_command="echo")
        await act.validate()

    with pytest.raises(SafetyGateError, match="empty launch_command"):
        act = OpenAppAction(sandbox, reason="r", app_name="App", launch_command="")
        await act.validate()

    with pytest.raises(SafetyGateError, match="executable not found"):
        act = OpenAppAction(
            sandbox, reason="r", app_name="App", launch_command="nonexistent_xyz_bin"
        )
        await act.validate()


async def test_adjust_volume_action(tmp_path: Path) -> None:
    sandbox = Sandbox([tmp_path])
    mock_tool = _make_mock_tool(tmp_path, "mock-wpctl")

    action = AdjustVolumeAction(
        sandbox,
        reason="voice command",
        direction="increase",
        tool_path=str(mock_tool),
    )
    assert action.tier == ActionTier.SAFE
    assert action.name == "adjust_volume"

    await action.validate()
    dry = await action.dry_run()
    assert "adjust volume increase" in dry

    res = await action.execute()
    assert res == "adjusted volume increase by 5%"

    # Invalid direction
    with pytest.raises(SafetyGateError, match="invalid volume adjustment direction"):
        bad_action = AdjustVolumeAction(sandbox, reason="r", direction="sideways")
        await bad_action.validate()


async def test_adjust_brightness_action(tmp_path: Path) -> None:
    sandbox = Sandbox([tmp_path])
    mock_tool = _make_mock_tool(tmp_path, "mock-brightnessctl")

    action = AdjustBrightnessAction(
        sandbox,
        reason="voice command",
        direction="decrease",
        step="10%",
        tool_path=str(mock_tool),
    )
    assert action.tier == ActionTier.SAFE
    assert action.name == "adjust_brightness"

    await action.validate()
    dry = await action.dry_run()
    assert "adjust brightness decrease by 10%" in dry

    res = await action.execute()
    assert res == "adjusted brightness decrease by 10%"

    # Invalid direction
    with pytest.raises(SafetyGateError, match="invalid brightness adjustment direction"):
        bad_action = AdjustBrightnessAction(sandbox, reason="r", direction="sideways")
        await bad_action.validate()


async def test_executor_proposes_and_runs_safe_voice_actions(tmp_path: Path) -> None:
    bus = EventBus()
    await bus.start()
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()

    mock_app = _make_mock_tool(tmp_path, "my-app")
    mock_wp = _make_mock_tool(tmp_path, "my-wpctl")
    mock_bright = _make_mock_tool(tmp_path, "my-bright")

    cfg = Config(
        inference_backend="fake",
        graph_db_path=str(tmp_path / "graph.json"),
        action_log_path=str(tmp_path / "actions.jsonl"),
        quarantine_path=str(tmp_path / "quarantine"),
        watch_paths=[str(tmp_path)],
        action_dry_run=False,
        action_enabled_tiers=["safe"],
    )

    executor = ActionExecutor(bus, cfg, gm)
    await executor.initialize()
    await executor.start()

    proposal_results: list[Event] = []
    bus.subscribe(EventType.ACTION_PROPOSAL_RESULT, proposal_results.append)

    try:
        # 1. Open app proposal
        bus.publish(
            Event(
                event_type=EventType.ACTION_PROPOSAL,
                source="test",
                payload={
                    "proposal_id": "prop-open-1",
                    "action_type": "open_app",
                    "reason": "voice command: open app",
                    "trigger": "voice:test",
                    "kwargs": {
                        "app_name": "My App",
                        "launch_command": str(mock_app),
                    },
                },
            )
        )
        await bus.join()
        for t in list(executor._tasks):
            await t
        await bus.join()

        assert len(proposal_results) == 1
        assert proposal_results[0].payload["proposal_id"] == "prop-open-1"
        assert proposal_results[0].payload["accepted"] is True
        assert proposal_results[0].payload["ok"] is True

        # 2. Volume adjustment proposal
        bus.publish(
            Event(
                event_type=EventType.ACTION_PROPOSAL,
                source="test",
                payload={
                    "proposal_id": "prop-vol-1",
                    "action_type": "adjust_volume",
                    "reason": "voice command: volume up",
                    "trigger": "voice:test",
                    "kwargs": {
                        "direction": "increase",
                        "tool_path": str(mock_wp),
                    },
                },
            )
        )
        await bus.join()
        for t in list(executor._tasks):
            await t
        await bus.join()

        assert len(proposal_results) == 2
        assert proposal_results[1].payload["proposal_id"] == "prop-vol-1"
        assert proposal_results[1].payload["accepted"] is True
        assert proposal_results[1].payload["ok"] is True

        # 3. Brightness adjustment proposal
        bus.publish(
            Event(
                event_type=EventType.ACTION_PROPOSAL,
                source="test",
                payload={
                    "proposal_id": "prop-bright-1",
                    "action_type": "adjust_brightness",
                    "reason": "voice command: brightness down",
                    "trigger": "voice:test",
                    "kwargs": {
                        "direction": "decrease",
                        "tool_path": str(mock_bright),
                    },
                },
            )
        )
        await bus.join()
        for t in list(executor._tasks):
            await t
        await bus.join()

        assert len(proposal_results) == 3
        assert proposal_results[2].payload["proposal_id"] == "prop-bright-1"
        assert proposal_results[2].payload["accepted"] is True
        assert proposal_results[2].payload["ok"] is True

    finally:
        bus.unsubscribe(EventType.ACTION_PROPOSAL_RESULT, proposal_results.append)
        await executor.stop()
        await bus.stop()
        GraphMemory._reset_for_tests()
