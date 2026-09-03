"""L7 · Action — the safety gate, the sandbox, and the confirmation handshake
(B7, D-14, Architecture.md §11b, rules.md §5).

Three of the B7 exit criteria are proved here:

- *dangerous actions cannot execute without a recorded confirmation* — denial,
  silence (timeout), and approval are all tested against the real handshake;
- *the audit log is complete for every attempt* — including every refusal, and
  including the case where the log itself cannot be written;
- the sandbox holds: no shell, no inherited environment, no path escape, and a
  runaway command dies on its budget.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neuropaca.action.actions import (
    FileWriteAction,
    MemoryWriteAction,
    NotificationAction,
    RunCommandAction,
)
from neuropaca.action.audit import ActionAudit
from neuropaca.action.base import ActionTier
from neuropaca.action.confirm import ConfirmationBroker
from neuropaca.action.executor import ActionExecutor
from neuropaca.action.gate import SafetyGate
from neuropaca.action.quarantine import Quarantine
from neuropaca.action.sandbox import Sandbox
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, NodeType, RelationType
from neuropaca.core.errors import SafetyGateError
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.models import Event
from neuropaca.drive.pressure import PressureEntry


def _config(tmp_path: Path, **over) -> Config:
    base: dict[str, object] = {
        "inference_backend": "fake",
        "graph_db_path": str(tmp_path / "graph.json"),
        "action_log_path": str(tmp_path / "actions.jsonl"),
        "quarantine_path": str(tmp_path / "quarantine"),
        "watch_paths": [str(tmp_path / "work")],
        "action_dry_run": False,
        "action_enabled_tiers": ["safe", "dangerous"],
        "action_confirmation_timeout_seconds": 1,
    }
    base.update(over)
    return Config(**base)  # type: ignore[arg-type]


def _audit_lines(config: Config) -> list[dict]:
    path = Path(config.action_log_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


async def _executor(tmp_path: Path, **over) -> tuple[ActionExecutor, EventBus, Config]:
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await graph.load()
    config = _config(tmp_path, **over)
    (tmp_path / "work").mkdir(exist_ok=True)
    executor = ActionExecutor(bus, config, graph)
    await executor.initialize()
    await executor.start()
    return executor, bus, config


async def _teardown(executor: ActionExecutor, bus: EventBus) -> None:
    await executor.stop()
    await bus.stop()


async def _settle(executor: ActionExecutor, bus: EventBus) -> None:
    """Wait for the gated action to actually finish, rather than guessing at a
    sleep budget.

    `_teardown` calls `executor.stop()`, which **cancels** pending tasks. A gated
    action that has not finished by then is killed before it can publish
    `ACTION_TRIGGERED` or write its audit pair — so a fixed sleep is a race
    against the machine, not a wait. It held on a fast box and lost on the CI
    runner, where the audit log's `fsync` alone can exceed 50 ms.

    Awaiting the executor's own task set is deterministic and needs no clock, per
    rules.md §8 ("no test sleeps"). The second `join()` drains the events those
    tasks published so a spy subscriber has seen them.
    """
    await bus.join()
    pending = list(executor._tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await bus.join()


async def _approve_next(bus: EventBus, *, approved: bool) -> list[str]:
    """Stand in for the human at the terminal: answer whatever L7 asks."""
    answered: list[str] = []

    async def responder(event: Event) -> None:
        request_id = str(event.payload["request_id"])
        answered.append(request_id)
        bus.publish(
            Event(
                event_type=EventType.ACTION_CONFIRMATION_RESPONSE,
                source="test-terminal",
                payload={"request_id": request_id, "approved": approved},
            )
        )

    bus.subscribe(EventType.ACTION_CONFIRMATION_REQUEST, responder)
    return answered


# ------------------------------------------------------------------- sandbox


def test_the_sandbox_refuses_a_path_outside_its_roots(tmp_path) -> None:
    sandbox = Sandbox([tmp_path / "work"])
    (tmp_path / "work").mkdir()
    assert sandbox.resolve_write_path(tmp_path / "work" / "note.md").name == "note.md"
    with pytest.raises(SafetyGateError, match="escapes the sandbox"):
        sandbox.resolve_write_path(tmp_path / "elsewhere" / "note.md")
    with pytest.raises(SafetyGateError, match="escapes the sandbox"):
        sandbox.resolve_write_path(tmp_path / "work" / ".." / "escape.md")
    with pytest.raises(SafetyGateError, match="must be absolute"):
        sandbox.resolve_write_path("relative.md")


def test_a_symlink_out_of_the_sandbox_is_resolved_then_refused(tmp_path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "secrets").mkdir()
    (work / "bridge").symlink_to(tmp_path / "secrets")
    sandbox = Sandbox([work])
    with pytest.raises(SafetyGateError, match="escapes the sandbox"):
        sandbox.resolve_write_path(work / "bridge" / "key.txt")


def test_with_no_roots_nothing_is_writable(tmp_path) -> None:
    """Fail closed: the shipped default has `watch_paths` empty."""
    with pytest.raises(SafetyGateError, match="no writable roots"):
        Sandbox([]).resolve_write_path(tmp_path / "x")


async def test_a_sandboxed_command_inherits_no_environment(tmp_path) -> None:
    """`env={}` is the guarantee: the child cannot read the daemon's variables."""
    import os

    os.environ["NEUROPACA_TEST_SECRET"] = "do-not-leak"
    try:
        sandbox = Sandbox([tmp_path])
        outcome = await sandbox.run(
            [sys.executable, "-c", "import os;print(sorted(os.environ))"], timeout_seconds=10
        )
    finally:
        del os.environ["NEUROPACA_TEST_SECRET"]

    assert outcome.ok
    # Whatever the interpreter sets up for itself (locale), nothing of the
    # daemon's environment crosses the boundary: no secret, no PATH, no HOME.
    for leaked in ("NEUROPACA_TEST_SECRET", "PATH", "HOME", "XDG_RUNTIME_DIR"):
        assert leaked not in outcome.stdout


