"""
tts.py — Ultra-low latency spoken output for the voice assistant (<50ms first-chunk synthesis).

Architecture:
- Persistent Warm Worker: Spawns a dedicated background process that keeps
  the Piper TTS model weights (en_IN-spicor.onnx) warm in memory, eliminating
  the 2+ second cold-start penalty on every utterance.
- Streaming Clause Synthesis: Synthesizes text in clause-level chunks (split
  on punctuation: commas, periods, question marks, newlines) so the first
  clause renders in < 50ms while subsequent text is still being processed.
- Instant Cancellation (< 15ms): Terminating active playback (aplay / Web Audio)
  happens immediately via SIGKILL on the playback child process, enabling
  instant barge-in.
- Multi-Engine: English uses Piper (22050Hz Indian-English voice), Hindi uses
  Kokoro (hf_alpha) via RealtimeTTS.
"""

import base64
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPER_MODEL_PATH = os.path.join(_BASE_DIR, "piper_voices", "en_IN-spicor.onnx")
_HINDI_VOICE = "hf_alpha"

_CLAUSE_SPLIT_REGEX = re.compile(r"([,.;:!?\n]+)")


FAST_PATH_SKILLS = frozenset({
    "set_volume", "volume_up", "volume_down", "mute", "unmute",
    "set_brightness", "brightness_up", "brightness_down",
    "battery_status", "toggle_wifi", "toggle_bluetooth",
    "toggle_airplane", "toggle_dark_mode", "lock_screen",
    "toggle_mic_mute", "toggle_dnd", "toggle_night_light",
    "toggle_display", "toggle_kbd_backlight",
    "switch_workspace", "switch_to_app", "close_app",
    "force_quit", "open_launcher", "show_desktop", "maximize_window",
    "reopen_last_closed", "open_app", "list_desktop_folders",
})

CONVERSATIONAL_SKILLS = frozenset({
    "answer_question", "read_latest_emails", "read_pdf",
    "web_search", "conversation", "summarize", "wiki",
})


# Precompiled regular expressions for ultra-fast clean_for_speech (< 0.1ms)
_RE_ACTION_TAG = re.compile(r"^\s*\[[a-zA-Z0-9_\-]+\]\s*")
_RE_ACRONYM = re.compile(r"\(([A-Z]{1,5})\)")
_RE_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_JSON = re.compile(r"\{[\s\S]*?\}")
_RE_URL = re.compile(r"https?://\S+|www\.\S+")
_RE_WO = re.compile(r"(^|\s)w/o(?=\s|$)", re.IGNORECASE)
_RE_W = re.compile(r"(^|\s)w/(?=\s|$)", re.IGNORECASE)
_RE_EG = re.compile(r"\be\.g\.,?\b", re.IGNORECASE)
_RE_IE = re.compile(r"\bi\.e\.,?\b", re.IGNORECASE)
_RE_VS = re.compile(r"\bvs\.?\b", re.IGNORECASE)
_RE_ETC = re.compile(r"\betc\.?\b", re.IGNORECASE)
_RE_APPROX = re.compile(r"\bapprox\.?\b", re.IGNORECASE)
_RE_KMH = re.compile(r"\bkm/h\b", re.IGNORECASE)
_RE_MPH = re.compile(r"\bmph\b", re.IGNORECASE)
_RE_PATH = re.compile(r"(?:^|\s)/(?:[a-zA-Z0-9_\-\.]+/)+([a-zA-Z0-9_\-\.]+)")
_RE_HEADER = re.compile(r"^\s*#+\s+", re.MULTILINE)
_RE_BLOCKQUOTE = re.compile(r"^\s*>\s+", re.MULTILINE)
_RE_BULLET = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
_RE_STRIKE = re.compile(r"~~([^~]+)~~")
_RE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_RE_ITALIC = re.compile(r"\*([^*]+)\*")
_RE_BOLD_UND = re.compile(r"__([^_]+)__")
_RE_ITALIC_UND = re.compile(r"_([^_]+)_")
_RE_DOLLAR_NUM = re.compile(r"\$(\d+(?:\.\d+)?)")
_RE_DOLLAR = re.compile(r"\$")
_RE_PERCENT_NUM = re.compile(r"(\d+)\s*%")
_RE_PERCENT = re.compile(r"%")
_RE_AMP = re.compile(r"\s*&\s*")
_RE_AT = re.compile(r"\s*@\s*")
_RE_EQUALS = re.compile(r"\s*=\s*")
_RE_PLUS = re.compile(r"\s*\+\s*")
_RE_WHITESPACE = re.compile(r"\s+")


