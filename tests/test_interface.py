"""B5/B12 · Interface (L9) — retrieval, the interactive-model seam, IPC, surfacing.

Since B12 the terminal is a read-only project guide: no `$` grammar, no `chat`,
no natural-language graph query. What remains on the socket is `health`,
`insights`, `notifications`, `confirmations`, `confirm`, `run`, `explain`.
`tell` / `overview` are client-side and deterministic (`tests/test_describe.py`).

No test loads a real model (rules.md §8) and no test sleeps (`FakeClock`).
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import FakeClock
from neuropaca.core.config import Config
from neuropaca.core.enums import EventType, MessageRole, NodeType, SignalType
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.inference import FakeInferenceBackend, create_interactive_backend
from neuropaca.core.models import Event
from neuropaca.interface import cli
from neuropaca.interface.layer import InterfaceLayer
from neuropaca.interface.message import Message
from neuropaca.learning.insight import Insight

# --------------------------------------------------------------------------- 1
# Core retrieval primitive (still used by GraphMemory; L9 no longer calls it)


async def _graph(tmp_path) -> GraphMemory:
    gm = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await gm.load()
    return gm


async def test_search_by_label_substring_is_case_insensitive(tmp_path) -> None:
    gm = await _graph(tmp_path)
    await gm.add_node("app:code", NodeType.APP, {"label": "Visual Studio Code"})
    await gm.add_node("file:/x", NodeType.FILE, {"label": "/home/u/notes.md"})

    hits = gm.search_by_label("studio")
    assert [n.id for n in hits] == ["app:code"]


async def test_search_by_label_empty_query_returns_nothing(tmp_path) -> None:
    gm = await _graph(tmp_path)
    assert gm.search_by_label("   ") == []


def test_message_role_is_an_enum_not_a_string() -> None:
    m = Message(role=MessageRole.USER, content="hi")
    assert m.role is MessageRole.USER
    assert m.role == "user"  # StrEnum wire form
    assert m.related_node_ids == ()


# --------------------------------------------------------------------------- 2
# The interactive-model seam — now used only by `tell --explain` (B12)


async def test_bitnet_runtime_routes_interactive_to_the_second_backend() -> None:
    loop_be, explain_be = FakeInferenceBackend(), FakeInferenceBackend()
    rt = BitNetRuntime.get_instance(loop_be, explain_be)
    assert rt.interactive_configured

    await rt.infer_async("loop prompt", 16)
    await rt.infer_async("explain prompt", 16, interactive=True)

    assert [c[0] for c in loop_be.calls] == ["loop prompt"]
    assert [c[0] for c in explain_be.calls] == ["explain prompt"]


async def test_bitnet_runtime_falls_back_to_loop_model_when_no_interactive_backend() -> None:
    loop_be = FakeInferenceBackend()
    rt = BitNetRuntime.get_instance(loop_be)
    assert not rt.interactive_configured
    await rt.infer_async("q", 16, interactive=True)
    assert loop_be.calls[0][0] == "q"


def test_create_interactive_backend_is_none_without_a_path() -> None:
    llama_cfg = Config(inference_backend="llama", model_path=__file__)
    assert create_interactive_backend(llama_cfg) is None
    assert create_interactive_backend(Config(inference_backend="fake")) is not None


def test_insight_extractive_grammar_still_works() -> None:
    from neuropaca.learning.prompts import build_insight_grammar, parse_insight

    g = build_insight_grammar(["n1"])
    raw = FakeInferenceBackend().infer("p", 48, 0.0, g)
    ins = parse_insight(
        raw, {"n1": "app:x"}, source_signal=SignalType.HIGH_LOAD, confidence=0.9, snapshot_count=1
    )
    assert ins is not None and ins.cited_node_ids == ("app:x",)


def test_fake_backend_paraphrases_an_explain_prompt() -> None:
    from neuropaca.learning.prompts import build_explain_prompt, clean_explain_answer

    prompt = build_explain_prompt(
        "src/neuropaca/interface/cli.py",
        "File: src/neuropaca/interface/cli.py\n\nThe thin CLI client.",
    )
    out = FakeInferenceBackend().infer(prompt, 260, 0.3, None)
    cleaned = clean_explain_answer(out)
    assert cleaned and "cli.py" in cleaned


# --------------------------------------------------------------------------- 3
# IPC, health routing, redaction


class _Wired:
    def __init__(self, layer: InterfaceLayer, bus: EventBus, graph: GraphMemory, sock: str) -> None:
        self.layer, self.bus, self.graph, self.sock = layer, bus, graph, sock

    async def request(self, payload: dict) -> dict:
        reader, writer = await asyncio.open_unix_connection(self.sock)
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), 5)
        writer.close()
        await writer.wait_closed()
        return json.loads(line)


async def _wired(
    tmp_path, *, clock: FakeClock | None = None, interactive: bool = True, **cfg
) -> _Wired:
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await graph.load()
    await graph.add_node(
        "app:webpack", NodeType.APP, {"label": "webpack (cpu heavy)", "relevance_score": 8.2}
    )
    sock = str(tmp_path / "np.sock")
    runtime = BitNetRuntime.get_instance(
        FakeInferenceBackend(),
        create_interactive_backend(Config(inference_backend="fake")) if interactive else None,
    )
    layer = InterfaceLayer(
        bus,
        Config(inference_backend="fake", **cfg),
        graph,
        runtime,
        clock=clock or FakeClock(),
        socket_path=sock,
    )
    await layer.initialize()
    await layer.start()
    return _Wired(layer, bus, graph, sock)


async def _teardown(w: _Wired) -> None:
    await w.layer.stop()
    await w.bus.stop()


async def test_run_is_relayed_to_l7_not_executed(tmp_path) -> None:
    """B7 (D-14), surfaced as `neuropaca run` since B12: L9 hands the command to
    the action layer and returns at once.

    It publishes exactly one `USER_MESSAGE` per command carrying the internal
    `prefix` enum (`$!` = run, `$$` = run + backup), and answers `queued` — never
    an answer, never an effect of its own."""
    w = await _wired(tmp_path)
    seen: list[tuple[str, str]] = []

    async def spy(event) -> None:
        seen.append((event.payload["prefix"], event.payload["text"]))

    w.bus.subscribe(EventType.USER_MESSAGE, spy)
    try:
        run = await w.request({"op": "run", "cmd": "kill it"})
        backup = await w.request({"op": "run", "cmd": "clean up", "backup": True})
        empty = await w.request({"op": "run", "cmd": ""})
        await w.bus.join()
    finally:
        await _teardown(w)

    assert run["ok"] is True and run["queued"] is True and run["prefix"] == "$!"
    assert backup["ok"] is True and backup["queued"] is True and backup["prefix"] == "$$"
    assert "answer" not in run and "answer" not in backup
    assert empty["ok"] is False and "empty command" in empty["error"]
    assert seen == [("$!", "kill it"), ("$$", "clean up")]


async def test_explain_op_paraphrases_a_first_party_summary(tmp_path) -> None:
    w = await _wired(tmp_path)
    try:
        resp = await w.request(
            {
                "op": "explain",
                "target": "src/neuropaca/drive/pressure.py",
                "summary": "File: src/neuropaca/drive/pressure.py\n\nThe drive layer.",
            }
        )
    finally:
        await _teardown(w)
    assert resp["ok"] is True
    assert resp["source"] == "model"
    assert resp["answer"]


async def test_explain_without_a_model_returns_empty_not_an_error(tmp_path) -> None:
    w = await _wired(tmp_path, interactive=False)
    try:
        resp = await w.request(
            {"op": "explain", "target": "x.py", "summary": "File: x.py\n\nA file."}
        )
        empty = await w.request({"op": "explain", "target": "x.py", "summary": ""})
    finally:
        await _teardown(w)
    assert resp["ok"] is True and resp["answer"] == "" and resp["source"] == "template-nomodel"
    assert empty["ok"] is False


async def test_explain_op_does_not_publish_or_store(tmp_path) -> None:
    """`explain` is a read-only paraphrase — nothing on the bus, nothing in the
    RAM conversation history."""
    w = await _wired(tmp_path)
    seen: list[Event] = []

    async def spy(event: Event) -> None:
        seen.append(event)

    w.bus.subscribe(EventType.USER_MESSAGE, spy)
    try:
        await w.request({"op": "explain", "target": "x.py", "summary": "File: x.py\n\nA file."})
        await w.bus.join()
        history = w.layer.conversation_history
    finally:
        await _teardown(w)
    assert seen == []
    assert history == ()


async def test_notification_intents_are_drained_by_the_notifications_op(tmp_path) -> None:
    """B7 (D-14): L7 publishes an intent, L9 owns delivery. A non-notification
    action result is *not* narrated to the user — the audit log has that."""
    w = await _wired(tmp_path)
    try:
        w.bus.publish(
            Event(
                event_type=EventType.ACTION_TRIGGERED,
                source="action",
                payload={
                    "ok": True,
                    "dry_run": True,
                    "intent": {
                        "kind": "notification",
                        "reason": "corroborated pressure",
                        "text": "webpack is hot",
                        "node_ids": ["app:webpack"],
                    },
                },
            )
        )
        w.bus.publish(
            Event(
                event_type=EventType.ACTION_TRIGGERED,
                source="action",
                payload={"ok": True, "intent": {"kind": "memory_write", "reason": "r"}},
            )
        )
        await w.bus.join()
        first = await w.request({"op": "notifications"})
        second = await w.request({"op": "notifications"})
    finally:
        await _teardown(w)

    assert [n["text"] for n in first["notifications"]] == ["webpack is hot"]
    assert first["notifications"][0]["dry_run"] is True
    assert second["notifications"] == []  # drained, delivered once


async def test_a_failed_action_is_not_narrated_to_the_user(tmp_path) -> None:
    w = await _wired(tmp_path)
    try:
        w.bus.publish(
            Event(
                event_type=EventType.ACTION_TRIGGERED,
                source="action",
                payload={
                    "ok": False,
                    "intent": {"kind": "notification", "text": "never said", "reason": "r"},
                },
            )
        )
        await w.bus.join()
        resp = await w.request({"op": "notifications"})
    finally:
        await _teardown(w)

    assert resp["notifications"] == []


async def test_l9_relays_a_confirmation_verdict_back_to_l7(tmp_path) -> None:
    """The headless-daemon handshake, end to end from L9's side: hold the prompt,
    show it on request, publish exactly the verdict the human typed."""
    w = await _wired(tmp_path)
    responses: list[dict] = []

    async def spy(event) -> None:
        responses.append(event.payload)

    w.bus.subscribe(EventType.ACTION_CONFIRMATION_RESPONSE, spy)
    try:
        w.bus.publish(
            Event(
                event_type=EventType.ACTION_CONFIRMATION_REQUEST,
                source="action",
                payload={
                    "request_id": "abc123",
                    "action": "run_command",
                    "tier": "dangerous",
                    "summary": "run /usr/bin/pkill with 1 argument(s), 30.0s budget",
                    "reason": "user requested via run",
                    "requested_at": "2026-09-01T12:00:00+00:00",
                },
            )
        )
        await w.bus.join()

        listed = await w.request({"op": "confirmations"})
        approved = await w.request({"op": "confirm", "request_id": "abc123", "approved": True})
        again = await w.request({"op": "confirm", "request_id": "abc123", "approved": True})
        unknown = await w.request({"op": "confirm", "request_id": "nope", "approved": True})
        await w.bus.join()
    finally:
        await _teardown(w)

    assert [c["request_id"] for c in listed["confirmations"]] == ["abc123"]
    assert listed["confirmations"][0]["summary"].startswith("run /usr/bin/pkill")
    assert approved["ok"] is True and approved["approved"] is True
    assert again["ok"] is False and "no confirmation is waiting" in again["error"]
    assert unknown["ok"] is False
    assert responses == [{"request_id": "abc123", "approved": True}]


async def test_a_prompt_is_retired_when_l7_stops_waiting(tmp_path) -> None:
    """Regression (found by `scripts/validate_b7_confirmation.py`): an expired
    confirmation used to sit in the terminal forever. L7's completion event
    carries the `confirmation_id`, and L9 retires the prompt on it."""
    w = await _wired(tmp_path)
    try:
        w.bus.publish(
            Event(
                event_type=EventType.ACTION_CONFIRMATION_REQUEST,
                source="action",
                payload={"request_id": "r1", "action": "run_command", "tier": "dangerous"},
            )
        )
        await w.bus.join()
        assert len((await w.request({"op": "confirmations"}))["confirmations"]) == 1

        w.bus.publish(
            Event(
                event_type=EventType.ACTION_TRIGGERED,
                source="action",
                payload={
                    "ok": False,
                    "detail": "refused: not confirmed (denied or expired)",
                    "confirmation_id": "r1",
                    "intent": {"kind": "run_command", "reason": "r"},
                },
            )
        )
        await w.bus.join()
        after = await w.request({"op": "confirmations"})
        late = await w.request({"op": "confirm", "request_id": "r1", "approved": True})
    finally:
        await _teardown(w)

    assert after["confirmations"] == []
    assert late["ok"] is False


async def test_a_prompt_older_than_the_timeout_is_never_offered(tmp_path) -> None:
    """Defence in depth: even if L7's completion event were lost, a prompt past
    its timeout cannot be approved."""
    clock = FakeClock()
    w = await _wired(tmp_path, clock=clock, action_confirmation_timeout_seconds=10)
    try:
        w.bus.publish(
            Event(
                event_type=EventType.ACTION_CONFIRMATION_REQUEST,
                source="action",
                payload={
                    "request_id": "old",
                    "action": "run_command",
                    "tier": "dangerous",
                    "requested_at": clock.now().isoformat(),
                },
            )
        )
        await w.bus.join()
        assert len((await w.request({"op": "confirmations"}))["confirmations"]) == 1

        await clock.advance(60)
        listed = await w.request({"op": "confirmations"})
        late = await w.request({"op": "confirm", "request_id": "old", "approved": True})
    finally:
        await _teardown(w)

    assert listed["confirmations"] == []
    assert late["ok"] is False


async def test_a_denial_is_relayed_as_a_denial(tmp_path) -> None:
    w = await _wired(tmp_path)
    responses: list[dict] = []

    async def spy(event) -> None:
        responses.append(event.payload)

    w.bus.subscribe(EventType.ACTION_CONFIRMATION_RESPONSE, spy)
    try:
        w.bus.publish(
            Event(
                event_type=EventType.ACTION_CONFIRMATION_REQUEST,
                source="action",
                payload={"request_id": "d1", "action": "run_command", "tier": "dangerous"},
            )
        )
        await w.bus.join()
        resp = await w.request({"op": "confirm", "request_id": "d1", "approved": False})
        await w.bus.join()
    finally:
        await _teardown(w)

    assert resp["approved"] is False
    assert responses == [{"request_id": "d1", "approved": False}]


async def test_health_op_bridges_request_and_report_over_the_bus(tmp_path) -> None:
    w = await _wired(tmp_path)

    async def fake_l10(_event: Event) -> None:
        w.bus.publish(
            Event(
                event_type=EventType.SYSTEM_HEALTH_REPORT,
                source="orchestrator",
                payload={"health": {"ok": True, "uptime_seconds": 42.0}},
            )
        )

    w.bus.subscribe(EventType.SYSTEM_HEALTH_REQUEST, fake_l10)
    try:
        resp = await w.request({"op": "health"})
    finally:
        await _teardown(w)
    assert resp["ok"] is True
    assert resp["health"]["uptime_seconds"] == 42.0


async def test_health_op_times_out_cleanly_when_l10_is_silent(tmp_path) -> None:
    w = await _wired(tmp_path)
    try:
        resp = await w.request({"op": "health"})
    finally:
        await _teardown(w)
    assert resp["ok"] is False and "timed out" in resp["error"]


async def test_run_command_is_ram_only_and_never_on_disk(tmp_path) -> None:
    marker = "zzq_secret_kernel_panic_marker"
    w = await _wired(tmp_path)
    try:
        await w.request({"op": "run", "cmd": f"pkill {marker}"})
        history = w.layer.conversation_history
    finally:
        await _teardown(w)

    assert [m.role for m in history] == [MessageRole.USER]
    assert marker in history[0].content
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert marker not in path.read_text("utf-8", errors="ignore"), path


async def test_ipc_payloads_are_redacted_in_logs(tmp_path, caplog) -> None:
    marker = "zzq_do_not_log_this_command"
    caplog.set_level(logging.DEBUG, logger="neuropaca.interface.layer")
    w = await _wired(tmp_path)
    try:
        await w.request({"op": "run", "cmd": marker})
    finally:
        await _teardown(w)

    ipc_lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith(("L9 <-", "L9 ->"))
    ]
    assert ipc_lines  # the debug lines were emitted
    assert all(marker not in line for line in ipc_lines)
    assert any("redacted" in line for line in ipc_lines)


# --------------------------------------------------------------------------- 4
# CLI client parsing


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["health"], {"op": "health"}),
        (["insights"], {"op": "insights"}),
        (["notifications"], {"op": "notifications"}),
        (["confirmations"], {"op": "confirmations"}),
        (["confirm", "abc123"], {"op": "confirm", "request_id": "abc123", "approved": True}),
        (
            ["confirm", "abc123", "--deny"],
            {"op": "confirm", "request_id": "abc123", "approved": False},
        ),
        (
            ["run", "pkill", "-f", "webpack"],
            {"op": "run", "cmd": "pkill -f webpack", "backup": False},
        ),
        (
            ["run", "--backup", "systemctl --user restart x"],
            {"op": "run", "cmd": "systemctl --user restart x", "backup": True},
        ),
    ],
)
def test_cli_parse(argv: list[str], expected: dict) -> None:
    request, _ = cli._parse(argv)
    assert request == expected


def test_cli_parse_rejects_garbage() -> None:
    with pytest.raises(cli._CliError):
        cli._parse([])
    with pytest.raises(cli._CliError):
        cli._parse(["ask", "what is up"])  # the `$` grammar is gone
    with pytest.raises(cli._CliError, match="needs a command"):
        cli._parse(["run"])
    # `confirm` names exactly one outstanding request — never a wildcard.
    with pytest.raises(cli._CliError, match="exactly one request id"):
        cli._parse(["confirm"])
    with pytest.raises(cli._CliError, match="exactly one request id"):
        cli._parse(["confirm", "a", "b"])


def test_cli_reports_a_missing_daemon(capsys) -> None:
    code = cli.main(["health", "--socket", "/nonexistent/neuropaca.sock"])
    assert code == 1
    assert "cannot reach the daemon" in capsys.readouterr().err


# --------------------------------------------------------------------------- 4b
# The interactive shell (B10, re-scoped B12) — interface/repl.py, a command menu.


@pytest.mark.parametrize(
    "line, argv",
    [
        ("health", ["health"]),
        ("doctor", ["doctor"]),
        ("overview", ["overview"]),
        ("insights", ["insights"]),
        ("tell src/neuropaca/interface/layer.py", ["tell", "src/neuropaca/interface/layer.py"]),
        ("tell drive/pressure.py --explain", ["tell", "drive/pressure.py", "--explain"]),
        ("run pkill -f webpack", ["run", "pkill -f webpack"]),
        (
            "run --backup systemctl --user restart x",
            ["run", "--backup", "systemctl --user restart x"],
        ),
        ("confirm abc123 --deny", ["confirm", "abc123", "--deny"]),
        # short aliases
        ("status", ["health"]),
        ("notes", ["notifications"]),
        ("pending", ["confirmations"]),
    ],
)
def test_repl_translate(line: str, argv: list[str]) -> None:
    from neuropaca.interface import repl

    assert repl._translate(line) == argv


def test_repl_translate_rejects_free_text() -> None:
    from neuropaca.interface import repl

    for line in ("how is the graph stored", "wat", "$doctor", "?why", "!ask hi"):
        assert isinstance(repl._translate(line), repl._UnknownVerb)


def test_repl_translations_all_reach_a_real_dispatcher() -> None:
    """Whatever the menu emits, an offline verb, the describe dispatcher, or a
    clean `_parse` must take it."""
    from neuropaca.interface import offline, repl

    for line in ("doctor", "health", "overview", "tell x.py", "run true", "confirmations"):
        argv = repl._translate(line)
        assert not isinstance(argv, repl._UnknownVerb)
        if argv[0] in offline.OFFLINE_VERBS or argv[0] in ("tell", "overview"):
            continue
        cli._parse(argv)  # raises cli._CliError if the menu produced garbage


def test_cli_help_prints_the_full_guide(capsys) -> None:
    for form in (["help"], ["--help"], ["-h"], ["-help"]):
        assert cli.main(form) == 0
        out = capsys.readouterr().out
        assert "understand the project" in out
        assert "tell <path>" in out


def test_cli_no_args_without_a_tty_is_a_usage_error(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main([]) == 2
    assert "usage: neuropaca" in capsys.readouterr().err


async def test_cli_run_end_to_end_against_a_live_socket(tmp_path, capsys) -> None:
    w = await _wired(tmp_path)
    try:
        code = await asyncio.get_running_loop().run_in_executor(
            None, cli.main, ["run", "true", "--socket", w.sock]
        )
    finally:
        await _teardown(w)
    assert code == 0
    assert "handed to the action layer" in capsys.readouterr().out


# --------------------------------------------------------------- insight surfacing


def _insight(node_id: str, *, category: str = "anomaly", confidence: float = 0.9) -> Insight:
    return Insight(
        category=category,
        cited_node_ids=("app:webpack",),
        source_signal=SignalType.HIGH_LOAD,
        confidence=confidence,
        snapshot_count=1,
        node_id=node_id,
    )


def _insight_event(insight: Insight) -> Event:
    return Event(
        event_type=EventType.INSIGHT_GENERATED, source="learning", payload={"insight": insight}
    )


async def test_insight_surfacing_priority_surface_once_and_daily_cap(tmp_path) -> None:
    clock = FakeClock()
    w = await _wired(tmp_path, clock=clock)
    try:
        routine = _insight("insight:routine", category="routine")
        weak = _insight("insight:weak", confidence=0.3)
        await w.layer.on_insight_generated(_insight_event(routine))
        await w.layer.on_insight_generated(_insight_event(weak))
        assert w.layer._pending_insights == []

        for i in range(5):  # cap is 3/day
            await w.layer.on_insight_generated(_insight_event(_insight(f"insight:{i}")))
        assert w.layer._surfaced_today == 3
        assert len(w.layer._pending_insights) == 3

        await w.layer.on_insight_generated(_insight_event(_insight("insight:0")))
        assert w.layer._surfaced_today == 3

        await clock.advance(24 * 3600)
        await w.layer.on_insight_generated(_insight_event(_insight("insight:tomorrow")))
        assert w.layer._surfaced_today == 1
    finally:
        await _teardown(w)


async def test_surfaced_at_is_stamped_and_survives_a_restart(tmp_path) -> None:
    clock = FakeClock()
    w = await _wired(tmp_path, clock=clock)
    try:
        await w.layer.on_insight_generated(_insight_event(_insight("insight:abc")))
        await w.bus.join()
        node = w.graph.get_node("insight:abc")
        assert node is not None and node.surfaced_at is not None
        await w.graph.save()
    finally:
        await _teardown(w)

    graph2 = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await graph2.load()
    assert graph2.get_node("insight:abc").surfaced_at is not None

    bus2 = EventBus.get_instance()
    await bus2.start()
    layer2 = InterfaceLayer(
        bus2,
        Config(inference_backend="fake"),
        graph2,
        BitNetRuntime.get_instance(FakeInferenceBackend()),
        clock=FakeClock(),
        socket_path=str(tmp_path / "np2.sock"),
    )
    await layer2.initialize()
    await layer2.start()
    try:
        await layer2.on_insight_generated(_insight_event(_insight("insight:abc")))
        assert layer2._pending_insights == []  # surface-once held across the restart
    finally:
        await layer2.stop()
        await bus2.stop()


# --------------------------------------------------------------- audit regressions


async def test_surfaced_ids_are_bounded_and_keep_the_newest(tmp_path) -> None:
    from neuropaca.interface.layer import _MAX_SURFACED_IDS

    clock = FakeClock()
    w = await _wired(tmp_path, clock=clock)
    try:
        total = _MAX_SURFACED_IDS + 50
        for i in range(total):
            w.layer._remember_surfaced(f"insight:{i}")
            if i % 3 == 0:
                w.layer._surfaced_today = 0
        assert len(w.layer._surfaced_ids) == _MAX_SURFACED_IDS
        assert f"insight:{total - 1}" in w.layer._surfaced_ids, "newest must survive"
        assert "insight:0" not in w.layer._surfaced_ids, "oldest must be evicted"
    finally:
        await _teardown(w)


async def test_pending_insights_are_bounded_when_nothing_drains_them(tmp_path) -> None:
    from neuropaca.interface.layer import _MAX_PENDING_INSIGHTS

    clock = FakeClock()
    w = await _wired(tmp_path, clock=clock)
    try:
        for day in range(_MAX_PENDING_INSIGHTS + 20):
            await w.layer.on_insight_generated(_insight_event(_insight(f"insight:d{day}")))
            await clock.advance(24 * 3600)
        assert len(w.layer._pending_insights) == _MAX_PENDING_INSIGHTS
        assert w.layer._pending_insights[-1].node_id.endswith(f"d{_MAX_PENDING_INSIGHTS + 19}")
    finally:
        await _teardown(w)