async def test_a_shell_metacharacter_is_an_argument_not_a_command(tmp_path) -> None:
    """There is no shell, so `;` is just text — nothing to inject into."""
    sandbox = Sandbox([tmp_path])
    outcome = await sandbox.run(
        [sys.executable, "-c", "import sys;print(sys.argv[1])", "; rm -rf /"], timeout_seconds=10
    )
    assert outcome.ok
    assert outcome.stdout.strip() == "; rm -rf /"


async def test_a_runaway_command_is_killed_on_its_budget(tmp_path) -> None:
    sandbox = Sandbox([tmp_path])
    with pytest.raises(SafetyGateError, match="exceeded its"):
        await sandbox.run([sys.executable, "-c", "import time;time.sleep(30)"], timeout_seconds=0.5)


def test_argv_validation_rejects_a_missing_executable() -> None:
    with pytest.raises(SafetyGateError, match="executable not found"):
        Sandbox([]).validate_argv(["definitely-not-a-real-binary-xyz"])
    with pytest.raises(SafetyGateError, match="empty command"):
        Sandbox([]).validate_argv([])


# ---------------------------------------------------------------- quarantine


async def test_quarantine_preserves_and_restores_prior_bytes(tmp_path) -> None:
    target = tmp_path / "note.md"
    target.write_text("original")
    q = Quarantine(tmp_path / "q", ttl_hours=1)

    token = await q.stash(target)
    assert token is not None
    target.write_text("clobbered")
    assert await q.restore(token) is True
    assert target.read_text() == "original"


async def test_quarantine_sweeps_only_expired_entries(tmp_path) -> None:
    target = tmp_path / "note.md"
    target.write_text("x")
    q = Quarantine(tmp_path / "q", ttl_hours=1)
    token = await q.stash(target)
    assert token is not None

    assert await q.purge_expired(datetime.now(UTC)) == 0
    assert await q.purge_expired(datetime.now(UTC) + timedelta(hours=2)) == 1
    assert await q.restore(token) is False


