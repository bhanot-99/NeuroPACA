import queue
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf

from config import SAMPLE_RATE


def record_until_enter(path: str) -> str:
    frames: list[np.ndarray] = []
    q: queue.Queue[np.ndarray] = queue.Queue()
    stop_event = threading.Event()

    def callback(indata, _frame_count, _time_info, _status) -> None:
        q.put(indata.copy())

    def wait_for_enter() -> None:
        input()
        stop_event.set()

    threading.Thread(target=wait_for_enter, daemon=True).start()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=callback):
        while not stop_event.is_set():
            try:
                frames.append(q.get(timeout=0.1))
            except queue.Empty:
                continue

    audio = np.concatenate(frames, axis=0) if frames else np.zeros((0, 1), dtype="int16")
    sf.write(path, audio, SAMPLE_RATE)
    return path
