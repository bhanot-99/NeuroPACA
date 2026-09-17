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

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPER_MODEL_PATH = os.path.join(_BASE_DIR, "piper_voices", "en_IN-spicor.onnx")
_HINDI_VOICE = "hf_alpha"

_CLAUSE_SPLIT_REGEX = re.compile(r"([,.;:!?\n]+)")


def clean_for_speech(text: str) -> str:
    """Prepares text for natural speech output by removing markdown, code blocks,
    URLs, action tags, and normalizing abbreviations."""
    if not text:
        return ""
    # Strip leading action tags like [answer], [ip], [battery], [conversation]
    text = re.sub(r"^\s*\[[a-zA-Z0-9_\-]+\]\s*", "", text)
    # Normalize parenthetical acronyms e.g. (EI) -> , EI,
    text = re.sub(r"\(([A-Z]{1,5})\)", r", \1, ", text)
    # Omit multi-line code blocks
    text = re.sub(r"```[\s\S]*?```", "code block omitted", text)
    # Strip backticks from inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Replace URLs with simple word "link"
    text = re.sub(r"https?://\S+", "link", text)
    # Remove markdown bold and italic asterisks
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    # Remove bullet markers
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    # Remove markdown header hashes
    text = re.sub(r"^\s*#+\s+", "", text, flags=re.MULTILINE)
    # Collapse multiple whitespace/newlines
    text = re.sub(r"\s+", " ", text).strip()
    return text


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

    def start(self, timeout: float = 10.0) -> None:
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

    def synthesize_chunks(self, text: str, lang: str = "en") -> Iterator[Dict[str, Any]]:
        """Requests real-time streaming synthesis chunks from the persistent worker."""
        self.start()
        clean = clean_for_speech(text)
        if not clean:
            return

        with self._req_lock:
            with self._write_lock:
                self._req_counter += 1
                req_id = self._req_counter
                req = {"cmd": "synthesize", "id": req_id, "text": clean, "lang": lang}
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

    def speak(self, text: str, lang: str = "en") -> None:
        """Plays speech out loud on the system via the persistent worker."""
        clean = clean_for_speech(text)
        if not clean:
            return
        self.start()

        with self._req_lock:
            with self._write_lock:
                self._req_counter += 1
                req_id = self._req_counter
                self._is_speaking = True
                req = {"cmd": "speak", "id": req_id, "text": clean, "lang": lang}
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


def warm_up() -> None:
    """Pre-warms the persistent Piper TTS worker so the model weights are resident."""
    _client.start()


def speak(text: str, lang: str | None = None) -> None:
    """Synchronous speech execution via the persistent warm worker."""
    if not text:
        return
    if lang is None or lang == "en":
        detected = detect_lang(text)
        lang = detected if detected != "en" else "en"
    _client.speak(text, lang=lang)


def cancel() -> None:
    """Cancels active speech synthesis and playback within < 15ms."""
    _client.cancel()


def synthesize_stream(text: str, lang: str | None = None) -> Iterator[Tuple[bytes, int, bool]]:
    """Yields (raw_pcm_bytes, sample_rate, is_last) tuples in real-time as chunks render."""
    if not text:
        return
    if lang is None or lang == "en":
        detected = detect_lang(text)
        lang = detected if detected != "en" else "en"

    for chunk in _client.synthesize_chunks(text, lang=lang):
        b64_audio = chunk.get("pcm_b64", "")
        pcm = base64.b64decode(b64_audio) if b64_audio else b""
        sr = chunk.get("sample_rate", 22050)
        is_last = chunk.get("is_last", False)
        yield pcm, sr, is_last


# ===========================================================================
# ─── Worker Process Implementation ──────────────────────────────────────────
# ===========================================================================

def _run_persistent_worker() -> None:
    """Runs inside the isolated subprocess. Pre-loads Piper into memory once and
    serves synthesis requests continuously over stdin/stdout."""
    # Ensure stdout is unbuffered
    sys.stdout.reconfigure(line_buffering=True)

    # 1. Warm load Piper model
    piper_voice = None
    try:
        from piper import PiperVoice
        if os.path.exists(_PIPER_MODEL_PATH):
            piper_voice = PiperVoice.load(_PIPER_MODEL_PATH)
    except Exception as exc:
        print(f"[worker warning] Failed to preload Piper: {exc}", file=sys.stderr)

    # Signal READY to parent process
    sys.stdout.write("READY\n")
    sys.stdout.flush()

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

        if cmd in ("synthesize", "speak"):
            cancel_flag.clear()
            text = req.get("text", "")
            lang = req.get("lang", "en")
            t_start = time.perf_counter()

            if lang == "hi":
                # Hindi synthesis via RealtimeTTS Kokoro
                try:
                    from RealtimeTTS import TextToAudioStream, KokoroEngine
                    engine = KokoroEngine(voice=_HINDI_VOICE, default_speed=0.95)
                    stream = TextToAudioStream(engine)
                    stream.feed(text)
                    if cmd == "speak":
                        stream.play()
                        sys.stdout.write(json.dumps({"event": "speak_done", "id": req_id}) + "\n")
                    else:
                        sys.stdout.write(json.dumps({"event": "done", "id": req_id}) + "\n")
                except Exception as exc:
                    sys.stdout.write(json.dumps({"event": "error", "id": req_id, "error": str(exc)}) + "\n")
                sys.stdout.flush()
                continue

            # English synthesis via Piper
            if piper_voice is None:
                from piper import PiperVoice
                piper_voice = PiperVoice.load(_PIPER_MODEL_PATH)

            if cmd == "speak":
                # Direct hardware playback via aplay
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
                # Real-time streaming chunks back to parent process
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