async def test_stashing_a_file_that_does_not_exist_is_not_a_failure(tmp_path) -> None:
    q = Quarantine(tmp_path / "q", ttl_hours=1)
    assert await q.stash(tmp_path / "absent.md") is None


# ---------------------------------------------------------------- the gate


async def test_the_audit_log_records_two_lines_for_every_attempt(tmp_path) -> None:
    executor, bus, config = await _executor(tmp_path)
    try:
        result = await executor.run_now(
            NotificationAction(reason="test", text="hello"), trigger="test"
        )
    finally:
        await _teardown(executor, bus)

    lines = _audit_lines(config)
    assert result.ok is True
    assert [line["phase"] for line in lines] == ["attempt", "result"]
    assert lines[0]["request_id"] == lines[1]["request_id"] == result.request_id
    assert lines[0]["trigger"] == "test"
    assert lines[1]["ok"] is True


async def test_a_refusal_is_an_attempt_and_is_logged(tmp_path) -> None:
    """A disabled tier still writes the pair — "what did it try to do to me" has
    to be answerable for refusals too."""
    executor, bus, config = await _executor(tmp_path, action_enabled_tiers=["safe"])
    try:
        result = await executor.run_now(
            RunCommandAction(executor.sandbox, reason="test", argv=("/bin/true",)),
            trigger="test",
        )
    finally:
        await _teardown(executor, bus)

    lines = _audit_lines(config)
    assert result.ok is False
    assert "not enabled" in result.detail
    assert [line["phase"] for line in lines] == ["attempt", "result"]


async def test_an_unwritable_audit_log_refuses_the_action(tmp_path) -> None:
    """rules.md §5.6 as a precondition: no record, no effect."""
    executor, bus, _config = await _executor(
        tmp_path, action_log_path=str(tmp_path / "wall" / "actions.jsonl")
    )
    (tmp_path / "wall").write_text("I am a file, not a directory")
    target = tmp_path / "work" / "note.md"
    try:
        result = await executor.run_now(
            FileWriteAction(
                executor.sandbox,
                executor.quarantine,
                reason="test",
                path=target,
                content="nope",
            ),
            trigger="test",
        )
    finally:
        await _teardown(executor, bus)

    assert result.ok is False
    assert "audit log could not be written" in result.detail
    assert not target.exists()


async def test_dry_run_is_the_shipped_default_and_causes_nothing(tmp_path) -> None:
    executor, bus, config = await _executor(tmp_path, action_dry_run=True)
    target = tmp_path / "work" / "note.md"
    try:
        result = await executor.run_now(
            FileWriteAction(
                executor.sandbox, executor.quarantine, reason="test", path=target, content="x"
            ),
            trigger="test",
        )
    finally:
        await _teardown(executor, bus)

    assert result.ok is True
    assert result.dry_run is True
    assert result.detail.startswith("dry-run: create")
    assert result.confirmed is None
    assert not target.exists()
    assert _audit_lines(config)[0]["dry_run"] is True


def test_the_default_config_ships_in_dry_run_with_only_the_safe_tier() -> None:
    fresh = Config(inference_backend="fake")
    assert fresh.action_dry_run is True
    assert fresh.action_enabled_tiers == ["safe"]
    assert fresh.api_call_enabled is False
    assert fresh.watch_paths == []  # => no path is writable at all


# -------------------------------------------------------- confirmation gate


async def test_a_dangerous_action_without_an_answer_is_refused(tmp_path) -> None:
    """Nobody at the terminal: the handshake expires and expiry means no."""
    executor, bus, config = await _executor(tmp_path)
    try:
        result = await executor.run_now(
            RunCommandAction(
                executor.sandbox,
                reason="test",
                argv=(sys.executable, "-c", "open(r'%s','w')" % (tmp_path / "work" / "ran.txt")),
            ),
            trigger="test",
        )
    finally:
        await _teardown(executor, bus)

    assert result.ok is False
    assert result.confirmed is False
    assert "not confirmed" in result.detail
    assert not (tmp_path / "work" / "ran.txt").exists()
    assert _audit_lines(config)[-1]["confirmed"] is False


