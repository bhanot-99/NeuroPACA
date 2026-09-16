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

import config
import dispatch
import llm_intent
import skills
from skills import _session_state, _tiers, semantic_match
import stt
import wiki_fastpath

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
                user_text = data.get("message", "").strip()

            if not user_text:
                continue

            t_start = time.perf_counter()
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
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=config.INTENT_MODEL,
                    contents=user_text,
                )
                response_text = response.text or "I did not understand that request."
                await websocket.send_json({
                    "type": "chat_message",
                    "text": response_text,
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
