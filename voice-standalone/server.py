"""
server.py — FastAPI WebSocket Gateway & Web UI Server for Voice Standalone.

Provides:
  - Web UI Dashboard on GET / (embedded HTML5 + TailwindCSS)
  - WebSocket /ws/chat for streaming text chat, audio upload, and fast-path execution
  - WebSocket /ws/audio for real-time PCM audio streaming
  - Interactive DANGEROUS safety tier confirmation modal integration
  - REST endpoints for system status, skill execution, and telemetry
"""

import asyncio
import base64
import json
import os
import subprocess
import tempfile
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from _compound_splitter import split_compound_utterance
import config
import dispatch
import llm_intent
import skills
from skills import _session_state, _tiers, semantic_match
import stt
import wiki_fastpath

try:
    import tts
except Exception:
    tts = None


def _speak_bg(text: str) -> None:
    if tts is not None and text and text.strip():
        try:
            tts.speak(text)
        except Exception as e:
            print(f"[tts warning] {e}")


async def _stream_tts_audio(
    websocket: WebSocket,
    text: str,
    lang: str = "en",
    skill_name: Optional[str] = None,
    engine: Optional[str] = None,
):
    """Splits text into clauses and streams 16-bit PCM chunks (Piper: 22050Hz, Kokoro: 24000Hz) to client."""
    if tts is None or not text.strip():
        return
    clauses = tts.split_into_clauses(text)
    if not clauses:
        return

    from audio_bus import audio_bus
    audio_bus.set_speaking_state(True)
    try:
        for c_idx, clause in enumerate(clauses):
            if not audio_bus.is_speaking():
                break
            is_last_clause = (c_idx == len(clauses) - 1)

            def _gen():
                return list(tts.synthesize_stream(clause, lang=lang, engine=engine, skill_name=skill_name))

            chunks = await asyncio.to_thread(_gen)
            for ch_idx, (pcm, sr, is_last_chunk) in enumerate(chunks):
                if not audio_bus.is_speaking():
                    break
                if not pcm:
                    continue
                b64_pcm = base64.b64encode(pcm).decode("ascii")
                try:
                    await websocket.send_json({
                        "type": "audio_chunk",
                        "pcm_b64": b64_pcm,
                        "sample_rate": sr,
                        "channels": 1,
                        "is_first": (c_idx == 0 and ch_idx == 0),
                        "is_last": (is_last_clause and is_last_chunk),
                    })
                except Exception:
                    return
    except Exception as exc:
        print(f"[ws stream error] {exc}")
    finally:
        audio_bus.set_speaking_state(False)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app = FastAPI(title="NeuroPaca Voice Gateway", version="1.0.0")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Store pending DANGEROUS confirmations awaiting user approval from the Web UI:
# confirm_id -> {"generator": gen, "future": asyncio.Future, "skill": name, "args": args, "tier": tier}
_pending_confirmations: Dict[str, Dict[str, Any]] = {}


@app.on_event("startup")
async def startup_event():
    # Warm up semantic index in background thread if not built
    try:
        await asyncio.to_thread(semantic_match._ensure_index_built)
    except Exception as e:
        print(f"[warning] Semantic matcher warmup failed: {e}")

    # Warm up persistent TTS worker
    try:
        if tts is not None:
            await asyncio.to_thread(tts.warm_up)
    except Exception as e:
        print(f"[warning] TTS warmup failed: {e}")