async def test_a_denied_confirmation_stops_the_action(tmp_path) -> None:
    executor, bus, _config = await _executor(tmp_path)
    await _approve_next(bus, approved=False)
    marker = tmp_path / "work" / "ran.txt"
    try:
        result = await executor.run_now(
            RunCommandAction(
                executor.sandbox,
                reason="test",
                argv=(sys.executable, "-c", f"open(r'{marker}','w')"),
            ),
            trigger="test",
        )
    finally:
        await _teardown(executor, bus)

    assert result.ok is False
    assert result.confirmed is False
    assert not marker.exists()


async def test_an_approved_confirmation_lets_the_command_run(tmp_path) -> None:
    executor, bus, config = await _executor(tmp_path)
    answered = await _approve_next(bus, approved=True)
    marker = tmp_path / "work" / "ran.txt"
    try:
        result = await executor.run_now(
            RunCommandAction(
                executor.sandbox,
                reason="test",
                argv=(sys.executable, "-c", f"open(r'{marker}','w')"),
            ),
            trigger="test",
        )
    finally:
        await _teardown(executor, bus)

    assert result.ok is True
    assert result.confirmed is True
    assert marker.exists()
    assert len(answered) == 1
    assert _audit_lines(config)[-1]["confirmed"] is True


async def test_a_stale_or_unknown_confirmation_response_is_ignored(tmp_path) -> None:
    bus = EventBus.get_instance()
    await bus.start()
    broker = ConfirmationBroker(bus, timeout_seconds=1)
    try:
        await broker.on_response(
            Event(
                event_type=EventType.ACTION_CONFIRMATION_RESPONSE,
                payload={"request_id": "nonexistent", "approved": True},
            )
        )
    finally:
        await bus.stop()
    assert broker.counters == (0, 0, 0)


# -------------------------------------------------------- concrete actions


async def test_a_file_write_backs_up_first_and_rolls_back_on_failure(tmp_path) -> None:
    executor, bus, _config = await _executor(tmp_path)
    await _approve_next(bus, approved=True)
    target = tmp_path / "work" / "note.md"
    target.write_text("original")
    try:
        result = await executor.run_now(
            FileWriteAction(
                executor.sandbox,
                executor.quarantine,
                reason="test",
                path=target,
                content="replacement",
            ),
            trigger="test",
        )
        assert result.ok is True
        assert target.read_text() == "replacement"

        # The prior bytes were preserved, not destroyed (rules.md §5.7).
        stashed = list((tmp_path / "quarantine").glob("*.bin"))
        assert len(stashed) == 1
        assert stashed[0].read_text() == "original"
    finally:
        await _teardown(executor, bus)


async def test_a_file_write_outside_the_sandbox_never_reaches_confirmation(tmp_path) -> None:
    """Validation runs before the human is asked: you are not prompted to approve
    something that was never permissible."""
    executor, bus, _config = await _executor(tmp_path)
    prompts = await _approve_next(bus, approved=True)
    outside = tmp_path / "elsewhere.md"
    try:
        result = await executor.run_now(
            FileWriteAction(
                executor.sandbox,
                executor.quarantine,
                reason="test",
                path=outside,
                content="x",
            ),
            trigger="test",
        )
    finally:
        await _teardown(executor, bus)

    assert result.ok is False
    assert "escapes the sandbox" in result.detail
    assert prompts == []
    assert not outside.exists()