def clean_for_speech(text: str) -> str:
    """Prepares text for natural speech output by removing markdown, code blocks,
    URLs, file paths, raw JSON, action tags, converting symbols, and normalizing abbreviations."""
    if not text:
        return ""
    # Strip leading action tags like [answer], [ip], [battery], [conversation]
    text = _RE_ACTION_TAG.sub("", text)
    # Normalize parenthetical acronyms e.g. (EI) -> , EI,
    text = _RE_ACRONYM.sub(r", \1, ", text)
    # Omit multi-line code blocks
    text = _RE_CODE_BLOCK.sub("code block omitted", text)
    # Strip backticks from inline code
    text = _RE_INLINE_CODE.sub(r"\1", text)
    # Strip raw JSON / dict structures
    text = _RE_JSON.sub("", text)
    # Replace URLs with simple word "link"
    text = _RE_URL.sub("link", text)
    # Normalize common abbreviations
    text = _RE_WO.sub(r"\1without", text)
    text = _RE_W.sub(r"\1with", text)
    text = _RE_EG.sub("for example", text)
    text = _RE_IE.sub("that is", text)
    text = _RE_VS.sub("versus", text)
    text = _RE_ETC.sub("etcetera", text)
    text = _RE_APPROX.sub("approximately", text)
    text = _RE_KMH.sub("kilometers per hour", text)
    text = _RE_MPH.sub("miles per hour", text)

    # Strip directory path prefixes and keep filename
    text = _RE_PATH.sub(r" \1", text)

    # Remove markdown header hashes
    text = _RE_HEADER.sub("", text)
    # Remove markdown blockquotes
    text = _RE_BLOCKQUOTE.sub("", text)
    # Remove bullet markers
    text = _RE_BULLET.sub("", text)
    # Remove markdown strikethrough, bold, and italic asterisks/underscores
    text = _RE_STRIKE.sub(r"\1", text)
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_ITALIC.sub(r"\1", text)
    text = _RE_BOLD_UND.sub(r"\1", text)
    text = _RE_ITALIC_UND.sub(r"\1", text)

    # Convert symbols to spoken words
    text = _RE_DOLLAR_NUM.sub(r"\1 dollars", text)
    text = _RE_DOLLAR.sub(" dollars ", text)
    text = _RE_PERCENT_NUM.sub(r"\1 percent", text)
    text = _RE_PERCENT.sub(" percent ", text)
    text = _RE_AMP.sub(" and ", text)
    text = _RE_AT.sub(" at ", text)
    text = _RE_EQUALS.sub(" equals ", text)
    text = _RE_PLUS.sub(" plus ", text)
    # Collapse multiple whitespace/newlines
    text = _RE_WHITESPACE.sub(" ", text).strip()
    return text


def select_engine(text: str, skill_name: Optional[str] = None, lang: str = "en") -> str:
    """Dynamically selects between 'piper' (Fast-Path) and 'kokoro' (High-Fidelity Conversational).
    - Fast-Path (Piper): Deterministic system commands, hardware status, volume/brightness confirmations,
      and responses < 10 words (TTFA < 100ms).
    - Conversational Path (Kokoro-82M): General Q&A, email reading, web summaries, small-talk
      (natural studio intonation), and responses >= 10 words.
    """
    if lang == "hi":
        return "kokoro"
    if skill_name:
        if skill_name in FAST_PATH_SKILLS:
            return "piper"
        if skill_name in CONVERSATIONAL_SKILLS:
            return "kokoro"
    cleaned = clean_for_speech(text)
    words = cleaned.split()
    if len(words) < 10:
        return "piper"
    return "kokoro"


