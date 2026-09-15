import queue
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf

from config import SAMPLE_RATE


def record_until_stopped(path: str, stop_event: threading.Event) -> str:
    """Record until the caller sets stop_event — the actual recording core,
    decoupled from *how* stop is signaled (Enter key, a tray click over a
    FIFO, anything else)."""
    frames: list[np.ndarray] = []
    q: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata, _frame_count, _time_info, _status) -> None:
        q.put(indata.copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=callback):
        while not stop_event.is_set():
            try:
                frames.append(q.get(timeout=0.1))
            except queue.Empty:
                continue

    audio = np.concatenate(frames, axis=0) if frames else np.zeros((0, 1), dtype="int16")
    sf.write(path, audio, SAMPLE_RATE)
    return path


def record_until_enter(path: str) -> str:
    """Terminal/manual-testing mode only (main.py) — NOT used by the tray+
    daemon path, which signals stop via daemon.py's FIFO instead. Keeping
    this is a developer convenience for running main.py directly, not a
    user-facing control surface."""
    stop_event = threading.Event()

    def wait_for_enter() -> None:
        input()
        stop_event.set()

    threading.Thread(target=wait_for_enter, daemon=True).start()
    return record_until_stopped(path, stop_event)
