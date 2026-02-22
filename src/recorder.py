"""Audio recording engine using arecord with automatic segment splitting."""

import os
import signal
import subprocess
import time
import glob

from config import (
    AUDIO_DEVICE, SAMPLE_RATE, CHANNELS, SAMPLE_FORMAT,
    SEGMENT_SECONDS, RECORDING_DIR, BYTE_RATE,
)


class Recorder:
    def __init__(self):
        self.proc = None
        self.state = "IDLE"  # IDLE, RECORDING
        self.session_start = None
        self.segment_count = 0
        self._last_known_segment = None  # for detecting new segments

    def start(self):
        """Launch arecord subprocess with automatic segment splitting."""
        os.makedirs(RECORDING_DIR, exist_ok=True)
        output_pattern = os.path.join(RECORDING_DIR, "%Y%m%d_%H%M%S.wav")
        cmd = [
            "arecord",
            "-D", AUDIO_DEVICE,
            "-f", SAMPLE_FORMAT,
            "-r", str(SAMPLE_RATE),
            "-c", str(CHANNELS),
            "-t", "wav",
            "--max-file-time", str(SEGMENT_SECONDS),
            "--use-strftime", output_pattern,
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.state = "RECORDING"
        if self.session_start is None:
            self.session_start = time.time()
            self.segment_count = 0
        self.segment_count += 1

    def stop(self):
        """Stop recording gracefully with SIGINT to preserve WAV headers."""
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        self.state = "IDLE"

    def is_alive(self):
        """Check if arecord subprocess is still running."""
        return self.proc is not None and self.proc.poll() is None

    def get_session_elapsed(self):
        """Total seconds since recording session started."""
        if self.session_start is not None:
            return time.time() - self.session_start
        return 0.0

    def get_segment_elapsed(self):
        """Approximate seconds into the current segment."""
        if self.state != "RECORDING" or self.session_start is None:
            return 0.0
        elapsed = self.get_session_elapsed()
        return elapsed % SEGMENT_SECONDS

    def get_segment_count(self):
        """Count WAV files in the recording directory for this session."""
        try:
            return len(glob.glob(os.path.join(RECORDING_DIR, "*.wav")))
        except OSError:
            return 0

    def reset_session(self):
        """Reset session tracking (called when stopping fully)."""
        self.session_start = None
        self.segment_count = 0
        self._last_known_segment = None

    def check_new_segment(self):
        """Detect if arecord created a new WAV segment file.

        Returns the basename of the new file, or None if no change.
        """
        try:
            wav_files = sorted(glob.glob(os.path.join(RECORDING_DIR, "*.wav")))
        except OSError:
            return None
        if not wav_files:
            return None
        latest = wav_files[-1]
        if latest != self._last_known_segment:
            self._last_known_segment = latest
            return os.path.basename(latest)
        return None

    @staticmethod
    def get_disk_usage():
        """Return (used_bytes, free_bytes, total_bytes) for the recording partition."""
        stat = os.statvfs(RECORDING_DIR if os.path.exists(RECORDING_DIR) else "/")
        free = stat.f_bavail * stat.f_frsize
        total = stat.f_blocks * stat.f_frsize
        used = total - free
        return used, free, total

    @staticmethod
    def get_remaining_hours():
        """Estimated hours of recording capacity remaining."""
        _, free, _ = Recorder.get_disk_usage()
        if BYTE_RATE <= 0:
            return 0.0
        return free / BYTE_RATE / 3600.0