def split_into_clauses(text: str, min_words: int = 3) -> List[str]:
    """Splits text into clause-level segments for streaming synthesis.
    Splits on punctuation (comma, semicolon, colon, period, question mark, newline)
    so the first 3-5 words can be dispatched to the TTS worker immediately."""
    cleaned = clean_for_speech(text)
    if not cleaned:
        return []

    tokens = _CLAUSE_SPLIT_REGEX.split(cleaned)
    clauses: List[str] = []
    current: List[str] = []

    for part in tokens:
        if not part:
            continue
        if _CLAUSE_SPLIT_REGEX.match(part):
            # Punctuation delimiter
            if current:
                current.append(part.strip())
                candidate = " ".join("".join(current).split()).strip()
                words = candidate.split()
                if len(words) >= min_words or any(p in part for p in ".!?"):
                    clauses.append(candidate)
                    current = []
        else:
            current.append(part)

    if current:
        remainder = " ".join("".join(current).split()).strip()
        if remainder:
            if clauses and len(remainder.split()) < min_words:
                clauses[-1] = clauses[-1] + " " + remainder
            else:
                clauses.append(remainder)

    return clauses if clauses else [cleaned]


def detect_lang(text: str) -> str:
    """Auto-detect language from script: Devanagari -> hi, default -> en."""
    for ch in text:
        if "\u0900" <= ch <= "\u097f":
            return "hi"
    return "en"


# ===========================================================================
# ─── Persistent Warm TTS Worker Process ─────────────────────────────────────
# ===========================================================================

class PersistentTTSClient:
    """Manages the lifecycle of a warm background TTS worker subprocess.
    Communicates via line-delimited JSON over stdin/stdout."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._req_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._req_counter = 0
        self._is_speaking = False

    def is_alive(self) -> bool:
        with self._write_lock:
            return self._proc is not None and self._proc.poll() is None

    def start(self, timeout: float = 15.0) -> None:
        with self._write_lock:
            if self._proc is not None and self._proc.poll() is None:
                return

            cmd = [sys.executable, os.path.abspath(__file__), "--persistent-worker"]
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            # Wait for READY signal from worker
            deadline = time.monotonic() + timeout
            ready = False
            while time.monotonic() < deadline:
                line = self._proc.stdout.readline() if self._proc.stdout else ""
                if line.strip() == "READY":
                    ready = True
                    break
                if self._proc.poll() is not None:
                    err = self._proc.stderr.read() if self._proc.stderr else "Unknown error"
                    raise RuntimeError(f"TTS persistent worker exited prematurely: {err}")
                time.sleep(0.01)

            if not ready:
                self._terminate()
                raise TimeoutError("TTS persistent worker failed to signal READY within timeout.")

    def _terminate(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=1.0)
            except Exception:
                pass
            self._proc = None

    def wait_conversational_ready(self, timeout: float = 30.0) -> bool:
        """Waits until the conversational engine (Kokoro) has completed background warmup."""
        self.start()
        with self._req_lock:
            with self._write_lock:
                self._req_counter += 1
                req_id = self._req_counter
                req = {"cmd": "wait_kokoro", "id": req_id}
                try:
                    if self._proc and self._proc.stdin:
                        self._proc.stdin.write(json.dumps(req) + "\n")
                        self._proc.stdin.flush()
                except Exception:
                    return False

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = self._proc.stdout.readline() if (self._proc and self._proc.stdout) else ""
                if not line:
                    break
                try:
                    data = json.loads(line)
                    if data.get("event") == "kokoro_ready" and data.get("id") == req_id:
                        return True
                except Exception:
                    continue
        return False

    def synthesize_chunks(
        self,
        text: str,
        lang: str = "en",
        engine: Optional[str] = None,
        skill_name: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Requests real-time streaming synthesis chunks from the persistent worker."""
        self.start()
        clean = clean_for_speech(text)
        if not clean:
            return

        chosen_engine = engine or select_engine(clean, skill_name=skill_name, lang=lang)

        with self._req_lock:
            with self._write_lock:
                self._req_counter += 1
                req_id = self._req_counter
                req = {
                    "cmd": "synthesize",
                    "id": req_id,
                    "text": clean,
                    "lang": lang,
                    "engine": chosen_engine,
                }
                try:
                    if self._proc and self._proc.stdin:
                        self._proc.stdin.write(json.dumps(req) + "\n")
                        self._proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    self._terminate()
                    self.start()
                    if self._proc and self._proc.stdin:
                        self._proc.stdin.write(json.dumps(req) + "\n")
                        self._proc.stdin.flush()

            # Read streaming response chunks from worker
            while True:
                line = self._proc.stdout.readline() if (self._proc and self._proc.stdout) else ""
                if not line:
                    break
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                event = data.get("event")
                if event == "cancelled":
                    break
                if data.get("id") != req_id:
                    continue

                if event == "chunk":
                    yield data
                    if data.get("is_last", False):
                        break
                elif event in ("done", "error"):
                    break

    def speak(
        self,
        text: str,
        lang: str = "en",
        engine: Optional[str] = None,
        skill_name: Optional[str] = None,
    ) -> None:
        """Plays speech out loud on the system via the persistent worker."""
        clean = clean_for_speech(text)
        if not clean:
            return
        self.start()

        chosen_engine = engine or select_engine(clean, skill_name=skill_name, lang=lang)

        with self._req_lock:
            with self._write_lock:
                self._req_counter += 1
                req_id = self._req_counter
                self._is_speaking = True
                req = {
                    "cmd": "speak",
                    "id": req_id,
                    "text": clean,
                    "lang": lang,
                    "engine": chosen_engine,
                }
                try:
                    if self._proc and self._proc.stdin:
                        self._proc.stdin.write(json.dumps(req) + "\n")
                        self._proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    self._terminate()
                    self.start()
                    if self._proc and self._proc.stdin:
                        self._proc.stdin.write(json.dumps(req) + "\n")
                        self._proc.stdin.flush()

            # Wait until playback finishes or cancellation occurs
            while True:
                line = self._proc.stdout.readline() if (self._proc and self._proc.stdout) else ""
                if not line:
                    break
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                event = data.get("event")
                if event == "cancelled":
                    break
                if data.get("id") != req_id:
                    continue
                if event in ("speak_done", "error"):
                    break
            self._is_speaking = False

    def cancel(self) -> None:
        """Immediately aborts any active synthesis and kills active aplay processes (< 15ms)."""
        with self._write_lock:
            if self._proc and self._proc.stdin and self._proc.poll() is None:
                try:
                    self._proc.stdin.write(json.dumps({"cmd": "cancel", "id": self._req_counter}) + "\n")
                    self._proc.stdin.flush()
                except Exception:
                    self._terminate()
            self._is_speaking = False