@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/status")
async def get_status():
    """Returns platform state, safety tier status, audio info, and battery level."""
    # Check battery
    battery_info = "Unknown"
    try:
        res = subprocess.run(["upower", "-e"], capture_output=True, text=True, timeout=1.0)
        bat_path = next((line for line in res.stdout.splitlines() if "BAT" in line), None)
        if bat_path:
            b_res = subprocess.run(["upower", "-i", bat_path], capture_output=True, text=True, timeout=1.0)
            for l in b_res.stdout.splitlines():
                if "percentage:" in l:
                    battery_info = l.split(":", 1)[1].strip()
                    break
    except Exception:
        pass

    # Check daemon service status
    daemon_active = False
    try:
        s_res = subprocess.run(["systemctl", "--user", "is-active", "voice-daemon.service"], capture_output=True, text=True, timeout=1.0)
        daemon_active = s_res.stdout.strip() == "active"
    except Exception:
        pass

    return {
        "status": "online",
        "daemon_active": daemon_active,
        "safety_tiers_enabled": config.SAFETY_TIERS_ENABLED,
        "enable_s2s_mode": config.ENABLE_S2S_MODE,
        "conversation_backend": config.CONVERSATION_BACKEND,
        "gemini_live_model": config.GEMINI_LIVE_MODEL,
        "intent_model": config.INTENT_MODEL,
        "battery": battery_info,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.post("/api/execute")
async def execute_skill_endpoint(payload: Dict[str, Any]):
    """Executes a skill directly via the dispatch chokepoint."""
    skill = payload.get("skill")
    args = payload.get("args", {})
    text = payload.get("text", f"api: {skill}")
    if not skill:
        raise HTTPException(status_code=400, detail="Missing 'skill' parameter")

    t0 = time.perf_counter()
    res = await asyncio.to_thread(dispatch.execute_skill, skill, args, text=text)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if res.get("outcome") == "needs_confirmation":
        confirm_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _pending_confirmations[confirm_id] = {
            "generator": res["generator"],
            "future": fut,
            "skill": skill,
            "args": args,
            "text": text,
            "tier": res.get("tier"),
        }
        return {
            "outcome": "needs_confirmation",
            "confirm_id": confirm_id,
            "tier": res.get("tier"),
            "pause": res.get("pause"),
            "elapsed_ms": elapsed_ms,
        }

    return {
        "outcome": res.get("outcome"),
        "tier": res.get("tier"),
        "output": res.get("output", "").strip(),
        "elapsed_ms": elapsed_ms,
    }


@app.post("/api/confirm/{confirm_id}")
async def confirm_endpoint(confirm_id: str, payload: Dict[str, Any]):
    """Resolves a pending DANGEROUS confirmation."""
    pending = _pending_confirmations.pop(confirm_id, None)
    if not pending:
        raise HTTPException(status_code=404, detail="Confirmation ID not found or expired")

    approved = payload.get("approved", False)
    edited = payload.get("edited", None)

    if not approved:
        sent_val = ""
    elif edited:
        sent_val = edited
    else:
        sent_val = None
    res = await asyncio.to_thread(
        dispatch.finish_confirm,
        pending["generator"],
        sent_val,
        name=pending["skill"],
        args=pending["args"],
        text=pending["text"],
        tier=pending["tier"],
    )
    return res


@app.websocket("/ws/chat")
async def websocket_chat_endpoint(websocket: WebSocket):
    """Full-duplex WebSocket endpoint for text chat, voice audio, and interactive tool gating."""
    await websocket.accept()
    client_id = str(uuid.uuid4())[:8]
    loop = asyncio.get_running_loop()
    from audio_bus import audio_bus

    def _on_vad_barge_in():
        try:
            asyncio.run_coroutine_threadsafe(websocket.send_json({"type": "audio_flush"}), loop)
        except Exception:
            pass

    audio_bus.on_barge_in(_on_vad_barge_in)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "chat")

            # -------------------------------------------------------------
            # Case 1: Resolving a DANGEROUS confirmation from the Web UI modal
            # -------------------------------------------------------------
            if msg_type == "confirm_response":
                confirm_id = data.get("confirm_id")
                approved = data.get("approved", False)
                edited = data.get("edited", None)

                pending = _pending_confirmations.pop(confirm_id, None)
                if pending:
                    if not approved:
                        sent_val = ""
                    elif edited:
                        sent_val = edited
                    else:
                        sent_val = None
                    t0 = time.perf_counter()
                    res = await asyncio.to_thread(
                        dispatch.finish_confirm,
                        pending["generator"],
                        sent_val,
                        name=pending["skill"],
                        args=pending["args"],
                        text=pending["text"],
                        tier=pending["tier"],
                    )
                    elapsed_ms = (time.perf_counter() - t0) * 1000
                    await websocket.send_json({
                        "type": "tool_executed",
                        "skill": pending["skill"],
                        "tier": pending["tier"],
                        "outcome": res.get("outcome"),
                        "output": res.get("output", "").strip() or ("Executed" if approved else "Cancelled"),
                        "elapsed_ms": elapsed_ms,
                    })
                continue

            # -------------------------------------------------------------
            # Case 2: Audio blob from client microphone
            # -------------------------------------------------------------
            user_text = ""
            if msg_type == "audio":
                audio_b64 = data.get("audio_data", "")
                if audio_b64:
                    raw_audio = base64.b64decode(audio_b64)
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                        tf.write(raw_audio)
                        tf_path = tf.name
                    try:
                        user_text = await asyncio.to_thread(stt.transcribe, tf_path)
                    except Exception as e:
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Transcription error: {e}",
                        })
                        continue
                    finally:
                        if os.path.exists(tf_path):
                            os.unlink(tf_path)

                    await websocket.send_json({
                        "type": "transcription",
                        "text": user_text,
                    })
            else:
                if isinstance(data, dict):
                    if data.get("type") == "barge_in":
                        from audio_bus import audio_bus
                        audio_bus.trigger_barge_in()
                        continue
                    if data.get("type") == "s2s_toggle":
                        s2s_active = bool(data.get("enabled", False))
                        await websocket.send_json({
                            "type": "s2s_status",
                            "enabled": s2s_active,
                            "backend": config.CONVERSATION_BACKEND,
                        })
                        continue
                user_text = data.get("message", "").strip()

            if not user_text:
                continue

            t_start = time.perf_counter()

            # -------------------------------------------------------------
            # Completeness Check & Trailing Intent Extension
            # -------------------------------------------------------------
            if llm_intent.is_trailing_utterance(user_text):
                prompt_msg = "Is that all, or is there anything else?"
                comp_info = llm_intent.check_intent_completeness(user_text)
                base_clean = comp_info["clean_text"]

                await websocket.send_json({
                    "type": "trailing_prompt",
                    "prompt": prompt_msg,
                    "original_text": user_text,
                    "clean_text": base_clean,
                    "extension_timeout": 3.0,
                })

                # Stream audio of the trailing prompt via Fast-Path Piper TTS
                await _stream_tts_audio(websocket, prompt_msg, skill_name="trailing_prompt")

                # Keep listening window open for a 3s extension
                follow_up_text = ""
                try:
                    fu_data = await asyncio.wait_for(websocket.receive_json(), timeout=3.0)
                    fu_type = fu_data.get("type")
                    if fu_type == "audio":
                        fu_b64 = fu_data.get("audio_data", "")
                        if fu_b64:
                            raw_fu = base64.b64decode(fu_b64)
                            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf_fu:
                                tf_fu.write(raw_fu)
                                tf_fu_path = tf_fu.name
                            try:
                                follow_up_text = await asyncio.to_thread(stt.transcribe, tf_fu_path)
                            finally:
                                if os.path.exists(tf_fu_path):
                                    os.unlink(tf_fu_path)
                            if follow_up_text:
                                await websocket.send_json({
                                    "type": "transcription",
                                    "text": follow_up_text,
                                })
                    elif fu_type == "chat":
                        follow_up_text = fu_data.get("message", "").strip()
                    elif fu_type == "barge_in":
                        from audio_bus import audio_bus
                        audio_bus.trigger_barge_in()
                except asyncio.TimeoutError:
                    follow_up_text = ""

                if follow_up_text:
                    cleaned_fu = follow_up_text.strip().lower().rstrip(".?!")
                    if cleaned_fu in {"no", "no that's all", "that's all", "thats all", "nothing else", "that is all", "nope", "no thank you", "no thanks", "nothing", "all"}:
                        user_text = base_clean
                    else:
                        user_text = f"{base_clean} {follow_up_text}".strip()
                else:
                    user_text = base_clean

                if not user_text:
                    await websocket.send_json({
                        "type": "chat_message",
                        "text": "I didn't catch that. Please tell me what you would like to do.",
                        "elapsed_ms": (time.perf_counter() - t_start) * 1000,
                    })
                    continue

            # Deterministic Compound Command Pre-Processor
            sub_commands = split_compound_utterance(user_text)
            if len(sub_commands) > 1:
                outputs = []
                for sub_cmd in sub_commands:
                    t_cmd_start = time.perf_counter()
                    sub_layer = None
                    sub_skill = None
                    sub_args = {}

                    # Step 1: Layer 0 Deterministic Grammar Match
                    n0, a0 = skills.match_skill(sub_cmd)
                    if n0 is not None:
                        sub_layer = "layer0"
                        sub_skill, sub_args = n0, a0

                    # Step 2: Layer 1 Semantic Vector Match
                    if sub_skill is None:
                        try:
                            n1, a1 = await asyncio.to_thread(semantic_match.match, sub_cmd)
                            if n1 is not None:
                                sub_layer = "layer1"
                                sub_skill, sub_args = n1, a1
                        except Exception:
                            pass

                    # Step 3: Wikipedia Fast-Path Lookup
                    if sub_skill is None:
                        try:
                            wiki_hit = await asyncio.to_thread(wiki_fastpath.lookup, sub_cmd)
                            if wiki_hit is not None:
                                sub_layer = "wiki"
                                q, a, u = wiki_hit
                                sub_skill = "answer_question"
                                sub_args = {"question": q, "answer": a, "url": u}
                        except Exception:
                            pass

                    # Step 4: Layer 2 LLM Intent Fallback (Cascade)
                    if sub_skill is None and config.LLM_FALLBACK_ENABLED:
                        try:
                            nl, al = await asyncio.to_thread(llm_intent.resolve_intent, sub_cmd)
                            if nl is not None:
                                sub_layer = "llm"
                                sub_skill, sub_args = nl, al
                        except Exception:
                            pass

                    if sub_skill is not None:
                        tier = _tiers.tier_of(sub_skill)
                        res = await asyncio.to_thread(
                            dispatch.execute_skill,
                            sub_skill,
                            sub_args,
                            text=sub_cmd,
                        )
                        cmd_elapsed_ms = (time.perf_counter() - t_cmd_start) * 1000
                        out_text = res.get("output", "").strip() or f"Executed {sub_skill}."
                        await websocket.send_json({
                            "type": "tool_executed",
                            "skill": sub_skill,
                            "args": sub_args,
                            "tier": tier,
                            "matched_layer": sub_layer,
                            "outcome": "executed",
                            "output": out_text,
                            "elapsed_ms": cmd_elapsed_ms,
                        })
                        outputs.append(out_text)
                    else:
                        outputs.append(f"No action for '{sub_cmd}'")

                if outputs:
                    consolidated = "; ".join(outputs)
                    await websocket.send_json({
                        "type": "chat_message",
                        "text": consolidated,
                        "elapsed_ms": (time.perf_counter() - t_start) * 1000,
                    })
                    asyncio.create_task(_stream_tts_audio(websocket, consolidated))
                continue

            matched_layer = None
            skill_name = None
            skill_args = {}

            # -------------------------------------------------------------
            # Step 1: Layer 0 Deterministic Grammar Match (~0.013ms)
            # -------------------------------------------------------------
            name0, args0 = skills.match_skill(user_text)
            if name0 is not None:
                matched_layer = "layer0"
                skill_name, skill_args = name0, args0

            # -------------------------------------------------------------
            # Step 2: Layer 1 Semantic Vector Match (~10.7ms)
            # -------------------------------------------------------------
            if skill_name is None:
                try:
                    name1, args1 = await asyncio.to_thread(semantic_match.match, user_text)
                    if name1 is not None:
                        matched_layer = "layer1"
                        skill_name, skill_args = name1, args1
                except Exception:
                    pass

            # -------------------------------------------------------------
            # Step 3: Wikipedia Fast-Path Lookup
            # -------------------------------------------------------------
            if skill_name is None:
                try:
                    wiki_hit = await asyncio.to_thread(wiki_fastpath.lookup, user_text)
                    if wiki_hit is not None:
                        matched_layer = "wiki"
                        q, a, u = wiki_hit
                        skill_name = "answer_question"
                        skill_args = {"question": q, "answer": a, "url": u}
                except Exception:
                    pass

            # -------------------------------------------------------------
            # Step 4: Layer 2 LLM Intent Fallback (Gemini -> Local Qwen)
            # -------------------------------------------------------------
            if skill_name is None and config.LLM_FALLBACK_ENABLED:
                try:
                    name_llm, args_llm = await asyncio.to_thread(llm_intent.resolve_intent, user_text)
                    if name_llm is not None:
                        matched_layer = "llm"
                        skill_name, skill_args = name_llm, args_llm
                except Exception:
                    pass

            # -------------------------------------------------------------
            # Execution Dispatch via Single Chokepoint
            # -------------------------------------------------------------
            if skill_name is not None:
                tier = _tiers.tier_of(skill_name)
                res = await asyncio.to_thread(
                    dispatch.execute_skill,
                    skill_name,
                    skill_args,
                    text=user_text,
                )
                elapsed_ms = (time.perf_counter() - t_start) * 1000

                # If DANGEROUS and intercepted by Policy Engine:
                if res.get("outcome") == "needs_confirmation":
                    confirm_id = str(uuid.uuid4())
                    _pending_confirmations[confirm_id] = {
                        "generator": res["generator"],
                        "skill": skill_name,
                        "args": skill_args,
                        "text": user_text,
                        "tier": tier,
                    }
                    await websocket.send_json({
                        "type": "needs_confirmation",
                        "confirm_id": confirm_id,
                        "skill": skill_name,
                        "args": skill_args,
                        "tier": tier,
                        "preview": res["pause"]["preview"],
                        "warnings": res["pause"]["warnings"],
                        "editable": res["pause"]["editable"],
                        "matched_layer": matched_layer,
                        "elapsed_ms": elapsed_ms,
                    })
                    continue

                output_text = res.get("output", "").strip() or f"Executed {skill_name}."
                await websocket.send_json({
                    "type": "tool_executed",
                    "skill": skill_name,
                    "args": skill_args,
                    "tier": tier,
                    "matched_layer": matched_layer,
                    "outcome": "executed",
                    "output": output_text,
                    "elapsed_ms": elapsed_ms,
                })
                # Stream PCM audio chunks to WebSocket client in real-time
                asyncio.create_task(_stream_tts_audio(websocket, output_text, skill_name=skill_name))
                continue

            # -------------------------------------------------------------
            # Unmatched Conversational Chat Response (Streaming)
            # -------------------------------------------------------------
            await websocket.send_json({
                "type": "chat_stream_start",
                "matched_layer": "none",
            })

            # Stream response via Gemini text generation
            try:
                from google import genai
                client = genai.Client(api_key=config.GEMINI_API_KEY)
                accumulated_text = ""
                clause_buffer = ""

                def _stream_gemini():
                    return client.models.generate_content_stream(
                        model=config.INTENT_MODEL,
                        contents=[llm_intent.SPOKEN_PERSONA, user_text],
                    )

                stream = await asyncio.to_thread(_stream_gemini)
                for chunk in stream:
                    delta = chunk.text or ""
                    if not delta:
                        continue
                    accumulated_text += delta
                    clause_buffer += delta

                    await websocket.send_json({
                        "type": "chat_chunk",
                        "delta": delta,
                        "text": accumulated_text,
                    })

                    # Check for completed clauses to start audio rendering early
                    if tts:
                        clauses = tts.split_into_clauses(clause_buffer)
                        if len(clauses) > 1:
                            ready_clause = clauses[0]
                            clause_buffer = " ".join(clauses[1:])
                            asyncio.create_task(_stream_tts_audio(websocket, ready_clause, skill_name="answer_question"))

                if clause_buffer.strip() and tts:
                    asyncio.create_task(_stream_tts_audio(websocket, clause_buffer.strip(), skill_name="answer_question"))

                await websocket.send_json({
                    "type": "chat_message",
                    "text": accumulated_text or "I did not understand that request.",
                    "elapsed_ms": (time.perf_counter() - t_start) * 1000,
                })
            except Exception as exc:
                await websocket.send_json({
                    "type": "chat_message",
                    "text": f"Could not process request: {exc}",
                    "elapsed_ms": (time.perf_counter() - t_start) * 1000,
                })

    except WebSocketDisconnect:
        pass
    finally:
        audio_bus.remove_barge_in(_on_vad_barge_in)


@app.websocket("/ws/audio")
async def websocket_audio_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time PCM audio streaming."""
    await websocket.accept()
    try:
        while True:
            # Receive raw binary PCM bytes or JSON audio envelope
            message = await websocket.receive()
            if "bytes" in message and message["bytes"]:
                pcm_bytes = message["bytes"]
                # In Phase 4, PCM chunks stream directly into the live conversation session.
                # Echo/ack or forward chunk:
                await websocket.send_json({"type": "audio_ack", "bytes_received": len(pcm_bytes)})
            elif "text" in message and message["text"]:
                data = json.loads(message["text"])
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    print("Starting Voice Standalone Web Gateway on http://localhost:8000 ...")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
