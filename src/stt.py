"""Speech-to-text engine using Vosk with push-to-talk activation.

Runs audio capture in a background thread. Main loop polls for
partial/final recognition results. PyAudio stream is opened only
when listening and closed to release the mic for arecord.
"""

import json
import threading
import time

import pyaudio
from vosk import Model, KaldiRecognizer, SetLogLevel

from config import VOSK_MODEL_PATH, STT_SAMPLE_RATE, STT_CHUNK_SIZE


class STTEngine:
    """Vosk-based speech-to-text with push-to-talk mic management."""

    def __init__(self):
        # Suppress Vosk internal logging (noisy)
        SetLogLevel(-1)
        self._model = Model(VOSK_MODEL_PATH)
        self._pa = pyaudio.PyAudio()
        self._stream = None
        self._recognizer = None
        self._thread = None
        self._running = False

        # Thread-safe result storage
        self._lock = threading.Lock()
        self._partial = ""
        self._finals = []          # queue of final results
        self._last_voice_time = 0.0  # monotonic time of last non-empty partial

    def start_listening(self):
        """Open mic and begin Vosk recognition in a background thread."""
        if self._running:
            return
        self._recognizer = KaldiRecognizer(self._model, STT_SAMPLE_RATE)
        self._recognizer.SetWords(False)
        # Open PyAudio capture stream
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=STT_SAMPLE_RATE,
            input=True,
            frames_per_buffer=STT_CHUNK_SIZE // 2,  # frames, not bytes
            input_device_index=self._find_wm8960_index(),
        )
        with self._lock:
            self._partial = ""
            self._finals.clear()
            self._last_voice_time = time.monotonic()
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop_listening(self):
        """Close mic and stop background thread. Safe to call if not listening."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._recognizer = None

    def get_partial(self):
        """Return the latest partial recognition text."""
        with self._lock:
            return self._partial

    def get_final(self):
        """Pop and return the oldest final result, or None."""
        with self._lock:
            if self._finals:
                return self._finals.pop(0)
            return None

    def get_all_finals(self):
        """Pop and return all queued final results as a single string."""
        with self._lock:
            if not self._finals:
                return None
            text = " ".join(self._finals)
            self._finals.clear()
            return text

    def seconds_since_voice(self):
        """Seconds since last non-empty partial/final was detected."""
        with self._lock:
            return time.monotonic() - self._last_voice_time

    def is_listening(self):
        """Check if the capture thread is running."""
        return self._running

    def is_mic_open(self):
        """Check if the PyAudio stream is open."""
        return self._stream is not None

    def cleanup(self):
        """Release all resources. Call on app shutdown."""
        self.stop_listening()
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    # ---- Internal ----

    def _capture_loop(self):
        """Background thread: read audio chunks and feed to Vosk."""
        while self._running:
            try:
                data = self._stream.read(
                    STT_CHUNK_SIZE // 2,  # frames
                    exception_on_overflow=False,
                )
            except Exception:
                # Stream closed or error — exit gracefully
                break

            if self._recognizer is None:
                break

            if self._recognizer.AcceptWaveform(data):
                # Final result for this utterance
                result = json.loads(self._recognizer.Result())
                text = result.get("text", "").strip()
                if text:
                    with self._lock:
                        self._finals.append(text)
                        self._last_voice_time = time.monotonic()
                        self._partial = ""
            else:
                # Partial result
                result = json.loads(self._recognizer.PartialResult())
                partial = result.get("partial", "").strip()
                with self._lock:
                    self._partial = partial
                    if partial:
                        self._last_voice_time = time.monotonic()

    def _find_wm8960_index(self):
        """Find the PyAudio device index for the WM8960 soundcard."""
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0 and "wm8960" in info["name"].lower():
                return i
        # Fallback: use default input
        return None