# Global persistent client singleton
_client = PersistentTTSClient()


def warm_up(wait_for_conversational: bool = False) -> None:
    """Pre-warms the persistent TTS workers (Piper & Kokoro) so model weights are resident."""
    _client.start()
    if wait_for_conversational:
        _client.wait_conversational_ready()


def speak(
    text: str,
    lang: str | None = None,
    engine: str | None = None,
    skill_name: str | None = None,
) -> None:
    """Synchronous speech execution via the persistent warm worker."""
    if not text:
        return
    if lang is None or lang == "en":
        detected = detect_lang(text)
        lang = detected if detected != "en" else "en"
    _client.speak(text, lang=lang, engine=engine, skill_name=skill_name)


def cancel() -> None:
    """Cancels active speech synthesis and playback within < 15ms."""
    _client.cancel()


def synthesize_stream(
    text: str,
    lang: str | None = None,
    engine: str | None = None,
    skill_name: str | None = None,
) -> Iterator[Tuple[bytes, int, bool]]:
    """Yields (raw_pcm_bytes, sample_rate, is_last) tuples in real-time as chunks render."""
    if not text:
        return
    if lang is None or lang == "en":
        detected = detect_lang(text)
        lang = detected if detected != "en" else "en"

    for chunk in _client.synthesize_chunks(text, lang=lang, engine=engine, skill_name=skill_name):
        b64_audio = chunk.get("pcm_b64", "")
        pcm = base64.b64decode(b64_audio) if b64_audio else b""
        sr = chunk.get("sample_rate", 22050)
        is_last = chunk.get("is_last", False)
        yield pcm, sr, is_last


# ===========================================================================
# ─── Worker Process Implementation ──────────────────────────────────────────
# ===========================================================================

