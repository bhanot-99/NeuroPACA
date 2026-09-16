"""
live_conversation.py — Step 8's conversational layer: OpenAI's Realtime
API sitting behind the existing wake word, full-duplex audio in both
directions, with every proposed action still funneled through
dispatch.execute_skill (the exact same tier-gating main.py and daemon.py's
single-shot path already use) via _run_tool() below — this module never
calls actions.DISPATCH directly, and never gains any capability the
existing tier system doesn't already gate.

Verified against the installed `openai` SDK (v3.14.1) source directly, not
assumed from memory — the model ids ("gpt-realtime" et al.), the audio
format (audio/pcm, 24kHz is the only supported rate), the server_vad
turn-detection shape, the nested session["audio"]["input"/"output"] shape,
and the response.output_audio.delta / response.function_call_arguments.done
event names all came from reading openai/types/realtime/*.py in this
project's own .venv — not guessed, and not necessarily current for the
SDK version installed by the time anyone reads this. Re-check before
shipping regardless: an event name or field moving between SDK releases
is a silent no-op here, not a Python error, since these are dict payloads
and BaseModel event attributes rather than something the type checker
would catch.

NOT YET LIVE-TESTED: no OPENAI_API_KEY is configured in this repo as of
writing, so this module is verified for internal consistency (imports,
event routing, the dispatch bridge) but has never actually connected.
Step 8's own plan (STEP8_CONVERSATION_PLAN.txt) names this as the first
thing to do once a real key exists.
"""

import asyncio
import base64
import json
import queue
import threading
import time

from google import genai
from google.genai import types as genai_types
import numpy as np
import sounddevice as sd
from openai import AsyncOpenAI

from audio_bus import audio_bus
import config
import dispatch
from skills import _session_state

REALTIME_SAMPLE_RATE = 24000  # the Realtime API's only supported PCM rate
                               # — unrelated to config.SAMPLE_RATE (16kHz),
                               # which stays wake-word/VAD/whisper-only.
_CHUNK_SAMPLES = REALTIME_SAMPLE_RATE // 50  # 20ms chunks
IDLE_TIMEOUT_SECONDS = 8.0  # both sides silent this long -> end the
                            # session and return to wake-word idle
                            # listening. Longer than daemon.py's
                            # NO_SPEECH_TIMEOUT_SECONDS (4.0) since this is
                            # a standing conversation, not one command.

_VOICE = "marin"  # any of alloy/ash/ballad/coral/echo/sage/shimmer/verse/
                  # marin/cedar work — this is a taste call, not a
                  # correctness one; swap freely.

_TOOLS = [
    {
        "type": "function",
        "name": "open_app",
        "description": "Launch a desktop application by name, e.g. firefox, code, spotify.",
        "parameters": {
            "type": "object",
            "properties": {"app_name": {"type": "string"}},
            "required": ["app_name"],
        },
    },
    {
        "type": "function",
        "name": "web_search",
        "description": "Search Google or YouTube in the default browser.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "site": {"type": "string", "enum": ["google", "youtube"]},
            },
            "required": ["query", "site"],
        },
    },
    {
        "type": "function",
        "name": "set_volume",
        "description": "Set system output volume to an exact percentage (0-100).",
        "parameters": {
            "type": "object",
            "properties": {"percent": {"type": "integer"}},
            "required": ["percent"],
        },
    },
    {
        "type": "function",
        "name": "set_brightness",
        "description": "Set screen brightness to an exact percentage (0-100).",
        "parameters": {
            "type": "object",
            "properties": {"percent": {"type": "integer"}},
            "required": ["percent"],
        },
    },
    {
        "type": "function",
        "name": "run_terminal",
        "description": "Run an arbitrary shell command on the user's Linux machine.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "type": "function",
        "name": "read_pdf",
        "description": "Read a PDF file by name from the user's common folders "
                       "(Downloads, Documents, Desktop, etc.) and return its extracted text.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
]

