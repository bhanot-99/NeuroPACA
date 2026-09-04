"""B5 · Interface (L9) — schemas, retrieval, dual-model routing, IPC, surfacing.

Grouped by the phase's implementation steps. No test loads a real model
(rules.md §8) and no test sleeps (`FakeClock`).
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
from neuropaca.core.models import Event, Node
from neuropaca.interface import cli
from neuropaca.interface.layer import InterfaceLayer
from neuropaca.interface.message import Message
from neuropaca.learning.insight import Insight
from neuropaca.learning.prompts import (
    alias_nodes,
    build_answer_grammar,
    build_answer_prompt,
    parse_answer,
)

# --------------------------------------------------------------------------- 1
# Core schemas & retrieval


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


async def test_search_by_label_exact_hub_match_seeds_the_domain(tmp_path) -> None:
    gm = await _graph(tmp_path)
    hits = gm.search_by_label("what have I been doing in engineering today")
    assert "domain:engineering" in {n.id for n in hits}


async def test_search_by_label_ranks_by_relevance_and_caps(tmp_path) -> None:
    gm = await _graph(tmp_path)
    for i in range(5):
        await gm.add_node(f"app:cpu{i}", NodeType.APP, {"label": f"cpu hog {i}"})
    await gm.update_node("app:cpu3", {"relevance_score": 9.0})

    hits = gm.search_by_label("cpu", limit=2)
    assert len(hits) == 2
    assert hits[0].id == "app:cpu3"


async def test_search_by_label_empty_query_returns_nothing(tmp_path) -> None:
    gm = await _graph(tmp_path)
    assert gm.search_by_label("   ") == []


def test_message_role_is_an_enum_not_a_string() -> None:
    m = Message(role=MessageRole.USER, content="hi")
    assert m.role is MessageRole.USER
    assert m.role == "user"  # StrEnum wire form
    assert m.related_node_ids == ()


# --------------------------------------------------------------------------- 2
# Dual-backend inference & the $? prompt / grammar / gate


def _nodes() -> list[Node]:
    return [
        Node(id="app:webpack", node_type=NodeType.APP, label="webpack", relevance_score=8.1),
        Node(id="file:/src/app", node_type=NodeType.FILE, label="/src/app", relevance_score=7.4),
    ]


def test_build_answer_grammar_rejects_non_aliases() -> None:
    with pytest.raises(ValueError, match="local alias"):
        build_answer_grammar(["app:webpack"])
    with pytest.raises(ValueError, match="at least one"):
        build_answer_grammar([])


def test_parse_answer_accepts_a_grounded_sentence() -> None:
    raw = '{"insight": "webpack is pinning a core.", "cited_nodes": ["n1"], "confidence": 0.8}'
    ans = parse_answer(raw, {"n1": "app:webpack"}, {"n1": "webpack"})
    assert ans is not None
    assert ans.cited_node_ids == ("app:webpack",)
    assert ans.confidence == 0.8


def test_parse_answer_discards_an_ungrounded_sentence() -> None:
    raw = '{"insight": "Something is slow.", "cited_nodes": ["n1"], "confidence": 0.8}'
    assert parse_answer(raw, {"n1": "app:webpack"}, {"n1": "webpack"}) is None


def test_parse_answer_discards_abstain_and_bad_vocab() -> None:
    assert parse_answer('{"insight": null, "cited_nodes": [], "confidence": 0.0}', {}, {}) is None
    raw = '{"insight": "webpack is busy", "cited_nodes": ["n9"], "confidence": 0.5}'
    assert parse_answer(raw, {"n1": "app:webpack"}, {"n1": "webpack"}) is None


def test_fake_backend_answers_the_dollar_query_grammar_with_a_grounded_citation() -> None:
    aliased = alias_nodes(_nodes())
    aliases = [a for a, _ in aliased]
    grammar = build_answer_grammar(aliases)
    prompt = build_answer_prompt("what is using my CPU?", aliased)

    raw = FakeInferenceBackend().infer(prompt, 96, 0.0, grammar)
    ans = parse_answer(
        raw,
        {a: n.id for a, n in aliased},
        {a: n.label for a, n in aliased},
    )
    assert ans is not None
    assert ans.cited_node_ids == ("app:webpack",)


async def test_bitnet_runtime_routes_interactive_to_the_second_backend() -> None:
    loop_be, chat_be = FakeInferenceBackend(), FakeInferenceBackend()
    rt = BitNetRuntime.get_instance(loop_be, chat_be)
    assert rt.interactive_configured

    await rt.infer_async("loop prompt", 16)
    await rt.infer_async("chat prompt", 16, interactive=True)

    assert [c[0] for c in loop_be.calls] == ["loop prompt"]
    assert [c[0] for c in chat_be.calls] == ["chat prompt"]


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


def test_insight_extractive_grammar_still_works_after_the_dollar_branch() -> None:
    from neuropaca.learning.prompts import build_insight_grammar, parse_insight

    g = build_insight_grammar(["n1"])
    raw = FakeInferenceBackend().infer("p", 48, 0.0, g)
    ins = parse_insight(
        raw, {"n1": "app:x"}, source_signal=SignalType.HIGH_LOAD, confidence=0.9, snapshot_count=1
    )
    assert ins is not None and ins.cited_node_ids == ("app:x",)


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


async def _wired(tmp_path, *, clock: FakeClock | None = None, **cfg) -> _Wired:
    bus = EventBus.get_instance()
    await bus.start()
    graph = GraphMemory.get_instance(persistence_path=str(tmp_path / "graph.json"))
    await graph.load()
    await graph.add_node(
        "app:webpack", NodeType.APP, {"label": "webpack (cpu heavy)", "relevance_score": 8.2}
    )
    sock = str(tmp_path / "np.sock")
    runtime = BitNetRuntime.get_instance(
        FakeInferenceBackend(), create_interactive_backend(Config(inference_backend="fake"))
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


async def test_socket_query_answers_dollar_with_a_grounded_node_label(tmp_path) -> None:
    """B5 exit: `$ what's using my CPU` -> an answer citing real node labels."""
    w = await _wired(tmp_path)
    try:
        resp = await w.request({"op": "query", "prefix": "$", "text": "what's using my CPU"})
    finally:
        await _teardown(w)

    assert resp["ok"] is True
    assert resp["source"] == "model"
    assert "webpack (cpu heavy)" in resp["answer"]  # a real node label
    assert resp["cited"] == ["app:webpack"]


async def test_bang_and_double_dollar_are_relayed_to_l7_not_executed(tmp_path) -> None:
    """B7 (D-14): L9 hands `$!` / `$$` to the action layer and returns at once.

    It publishes exactly one `USER_MESSAGE` per command carrying the prefix, and
    it answers `queued` — never an answer, and never an effect of its own."""
    w = await _wired(tmp_path)
    seen: list[tuple[str, str]] = []

    async def spy(event) -> None:
        seen.append((event.payload["prefix"], event.payload["text"]))

    w.bus.subscribe(EventType.USER_MESSAGE, spy)
    try:
        bang = await w.request({"op": "query", "prefix": "$!", "text": "kill it"})
        dd = await w.request({"op": "query", "prefix": "$$", "text": "clean up"})
        empty = await w.request({"op": "query", "prefix": "$!", "text": ""})
        await w.bus.join()
    finally:
        await _teardown(w)

    assert bang["ok"] is True and bang["queued"] is True and bang["prefix"] == "$!"
    assert dd["ok"] is True and dd["queued"] is True and dd["prefix"] == "$$"
    assert "answer" not in bang and "answer" not in dd
    assert empty["ok"] is False and "empty command" in empty["error"]
    assert seen == [("$!", "kill it"), ("$$", "clean up")]


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
                    "reason": "user requested via $!",
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
    # One prompt, one verdict: a request cannot be answered twice, and an id
    # nobody is waiting on is never published.
    assert again["ok"] is False and "no confirmation is waiting" in again["error"]
    assert unknown["ok"] is False
    assert responses == [{"request_id": "abc123", "approved": True}]


async def test_a_prompt_is_retired_when_l7_stops_waiting(tmp_path) -> None:
    """Regression (found on the target box by `scripts/validate_b7_confirmation.py`):
    an expired confirmation used to sit in the terminal forever, so the next
    `confirm` answered a question nobody was listening to. L7's completion event
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