async def test_a_memory_write_is_safe_silent_and_reversible(tmp_path) -> None:
    executor, bus, _config = await _executor(tmp_path)
    graph = GraphMemory.get_instance()
    await graph.add_node("app:webpack", NodeType.APP, {"label": "webpack"})
    action = MemoryWriteAction(
        graph,
        reason="pressure",
        node_id="action:test",
        node_type=NodeType.EVENT_LOG,
        attributes={"label": "pressure on webpack"},
        edges=(("app:webpack", RelationType.RELATED_TO),),
    )
    try:
        assert action.tier is ActionTier.SAFE
        result = await executor.run_now(action, trigger="test")
        assert result.ok is True
        assert result.confirmed is None  # safe tier is never prompted
        assert graph.get_node("action:test") is not None

        assert await action.rollback() is True
        assert graph.get_node("action:test") is None
    finally:
        await _teardown(executor, bus)


async def test_a_notification_is_an_intent_l7_never_touches_the_desktop(tmp_path) -> None:
    executor, bus, _config = await _executor(tmp_path)
    seen: list[dict] = []

    async def spy(event: Event) -> None:
        seen.append(event.payload)

    bus.subscribe(EventType.ACTION_TRIGGERED, spy)
    try:
        await executor.run_now(
            NotificationAction(reason="corroborated", text="webpack is hot", node_ids=("app:x",)),
            trigger="test",
        )
        await bus.join()
    finally:
        await _teardown(executor, bus)

    assert len(seen) == 1
    assert seen[0]["intent"] == {
        "kind": "notification",
        "reason": "corroborated",
        "text": "webpack is hot",
        "node_ids": ["app:x"],
    }


def test_b7_ships_no_outbound_socket() -> None:
    """rules.md §5.5 / §6: `ApiCallAction` is not built in B7, so there is no
    code path from an action to a network socket at all."""
    import neuropaca.action as action_pkg

    assert not any("api" in name.lower() for name in action_pkg.__all__)


# ------------------------------------------------------------- the executor


async def test_the_low_tier_writes_a_silent_memory_record(tmp_path) -> None:
    executor, bus, _config = await _executor(tmp_path)
    graph = GraphMemory.get_instance()
    await graph.add_node("app:webpack", NodeType.APP, {"label": "webpack"})
    now = datetime.now(UTC)
    try:
        bus.publish(
            Event(
                event_type=EventType.PRESSURE_THRESHOLD_REACHED,
                source="drive",
                payload={
                    "tier": "low",
                    "entry": PressureEntry(
                        node_id="app:webpack",
                        pressure=1.2,
                        reason="cpu pinned",
                        created_at=now,
                        last_updated=now,
                        sources=("diagnosis",),
                    ),
                },
            )
        )
        await _settle(executor, bus)
        written = [n for n in graph.node_ids if n.startswith("action:")]
        assert len(written) == 1
        node = graph.get_node(written[0])
        assert node is not None and "app:webpack" in node.label
    finally:
        await _teardown(executor, bus)


async def test_the_high_tier_asks_instead_of_acting(tmp_path) -> None:
    """B7 registers no autonomous dangerous action: crossing the high threshold
    prompts the human (Architecture.md §7) rather than killing anything."""
    executor, bus, _config = await _executor(tmp_path)
    intents: list[dict] = []

    async def spy(event: Event) -> None:
        intents.append(event.payload["intent"])

    bus.subscribe(EventType.ACTION_TRIGGERED, spy)
    now = datetime.now(UTC)
    try:
        bus.publish(
            Event(
                event_type=EventType.PRESSURE_THRESHOLD_REACHED,
                source="drive",
                payload={
                    "tier": "high",
                    "entry": PressureEntry(
                        node_id="app:webpack",
                        pressure=3.4,
                        reason="cpu pinned and corroborated",
                        created_at=now,
                        last_updated=now,
                        sources=("diagnosis", "learning"),
                    ),
                },
            )
        )
        await _settle(executor, bus)
    finally:
        await _teardown(executor, bus)

    assert [i["kind"] for i in intents] == ["notification"]
    assert "diagnosis+learning" in intents[0]["text"]


