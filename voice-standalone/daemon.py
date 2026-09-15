"""
Always-on voice daemon, controlled by the tray icon (tray.py) over a FIFO —
NOT a terminal control surface (see the "no terminal control surfaces"
preference). main.py's input()-gated loop remains for manual/dev testing
only; this is the real, user-facing entry point, meant to run continuously
from login to logout via systemd --user (see systemd/voice-daemon.service).

Click 1 (tray) -> start recording. Click 2 -> stop, transcribe, resolve,
execute. Feedback is desktop notifications (notify-send) — there's no
terminal to read once this runs as a background service.
"""

import contextlib
import io
import os
import tempfile
import threading

import actions
import llm_intent
import skills
import stt
from audio_capture import record_until_stopped
from skills import _session_state, semantic_match

FIFO_PATH = os.path.expanduser("~/.local/share/voice-standalone/toggle.fifo")


def _notify(title: str, body: str) -> None:
    import subprocess
    # Truncate — a very long body (e.g. a full `ps aux` dump) makes for an
    # unreadable notification popup; the point is a quick glance, not a log.
    body = body.strip()
    if len(body) > 300:
        body = body[:300] + "…"
    subprocess.run(["notify-send", "--app-name=Voice Assistant", title, body], check=False)


def _ensure_fifo() -> None:
    os.makedirs(os.path.dirname(FIFO_PATH), exist_ok=True)
    if not os.path.exists(FIFO_PATH):
        os.mkfifo(FIFO_PATH)


def _wait_for_toggle() -> None:
    """Blocks until exactly one message arrives on the FIFO. Reopens each
    call — a FIFO reader hits EOF once its writer closes, so a fresh open
    is needed per message, not one persistent file handle."""
    with open(FIFO_PATH) as f:
        f.readline()


def main() -> None:
    _ensure_fifo()

    print("Loading local semantic matcher...")
    layer1_available = True
    try:
        semantic_match._ensure_index_built()
    except Exception as exc:
        layer1_available = False
        print(f"[warning] Layer 1 unavailable this session: {exc}")

    _notify("Voice Assistant", "Ready — click the tray icon to talk.")

    while True:
        _wait_for_toggle()

        # Asleep gates EXECUTION, not the ability to record and recognize
        # "wake up" — same design as main.py.
        _notify("Voice Assistant", "Listening...")
        stop_event = threading.Event()
        threading.Thread(target=lambda: (_wait_for_toggle(), stop_event.set()), daemon=True).start()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name

        try:
            record_until_stopped(wav_path, stop_event)
            text = stt.transcribe(wav_path)
        except Exception as exc:
            _notify("Voice Assistant — error", f"Could not transcribe that: {exc}")
            continue
        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)

        if not text:
            _notify("Voice Assistant", "(heard nothing)")
            continue

        try:
            name, args = skills.match_skill(text)
            if name is None and layer1_available:
                name, args = semantic_match.match(text)
            if name is None:
                name, args = llm_intent.resolve_intent(text)

            if name is None:
                _notify(f"Heard: {text}", "No matching action.")
                continue

            if _session_state.is_asleep() and name != "wake_back_up":
                _notify("Voice Assistant", "(asleep — say 'wake up' to resume)")
                continue

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                actions.DISPATCH[name](args)
            output = buffer.getvalue().strip() or f"Ran: {name}"
            _session_state.set_last_response(output)
            _notify(f"Heard: {text}", output)
        except Exception as exc:
            _notify("Voice Assistant — error", f"Could not complete that command: {exc}")
            continue


if __name__ == "__main__":
    main()