async def test_conversation_history_is_ram_only_and_never_on_disk(tmp_path) -> None:
    marker = "zzq_secret_kernel_panic_marker"
    w = await _wired(tmp_path)
    try:
        await w.request({"op": "query", "prefix": "$", "text": f"what about {marker}"})
        history = w.layer.conversation_history
    finally:
        await _teardown(w)

    assert [m.role for m in history] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert history[0].content == f"what about {marker}"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert marker not in path.read_text("utf-8", errors="ignore"), path


async def test_ipc_payloads_are_redacted_in_logs(tmp_path, caplog) -> None:
    marker = "zzq_do_not_log_this_query"
    caplog.set_level(logging.DEBUG, logger="neuropaca.interface.layer")
    w = await _wired(tmp_path)
    try:
        await w.request({"op": "query", "prefix": "$", "text": marker})
    finally:
        await _teardown(w)

    ipc_lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith(("L9 <-", "L9 ->"))
    ]
    assert ipc_lines  # the debug lines were emitted
    assert all(marker not in line for line in ipc_lines)
    assert any("redacted" in line for line in ipc_lines)


# --------------------------------------------------------------------------- 4
# CLI client, insight surfacing, surfaced_at persistence


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["health"], {"op": "health"}),
        (["insights"], {"op": "insights"}),
        (["ask", "what", "is", "up"], {"op": "query", "prefix": "$", "text": "what is up"}),
        (["diagnose", "why slow"], {"op": "query", "prefix": "$?", "text": "why slow"}),
        (["$? why slow"], {"op": "query", "prefix": "$?", "text": "why slow"}),
        (["$ hello there"], {"op": "query", "prefix": "$", "text": "hello there"}),
        (["$! kill it"], {"op": "query", "prefix": "$!", "text": "kill it"}),
        (["$$ restart it"], {"op": "query", "prefix": "$$", "text": "restart it"}),
        (["notifications"], {"op": "notifications"}),
        (["confirmations"], {"op": "confirmations"}),
        (["confirm", "abc123"], {"op": "confirm", "request_id": "abc123", "approved": True}),
        (
            ["confirm", "abc123", "--deny"],
            {"op": "confirm", "request_id": "abc123", "approved": False},
        ),
    ],
)
def test_cli_parse(argv: list[str], expected: dict) -> None:
    request, _ = cli._parse(argv)
    assert request == expected