_SYSTEM_INSTRUCTIONS = (
    "You are a voice assistant named Jarvis, talking with the user out loud "
    "in real time. Be warm, brief, and conversational — this is spoken "
    "dialogue, not a document. When the user asks for something actionable, "
    "call the matching tool instead of just describing what you'd do."
)


_GEMINI_TOOLS = [{
    "function_declarations": [
        {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["parameters"],
        }
        for t in _TOOLS
    ]
}]


class RealtimeUnavailable(Exception):
    """Raised when a Realtime session can't be established or drops mid-
    call — the caller (daemon.py) is expected to fall back to the existing
    single-shot local pipeline, not treat this as fatal."""


def run_session(manual_stop_event: threading.Event, notify) -> None:
    """Blocking entry point — runs one full conversation session and
    returns when it ends naturally (idle timeout, a tray click via
    manual_stop_event, or the model calling sleep_stop_listening). Raises
    RealtimeUnavailable if the session can't be established or drops
    before any of those.

    `notify` is daemon.py's own _notify(title, body, speak_text=...)
    function, passed in rather than imported back from daemon — this
    module has no reason to know daemon.py exists otherwise, and
    importing it back would be circular (daemon.py is the one importing
    this module)."""
    try:
        if config.CONVERSATION_BACKEND in ("gemini", "gemini-2.5-flash"):
            asyncio.run(_run_session_gemini_async(manual_stop_event, notify))
        else:
            asyncio.run(_run_session_openai_async(manual_stop_event, notify))
    except RealtimeUnavailable:
        raise
    except Exception as exc:
        raise RealtimeUnavailable(str(exc)) from exc


