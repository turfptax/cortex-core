"""Cortex Sound Manager — non-blocking audio playback via aplay.

Plays WAV files from the assets/sounds/ directory through the WM8960
sound card on the Pi Zero 2W.  Each playback launches a subprocess
so it never blocks the main loop.
"""

import logging
import os
import subprocess

from config import SOUND_ENABLED, SOUND_DEVICE, SOUND_DIR

log = logging.getLogger("sound")


class SoundManager:
    """Non-blocking audio playback using aplay subprocess."""

    def __init__(self, sound_dir=None, device=None, enabled=None):
        self._sound_dir = sound_dir or SOUND_DIR
        self._device = device or SOUND_DEVICE
        self._enabled = enabled if enabled is not None else SOUND_ENABLED
        self._current_proc = None

        if self._enabled:
            if os.path.isdir(self._sound_dir):
                sounds = [f for f in os.listdir(self._sound_dir)
                          if f.endswith(".wav")]
                log.info("SoundManager ready: %d sounds in %s",
                         len(sounds), self._sound_dir)
            else:
                log.warning("Sound directory not found: %s", self._sound_dir)
        else:
            log.info("SoundManager disabled")

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = bool(value)
        if not self._enabled:
            self.stop()

    def play(self, name, block=False):
        """Play a WAV file by name (without .wav extension).

        Non-blocking by default.  Kills any currently playing sound first.
        Returns True if playback started, False otherwise.
        """
        if not self._enabled:
            return False

        path = os.path.join(self._sound_dir, "{}.wav".format(name))
        if not os.path.exists(path):
            log.debug("Sound file not found: %s", path)
            return False

        # Kill any currently playing sound
        self.stop()

        try:
            self._current_proc = subprocess.Popen(
                ["aplay", "-D", self._device, path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.debug("Playing sound: %s", name)
            if block:
                self._current_proc.wait()
            return True
        except FileNotFoundError:
            log.warning("aplay not found — sound playback unavailable")
            self._enabled = False
            return False
        except Exception as e:
            log.error("Failed to play sound %s: %s", name, e)
            return False

    def stop(self):
        """Stop any currently playing sound."""
        if self._current_proc is not None:
            if self._current_proc.poll() is None:
                try:
                    self._current_proc.terminate()
                except Exception:
                    pass
            self._current_proc = None

    def is_playing(self):
        """Check if a sound is currently playing."""
        if self._current_proc is None:
            return False
        return self._current_proc.poll() is None