def test_cli_parse_rejects_garbage() -> None:
    with pytest.raises(cli._CliError):
        cli._parse([])
    # `confirm` names exactly one outstanding request — never a wildcard, never
    # "all" (rules.md §5.2: one recorded confirmation per dangerous action).
    with pytest.raises(cli._CliError, match="exactly one request id"):
        cli._parse(["confirm"])
    with pytest.raises(cli._CliError, match="exactly one request id"):
        cli._parse(["confirm", "a", "b"])


def test_cli_reports_a_missing_daemon(capsys) -> None:
    code = cli.main(["health", "--socket", "/nonexistent/neuropaca.sock"])
    assert code == 1
    assert "cannot reach the daemon" in capsys.readouterr().err


async def test_cli_end_to_end_against_a_live_socket(tmp_path, capsys) -> None:
    w = await _wired(tmp_path)
    try:
        code = await asyncio.get_running_loop().run_in_executor(
            None,
            cli.main,
            ["ask", "what's using my CPU", "--socket", w.sock],
        )
    finally:
        await _teardown(w)
    assert code == 0
    assert "webpack (cpu heavy)" in capsys.readouterr().out


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
        # routine / low-confidence are filtered by the priority gate
        routine = _insight("insight:routine", category="routine")
        weak = _insight("insight:weak", confidence=0.3)
        await w.layer.on_insight_generated(_insight_event(routine))
        await w.layer.on_insight_generated(_insight_event(weak))
        assert w.layer._pending_insights == []

        for i in range(5):  # cap is 3/day
            await w.layer.on_insight_generated(_insight_event(_insight(f"insight:{i}")))
        assert w.layer._surfaced_today == 3
        assert len(w.layer._pending_insights) == 3

        # surface-once: replaying an already-surfaced insight does nothing
        await w.layer.on_insight_generated(_insight_event(_insight("insight:0")))
        assert w.layer._surfaced_today == 3

        # local midnight resets the cap
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

    # a fresh process: reload the graph, new InterfaceLayer -> already-surfaced
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
# From the B9 optimisation audit: L9's two collections that grew without a ceiling
# on a daemon meant to run for months.


async def test_surfaced_ids_are_bounded_and_keep_the_newest(tmp_path) -> None:
    """Surface-once bookkeeping outlives the node it describes — an insight is
    pruned at its 48 h TTL but its id had to stay remembered. Remembering every
    id forever made that a leak; the cap keeps the newest, which are the only
    ones a live node can still match."""
    from neuropaca.interface.layer import _MAX_SURFACED_IDS

    clock = FakeClock()
    w = await _wired(tmp_path, clock=clock)
    try:
        total = _MAX_SURFACED_IDS + 50
        for i in range(total):
            w.layer._remember_surfaced(f"insight:{i}")
            if i % 3 == 0:  # keep the daily cap out of it — this is the id store
                w.layer._surfaced_today = 0
        assert len(w.layer._surfaced_ids) == _MAX_SURFACED_IDS
        assert f"insight:{total - 1}" in w.layer._surfaced_ids, "newest must survive"
        assert "insight:0" not in w.layer._surfaced_ids, "oldest must be evicted"
    finally:
        await _teardown(w)


async def test_pending_insights_are_bounded_when_nothing_drains_them(tmp_path) -> None:
    """Insights queue until a CLI client reads them out. Nothing guarantees one
    ever connects, so the queue needs its own ceiling."""
    from neuropaca.interface.layer import _MAX_PENDING_INSIGHTS

    clock = FakeClock()
    w = await _wired(tmp_path, clock=clock)
    try:
        for day in range(_MAX_PENDING_INSIGHTS + 20):
            await w.layer.on_insight_generated(_insight_event(_insight(f"insight:d{day}")))
            await clock.advance(24 * 3600)  # a fresh day, so the 3/day cap never bites
        assert len(w.layer._pending_insights) == _MAX_PENDING_INSIGHTS
        # the tail is kept — the newest insights are the ones worth showing
        assert w.layer._pending_insights[-1].node_id.endswith(f"d{_MAX_PENDING_INSIGHTS + 19}")
    finally:
        await _teardown(w)