async def test_bang_forces_the_dangerous_tier_but_not_past_confirmation(tmp_path) -> None:
    """D-14: `$!` skips L3/L4 and forces the tier — it does not skip the gate."""
    executor, bus, config = await _executor(tmp_path)
    marker = tmp_path / "work" / "bang.txt"
    try:
        bus.publish(
            Event(
                event_type=EventType.USER_MESSAGE,
                source="interface",
                payload={
                    "prefix": "$!",
                    "text": f"{sys.executable} -c \"open(r'{marker}','w')\"",
                },
            )
        )
        await bus.join()
        await asyncio.sleep(1.4)  # past the 1 s confirmation timeout
    finally:
        await _teardown(executor, bus)

    lines = _audit_lines(config)
    assert not marker.exists()  # nobody confirmed, so nothing ran
    assert lines[-1]["ok"] is False
    assert "not confirmed" in lines[-1]["detail"]
    assert lines[-1]["trigger"] == "user:$!"
    assert lines[-1]["tier"] == "dangerous"


async def test_double_dollar_backs_up_the_daemons_state_first(tmp_path) -> None:
    executor, bus, config = await _executor(tmp_path)
    Path(config.graph_db_path).write_text("{}")
    await _approve_next(bus, approved=True)
    marker = tmp_path / "work" / "dd.txt"
    try:
        bus.publish(
            Event(
                event_type=EventType.USER_MESSAGE,
                source="interface",
                payload={
                    "prefix": "$$",
                    "text": f"{sys.executable} -c \"open(r'{marker}','w')\"",
                },
            )
        )
        await _settle(executor, bus)
    finally:
        await _teardown(executor, bus)

    assert marker.exists()
    stashed = list((tmp_path / "quarantine").glob("*.bin"))
    assert len(stashed) == 1  # the graph was preserved before the command ran


async def test_a_question_prefix_never_reaches_the_action_layer(tmp_path) -> None:
    executor, bus, config = await _executor(tmp_path)
    try:
        for prefix in ("$", "$?"):
            bus.publish(
                Event(
                    event_type=EventType.USER_MESSAGE,
                    source="interface",
                    payload={"prefix": prefix, "text": "/bin/true"},
                )
            )
        await _settle(executor, bus)
    finally:
        await _teardown(executor, bus)

    assert _audit_lines(config) == []


async def test_an_unparseable_command_is_refused_and_still_logged(tmp_path) -> None:
    executor, bus, config = await _executor(tmp_path)
    try:
        bus.publish(
            Event(
                event_type=EventType.USER_MESSAGE,
                source="interface",
                payload={"prefix": "$!", "text": 'unbalanced "quote'},
            )
        )
        await _settle(executor, bus)
    finally:
        await _teardown(executor, bus)

    lines = _audit_lines(config)
    assert [line["phase"] for line in lines] == ["attempt", "result"]
    assert lines[-1]["ok"] is False


def test_the_audit_file_is_owner_only(tmp_path) -> None:
    audit = ActionAudit(tmp_path / "actions.jsonl")
    asyncio.run(audit.record("attempt", action="x"))
    assert oct((tmp_path / "actions.jsonl").stat().st_mode)[-3:] == "600"


async def test_health_reports_the_mode_and_the_counters(tmp_path) -> None:
    executor, bus, _config = await _executor(tmp_path, action_dry_run=True)
    try:
        await executor.run_now(NotificationAction(reason="r", text="hi"), trigger="test")
        report = executor.health()
    finally:
        await _teardown(executor, bus)

    assert report.ok is True
    assert "dry-run" in report.detail
    assert "1 dry-run" in report.detail


def test_the_gate_holds_no_action_specific_state(tmp_path) -> None:
    """One gate serves every module and can run actions concurrently."""
    bus = EventBus.get_instance()
    config = _config(tmp_path)
    gate = SafetyGate(
        config,
        bus,
        ActionAudit(config.action_log_path),
        Quarantine(config.quarantine_path, 1),
        ConfirmationBroker(bus, 1),
    )
    assert gate.counters == {"executed": 0, "refused": 0, "dry_runs": 0, "rollbacks": 0}