async def _run_session_gemini_async(manual_stop_event: threading.Event, notify) -> None:
    if not config.GEMINI_API_KEY:
        raise RealtimeUnavailable("GEMINI_API_KEY is not set")

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    state = {"last_activity": time.monotonic(), "response_in_progress": False}
    stop = asyncio.Event()
    confirm_pending = threading.Event()

    config_live = genai_types.LiveConnectConfig(
        response_modalities=[genai_types.Modality.AUDIO],
        tools=_GEMINI_TOOLS,
        system_instruction=genai_types.Content(parts=[genai_types.Part.from_text(text=_SYSTEM_INSTRUCTIONS)]),
    )

    playback_queue: queue.Queue = queue.Queue()

    def _playback_callback(outdata, frames, _time_info, _status) -> None:
        needed = frames * 2  # int16 mono = 2 bytes/sample
        buf = bytearray()
        while len(buf) < needed:
            try:
                buf += playback_queue.get_nowait()
            except queue.Empty:
                break
        buf = bytes(buf[:needed]).ljust(needed, b"\x00")
        outdata[:] = np.frombuffer(buf, dtype="int16").reshape(-1, 1)

    try:
        async with client.aio.live.connect(model=config.GEMINI_LIVE_MODEL, config=config_live) as session:
            with audio_bus.subscribe(sample_rate=REALTIME_SAMPLE_RATE, chunk_size=_CHUNK_SAMPLES) as mic_sub, \
                 sd.OutputStream(samplerate=REALTIME_SAMPLE_RATE, channels=1, dtype="int16",
                                  blocksize=_CHUNK_SAMPLES, callback=_playback_callback):

                async def _send_mic_audio():
                    while not stop.is_set():
                        chunk = await asyncio.to_thread(mic_sub.get_bytes)
                        await session.send_realtime_input(
                            media=genai_types.Blob(data=chunk, mime_type="audio/pcm;rate=24000")
                        )

                async def _watch_idle_and_manual_stop():
                    while not stop.is_set():
                        if manual_stop_event.is_set() and not confirm_pending.is_set():
                            manual_stop_event.clear()
                            stop.set()
                            return
                        idle_for = time.monotonic() - state["last_activity"]
                        if idle_for > IDLE_TIMEOUT_SECONDS and not state["response_in_progress"]:
                            stop.set()
                            return
                        await asyncio.sleep(0.2)

                async def _handle_events():
                    async for response in session.receive():
                        state["last_activity"] = time.monotonic()
                        sc = response.server_content
                        if sc is not None:
                            if sc.interrupted:
                                while not playback_queue.empty():
                                    try:
                                        playback_queue.get_nowait()
                                    except queue.Empty:
                                        break
                            if sc.model_turn is not None:
                                state["response_in_progress"] = True
                                for part in sc.model_turn.parts:
                                    if part.inline_data is not None:
                                        playback_queue.put(part.inline_data.data)
                            if sc.turn_complete:
                                state["response_in_progress"] = False

                        if response.tool_call is not None:
                            for call in response.tool_call.function_calls:
                                name, call_id, args = call.name, call.id, (call.args or {})

                                if name == "sleep_stop_listening":
                                    _session_state.go_to_sleep()
                                    stop.set()
                                    return

                                output = await asyncio.to_thread(
                                    _run_tool, name, args, notify=notify,
                                    manual_stop_event=manual_stop_event,
                                    confirm_pending=confirm_pending,
                                )
                                await session.send_tool_response(
                                    function_responses=[genai_types.FunctionResponse(
                                        id=call_id,
                                        name=name,
                                        response={"output": output},
                                    )]
                                )

                        if stop.is_set():
                            return

                send_task = asyncio.create_task(_send_mic_audio())
                watch_task = asyncio.create_task(_watch_idle_and_manual_stop())
                events_task = asyncio.create_task(_handle_events())

                done, pending = await asyncio.wait(
                    {send_task, watch_task, events_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                stop.set()
                for t in pending:
                    t.cancel()
                for t in (send_task, watch_task, events_task):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

                for t in done:
                    if t.exception() is not None:
                        raise t.exception()
    except RealtimeUnavailable:
        raise
    except Exception as exc:
        raise RealtimeUnavailable(str(exc)) from exc


async def _run_session_openai_async(manual_stop_event: threading.Event, notify) -> None:
    if not config.OPENAI_API_KEY:
        raise RealtimeUnavailable("OPENAI_API_KEY is not set")

    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    state = {"last_activity": time.monotonic(), "response_in_progress": False}
    stop = asyncio.Event()
    # Set for the duration of a DANGEROUS-tier confirm wait (_run_tool
    # below) — manual_stop_event is the SAME tray-click event daemon.py
    # already overloads for both "confirm this" and "trigger/stop
    # listening" elsewhere in this codebase. Without this flag, a click
    # meant to confirm a dangerous action mid-conversation would also read
    # as "end the whole session" to _watch_manual_stop below, racing the
    # confirm and ending the call right as the action is approved.
    confirm_pending = threading.Event()

    try:
        async with client.realtime.connect(model=config.REALTIME_MODEL) as connection:
            await connection.session.update(session={
                "type": "realtime",
                "model": config.REALTIME_MODEL,
                "instructions": _SYSTEM_INSTRUCTIONS,
                "tools": _TOOLS,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": REALTIME_SAMPLE_RATE},
                        "turn_detection": {
                            "type": "server_vad",
                            "interrupt_response": True,
                            "create_response": True,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": REALTIME_SAMPLE_RATE},
                        "voice": _VOICE,
                    },
                },
            })

            playback_queue: queue.Queue = queue.Queue()

            def _playback_callback(outdata, frames, _time_info, _status) -> None:
                needed = frames * 2  # int16 mono = 2 bytes/sample
                buf = bytearray()
                while len(buf) < needed:
                    try:
                        buf += playback_queue.get_nowait()
                    except queue.Empty:
                        break
                buf = bytes(buf[:needed]).ljust(needed, b"\x00")
                outdata[:] = np.frombuffer(buf, dtype="int16").reshape(-1, 1)

            with audio_bus.subscribe(sample_rate=REALTIME_SAMPLE_RATE, chunk_size=_CHUNK_SAMPLES) as mic_sub, \
                 sd.OutputStream(samplerate=REALTIME_SAMPLE_RATE, channels=1, dtype="int16",
                                  blocksize=_CHUNK_SAMPLES, callback=_playback_callback):

                async def _send_mic_audio():
                    while not stop.is_set():
                        chunk = await asyncio.to_thread(mic_sub.get_bytes)
                        await connection.input_audio_buffer.append(
                            audio=base64.b64encode(chunk).decode("ascii")
                        )

                async def _watch_idle_and_manual_stop():
                    while not stop.is_set():
                        if manual_stop_event.is_set() and not confirm_pending.is_set():
                            manual_stop_event.clear()
                            stop.set()
                            return
                        idle_for = time.monotonic() - state["last_activity"]
                        if idle_for > IDLE_TIMEOUT_SECONDS and not state["response_in_progress"]:
                            stop.set()
                            return
                        await asyncio.sleep(0.2)

                async def _handle_events():
                    async for event in connection:
                        state["last_activity"] = time.monotonic()
                        etype = event.type

                        if etype == "response.output_audio.delta":
                            playback_queue.put(base64.b64decode(event.delta))

                        elif etype == "response.created":
                            state["response_in_progress"] = True

                        elif etype == "response.done":
                            state["response_in_progress"] = False

                        elif etype == "response.function_call_arguments.done":
                            name, call_id = event.name, event.call_id
                            args = json.loads(event.arguments) if event.arguments else {}

                            if name == "sleep_stop_listening":
                                _session_state.go_to_sleep()
                                stop.set()
                                return

                            output = await asyncio.to_thread(
                                _run_tool, name, args, notify=notify,
                                manual_stop_event=manual_stop_event,
                                confirm_pending=confirm_pending,
                            )
                            await connection.conversation.item.create(item={
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": output,
                            })
                            await connection.response.create()

                        elif etype == "error":
                            raise RealtimeUnavailable(str(event))

                        if stop.is_set():
                            return

                send_task = asyncio.create_task(_send_mic_audio())
                watch_task = asyncio.create_task(_watch_idle_and_manual_stop())
                events_task = asyncio.create_task(_handle_events())

                done, pending = await asyncio.wait(
                    {send_task, watch_task, events_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                stop.set()
                for t in pending:
                    t.cancel()
                for t in (send_task, watch_task, events_task):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

                for t in done:
                    if t.exception() is not None:
                        raise t.exception()
    except RealtimeUnavailable:
        raise
    except Exception as exc:
        raise RealtimeUnavailable(str(exc)) from exc


def _run_tool(name: str, args: dict, *, notify, manual_stop_event: threading.Event,
              confirm_pending: threading.Event) -> str:
    """Runs one Realtime-proposed tool call through the exact same
    tier-gated chokepoint (dispatch.execute_skill) main.py and daemon.py's
    single-shot path already use. DANGEROUS tools pause on the same 10s
    tray-confirm window daemon.py already shows there — this blocks the
    calling thread (not the asyncio event loop; see the asyncio.to_thread
    call site) until the user confirms, cancels, or the window lapses,
    while the rest of the live session keeps running normally."""
    text = f"[live conversation] {name}({args})"
    result = dispatch.execute_skill(
        name, args, text=text,
        # A spoken "about to run X" REVIEW notice would talk over the
        # live session's own audio — the model narrates instead, so this
        # is a silent pause rather than main.py's/daemon.py's own notices.
        on_review=lambda name, args: None,
    )

    if result["outcome"] == "needs_confirmation":
        pause, tier = result["pause"], result["tier"]
        warning_text = ("\n⚠ " + "; ".join(pause["warnings"])) if pause["warnings"] else ""
        notify(
            f"Confirm: {pause['preview']}",
            f"Click the tray within 10s to run it.{warning_text}",
            speak_text=f"Please confirm: {pause['preview']}",
        )
        confirm_pending.set()
        try:
            confirmed = manual_stop_event.wait(timeout=10)
            if confirmed:
                manual_stop_event.clear()
        finally:
            confirm_pending.clear()
        result = dispatch.finish_confirm(
            result["generator"], None if confirmed else "",
            name=name, args=args, text=text, tier=tier,
        )
        if result["outcome"] == "cancelled":
            return "Cancelled — no confirmation within 10 seconds."

    return result["output"].strip() or f"Ran {name}."