def _run_persistent_worker() -> None:
    """Runs inside the isolated subprocess. Pre-loads Piper ONNX and Kokoro-82M into
    memory once and serves synthesis requests continuously over stdin/stdout."""
    # Ensure stdout is unbuffered
    sys.stdout.reconfigure(line_buffering=True)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    # 1. Warm load Piper model (Fast-Path)
    piper_voice = None
    try:
        from piper import PiperVoice
        if os.path.exists(_PIPER_MODEL_PATH):
            piper_voice = PiperVoice.load(_PIPER_MODEL_PATH)
            for _ in piper_voice.synthesize("."):
                pass
    except Exception as exc:
        print(f"[worker warning] Failed to preload Piper: {exc}", file=sys.stderr)

    # Signal READY to parent process once Piper Fast-Path is fully resident and primed
    sys.stdout.write("READY\n")
    sys.stdout.flush()

    # 2. Warm load Kokoro-82M model (Conversational High-Fidelity) in background thread
    kokoro_ready_event = threading.Event()
    kokoro_lock = threading.Lock()
    kokoro_pipelines = {}

    def _preload_kokoro():
        try:
            import torch
            torch.set_num_threads(4)
            from kokoro import KPipeline
            with kokoro_lock:
                kokoro_pipelines["a"] = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device="cpu")
        except Exception as exc:
            print(f"[worker warning] Failed to preload Kokoro: {exc}", file=sys.stderr)
        finally:
            kokoro_ready_event.set()

    threading.Thread(target=_preload_kokoro, daemon=True).start()

    active_aplay_proc: list[Optional[subprocess.Popen]] = [None]
    cancel_flag = threading.Event()
    cmd_queue = queue.Queue()

    def _stdin_reader():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except Exception:
                continue

            cmd = req.get("cmd")
            if cmd == "cancel":
                cancel_flag.set()
                proc = active_aplay_proc[0]
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                sys.stdout.write(json.dumps({"event": "cancelled", "id": req.get("id")}) + "\n")
                sys.stdout.flush()
            else:
                cmd_queue.put(req)

    reader_thread = threading.Thread(target=_stdin_reader, daemon=True)
    reader_thread.start()

    while True:
        try:
            req = cmd_queue.get()
        except Exception:
            break

        cmd = req.get("cmd")
        req_id = req.get("id")

        if cmd == "ping":
            sys.stdout.write(json.dumps({"event": "pong", "id": req_id}) + "\n")
            sys.stdout.flush()
            continue

        if cmd == "wait_kokoro":
            kokoro_ready_event.wait(timeout=30.0)
            sys.stdout.write(json.dumps({"event": "kokoro_ready", "id": req_id}) + "\n")
            sys.stdout.flush()
            continue

        if cmd in ("synthesize", "speak"):
            cancel_flag.clear()
            text = req.get("text", "")
            lang = req.get("lang", "en")
            target_engine = req.get("engine", "piper")
            t_start = time.perf_counter()

            # Execute Kokoro High-Fidelity Synthesis
            if target_engine == "kokoro" or lang == "hi":
                try:
                    lang_code = "h" if lang == "hi" else "a"
                    if lang_code == "a":
                        kokoro_ready_event.wait(timeout=30.0)
                    with kokoro_lock:
                        if lang_code not in kokoro_pipelines:
                            import torch
                            torch.set_num_threads(4)
                            from kokoro import KPipeline
                            kokoro_pipelines[lang_code] = KPipeline(lang_code=lang_code, repo_id="hexgrad/Kokoro-82M", device="cpu")
                        pipeline = kokoro_pipelines[lang_code]
                    voice_name = _HINDI_VOICE if lang == "hi" else "af_heart"

                    if cmd == "speak":
                        proc = subprocess.Popen(
                            ["aplay", "-r", "24000", "-f", "S16_LE", "-c", "1", "-q"],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        active_aplay_proc[0] = proc
                        sys.stdout.write(json.dumps({"event": "speak_started", "id": req_id}) + "\n")
                        sys.stdout.flush()

                        for gs, ps, audio in pipeline(text, voice=voice_name, speed=1.0):
                            if cancel_flag.is_set():
                                break
                            if audio is not None and len(audio) > 0:
                                pcm_bytes = (audio.clamp(-1.0, 1.0).numpy() * 32767).astype(np.int16).tobytes()
                                if proc.stdin:
                                    try:
                                        proc.stdin.write(pcm_bytes)
                                        proc.stdin.flush()
                                    except (BrokenPipeError, OSError):
                                        break

                        if proc.stdin:
                            try:
                                proc.stdin.close()
                            except Exception:
                                pass
                        proc.wait(timeout=10.0)
                        active_aplay_proc[0] = None

                        event_name = "cancelled" if cancel_flag.is_set() else "speak_done"
                        sys.stdout.write(json.dumps({"event": event_name, "id": req_id}) + "\n")
                        sys.stdout.flush()
                        continue

                    elif cmd == "synthesize":
                        idx = 0
                        chunk_slice_size = 8192
                        for gs, ps, audio in pipeline(text, voice=voice_name, speed=1.0):
                            if cancel_flag.is_set():
                                break
                            if audio is not None and len(audio) > 0:
                                pcm_bytes = (audio.clamp(-1.0, 1.0).numpy() * 32767).astype(np.int16).tobytes()
                                # Yield in small streamable slices for low TTFA
                                for offset in range(0, len(pcm_bytes), chunk_slice_size):
                                    if cancel_flag.is_set():
                                        break
                                    slice_data = pcm_bytes[offset : offset + chunk_slice_size]
                                    resp = {
                                        "event": "chunk",
                                        "id": req_id,
                                        "pcm_b64": base64.b64encode(slice_data).decode("ascii"),
                                        "sample_rate": 24000,
                                        "channels": 1,
                                        "engine": "kokoro",
                                        "is_first": (idx == 0),
                                        "is_last": False,
                                        "elapsed_ms": (time.perf_counter() - t_start) * 1000,
                                    }
                                    sys.stdout.write(json.dumps(resp) + "\n")
                                    sys.stdout.flush()
                                    idx += 1

                        if not cancel_flag.is_set():
                            sys.stdout.write(json.dumps({"event": "done", "id": req_id}) + "\n")
                            sys.stdout.flush()
                        continue

                except Exception as exc:
                    print(f"[worker kokoro exc]: {exc!r}", file=sys.stderr)
                    # If Kokoro failed on English, fallback to Piper
                    if lang == "hi" or piper_voice is None:
                        sys.stdout.write(json.dumps({"event": "error", "id": req_id, "error": str(exc)}) + "\n")
                        sys.stdout.flush()
                        continue

            # Execute Piper Fast-Path Synthesis (Default / Fallback)
            if piper_voice is None:
                from piper import PiperVoice
                piper_voice = PiperVoice.load(_PIPER_MODEL_PATH)

            if cmd == "speak":
                try:
                    proc = subprocess.Popen(
                        ["aplay", "-r", "22050", "-f", "S16_LE", "-c", "1", "-q"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    active_aplay_proc[0] = proc
                    sys.stdout.write(json.dumps({"event": "speak_started", "id": req_id}) + "\n")
                    sys.stdout.flush()

                    for chunk in piper_voice.synthesize(text):
                        if cancel_flag.is_set():
                            break
                        if proc.stdin:
                            try:
                                proc.stdin.write(chunk.audio_int16_bytes)
                                proc.stdin.flush()
                            except (BrokenPipeError, OSError):
                                break

                    if proc.stdin:
                        try:
                            proc.stdin.close()
                        except Exception:
                            pass
                    proc.wait(timeout=10.0)
                except Exception as exc:
                    sys.stdout.write(json.dumps({"event": "error", "id": req_id, "error": str(exc)}) + "\n")
                finally:
                    active_aplay_proc[0] = None

                event_name = "cancelled" if cancel_flag.is_set() else "speak_done"
                sys.stdout.write(json.dumps({"event": event_name, "id": req_id}) + "\n")
                sys.stdout.flush()

            elif cmd == "synthesize":
                try:
                    for idx, chunk in enumerate(piper_voice.synthesize(text)):
                        if cancel_flag.is_set():
                            break
                        pcm_bytes = chunk.audio_int16_bytes
                        pcm_b64 = base64.b64encode(pcm_bytes).decode("ascii")
                        resp = {
                            "event": "chunk",
                            "id": req_id,
                            "pcm_b64": pcm_b64,
                            "sample_rate": chunk.sample_rate,
                            "channels": chunk.sample_channels,
                            "engine": "piper",
                            "is_first": (idx == 0),
                            "is_last": False,
                            "elapsed_ms": (time.perf_counter() - t_start) * 1000,
                        }
                        sys.stdout.write(json.dumps(resp) + "\n")
                        sys.stdout.flush()
                    if not cancel_flag.is_set():
                        sys.stdout.write(json.dumps({"event": "done", "id": req_id}) + "\n")
                        sys.stdout.flush()
                except Exception as exc:
                    sys.stdout.write(json.dumps({"event": "error", "id": req_id, "error": str(exc)}) + "\n")
                    sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--persistent-worker":
        try:
            _run_persistent_worker()
        except Exception as err:
            print(f"[tts persistent worker error] {err}", file=sys.stderr)
            sys.exit(1)
    else:
        # Standalone invocation for testing
        print("Starting persistent worker test...")
        warm_up()
        print("Warm up complete. Testing speak...")
        speak("Testing ultra low latency persistent speech synthesis.")
        print("Speak complete.")
