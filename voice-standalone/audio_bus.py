"""
audio_bus.py — Step 9: Single microphone capture stream with resampled distribution.

Opens exactly ONE sounddevice.InputStream at 24kHz native capture rate,
feeding both:
  1. Wake-word & local VAD/Whisper consumer at 16kHz (resampled via soxr.ResampleStream)
  2. OpenAI Realtime conversation consumer at native 24kHz

Prevents concurrent device opens / PipeWire duplicate stream contention.
"""

import queue
import threading
from typing import Optional

import numpy as np
import sounddevice as sd
import soxr

NATIVE_SAMPLE_RATE = 24000
NATIVE_CHANNELS = 1
NATIVE_DTYPE = "int16"
NATIVE_BLOCKSIZE = 480  # 20ms at 24kHz


class AudioSubscription:
    """Subscription handle for an audio consumer requesting a specific sample rate
    and chunk size."""

    def __init__(
        self,
        bus: "AudioBus",
        sample_rate: int = 16000,
        chunk_size: int = 1280,
        maxsize: int = 100,
    ) -> None:
        self._bus = bus
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=maxsize)
        self._buf = np.zeros(0, dtype=np.int16)
        self._closed = False

        if sample_rate != NATIVE_SAMPLE_RATE:
            self._resampler: Optional[soxr.ResampleStream] = soxr.ResampleStream(
                NATIVE_SAMPLE_RATE, sample_rate, NATIVE_CHANNELS, dtype=NATIVE_DTYPE, quality="MQ"
            )
        else:
            self._resampler = None

    def feed(self, raw_chunk_24k: np.ndarray) -> None:
        """Called by AudioBus distributor thread with native 24kHz audio."""
        if self._closed:
            return

        if self._resampler is not None:
            resampled = self._resampler.resample_chunk(raw_chunk_24k)
        else:
            resampled = raw_chunk_24k

        if len(self._buf) > 0:
            self._buf = np.concatenate([self._buf, resampled])
        else:
            self._buf = resampled

        while len(self._buf) >= self.chunk_size:
            chunk = self._buf[: self.chunk_size]
            self._buf = self._buf[self.chunk_size :]
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                # Drop oldest chunk to avoid unbounded latency if subscriber is slow
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                self._queue.put_nowait(chunk)

    def get(self, timeout: Optional[float] = None) -> np.ndarray:
        """Blocking get of next numpy int16 chunk."""
        return self._queue.get(timeout=timeout)

    def get_bytes(self, timeout: Optional[float] = None) -> bytes:
        """Blocking get of next chunk as raw bytes."""
        return self._queue.get(timeout=timeout).tobytes()

    def get_nowait(self) -> np.ndarray:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    def drain(self) -> None:
        """Discard any pending buffered audio in the queue and internal buffer."""
        self._buf = np.zeros(0, dtype=np.int16)
        if self._resampler is not None:
            self._resampler.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus.unsubscribe(self)

    def __enter__(self) -> "AudioSubscription":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class AudioBus:
    """Manages the single shared InputStream on the microphone."""

    def __init__(self) -> None:
        self._subscribers: list[AudioSubscription] = []
        self._lock = threading.Lock()
        self._stream: Optional[sd.InputStream] = None
        self._raw_queue: queue.Queue[np.ndarray] = queue.Queue()
        self._running = False
        self._distributor_thread: Optional[threading.Thread] = None

    def _distributor_loop(self) -> None:
        while self._running:
            try:
                chunk = self._raw_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            with self._lock:
                active_subs = list(self._subscribers)

            for sub in active_subs:
                sub.feed(chunk)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True

            self._distributor_thread = threading.Thread(
                target=self._distributor_loop, name="audio-bus-distributor", daemon=True
            )
            self._distributor_thread.start()

            def _callback(indata, _frames, _time_info, _status):
                if self._running:
                    self._raw_queue.put(indata.copy().flatten())

            self._stream = sd.InputStream(
                samplerate=NATIVE_SAMPLE_RATE,
                channels=NATIVE_CHANNELS,
                dtype=NATIVE_DTYPE,
                blocksize=NATIVE_BLOCKSIZE,
                callback=_callback,
            )
            self._stream.start()

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False

            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

            if self._distributor_thread is not None:
                self._distributor_thread.join(timeout=1.0)
                self._distributor_thread = None

            while not self._raw_queue.empty():
                try:
                    self._raw_queue.get_nowait()
                except queue.Empty:
                    break

    def subscribe(
        self,
        sample_rate: int = 16000,
        chunk_size: int = 1280,
        maxsize: int = 100,
    ) -> AudioSubscription:
        """Subscribes a consumer for resampled audio chunks. Starts the bus
        stream if not already running."""
        self.start()
        sub = AudioSubscription(self, sample_rate=sample_rate, chunk_size=chunk_size, maxsize=maxsize)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: AudioSubscription) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)


# Global singleton instance
audio_bus = AudioBus()
