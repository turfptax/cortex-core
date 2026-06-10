#!/usr/bin/env python3
"""Generate placeholder WAV sound effects for Cortex Core.

Core itself only plays boot.wav at startup (src/main.py). The rest of
the set (feed, clean, evolve, etc.) are legacy Cortex Pet effects from
before the Slice 11 extraction, kept because plugins can reach them
through the SoundManager fallback in plugin_api.py.

Creates simple sine-wave based sounds using only Python stdlib (wave + struct).
These are functional placeholders — replace with real sound design later.

Usage:
    python generate_sounds.py [output_dir]
    Default output: ../src/assets/sounds/
"""

import math
import os
import struct
import sys
import wave

SAMPLE_RATE = 22050
DEFAULT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "assets", "sounds",
)


def generate_tone(freq, duration_s, volume=0.5, sample_rate=SAMPLE_RATE):
    """Generate a sine wave tone as raw 16-bit PCM samples."""
    n_samples = int(sample_rate * duration_s)
    samples = []
    for i in range(n_samples):
        t = i / sample_rate
        # Apply fade-in/fade-out envelope (10ms ramps)
        ramp = min(1.0, i / (sample_rate * 0.01),
                   (n_samples - i) / (sample_rate * 0.01))
        value = volume * ramp * math.sin(2 * math.pi * freq * t)
        samples.append(int(value * 32767))
    return samples


def generate_sweep(start_freq, end_freq, duration_s, volume=0.5):
    """Generate a frequency sweep (rising or falling tone)."""
    n_samples = int(SAMPLE_RATE * duration_s)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        progress = i / n_samples
        freq = start_freq + (end_freq - start_freq) * progress
        ramp = min(1.0, i / (SAMPLE_RATE * 0.01),
                   (n_samples - i) / (SAMPLE_RATE * 0.01))
        value = volume * ramp * math.sin(2 * math.pi * freq * t)
        samples.append(int(value * 32767))
    return samples


def silence(duration_s):
    """Generate silence."""
    return [0] * int(SAMPLE_RATE * duration_s)


def write_wav(filepath, samples, sample_rate=SAMPLE_RATE):
    """Write 16-bit mono WAV file."""
    with wave.open(filepath, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        data = struct.pack("<{}h".format(len(samples)), *samples)
        wf.writeframes(data)


def gen_feed(out_dir):
    """Feed sound: 3 rising happy tones (nom nom nom)."""
    samples = []
    for freq in [600, 800, 1000]:
        samples.extend(generate_tone(freq, 0.12, 0.4))
        samples.extend(silence(0.05))
    write_wav(os.path.join(out_dir, "feed.wav"), samples)


def gen_clean(out_dir):
    """Clean sound: sparkle sweep up then down."""
    samples = generate_sweep(800, 2000, 0.2, 0.3)
    samples.extend(generate_sweep(2000, 1200, 0.15, 0.25))
    write_wav(os.path.join(out_dir, "clean.wav"), samples)


def gen_hungry(out_dir):
    """Hungry sound: plaintive low descending tone."""
    samples = generate_sweep(400, 250, 0.5, 0.35)
    samples.extend(silence(0.1))
    samples.extend(generate_sweep(350, 200, 0.4, 0.25))
    write_wav(os.path.join(out_dir, "hungry.wav"), samples)


def gen_happy_chirp(out_dir):
    """Happy chirp: quick 3-note ascending arpeggio."""
    for freq in [800, 1000, 1200]:
        samples = generate_tone(freq, 0.08, 0.35)
    # Rebuild properly
    samples = []
    for freq in [800, 1000, 1200]:
        samples.extend(generate_tone(freq, 0.08, 0.35))
        samples.extend(silence(0.02))
    write_wav(os.path.join(out_dir, "happy_chirp.wav"), samples)


def gen_sad_tone(out_dir):
    """Sad tone: slow descending minor interval."""
    samples = generate_tone(440, 0.3, 0.3)
    samples.extend(silence(0.05))
    samples.extend(generate_tone(330, 0.4, 0.25))
    write_wav(os.path.join(out_dir, "sad_tone.wav"), samples)


def gen_thinking(out_dir):
    """Thinking: soft sustained hum."""
    samples = generate_tone(440, 0.6, 0.15)
    write_wav(os.path.join(out_dir, "thinking.wav"), samples)


def gen_response(out_dir):
    """Response: light ding (high short tone)."""
    samples = generate_tone(1200, 0.15, 0.35)
    write_wav(os.path.join(out_dir, "response.wav"), samples)


def gen_coma_enter(out_dir):
    """Coma enter: descending tones fading out."""
    samples = []
    for i, freq in enumerate([600, 500, 400, 300]):
        vol = 0.35 - i * 0.08
        samples.extend(generate_tone(freq, 0.3, max(0.05, vol)))
        samples.extend(silence(0.05))
    write_wav(os.path.join(out_dir, "coma_enter.wav"), samples)


def gen_coma_wake(out_dir):
    """Coma wake: rising fanfare."""
    samples = []
    for i, freq in enumerate([300, 400, 500, 600, 800]):
        vol = 0.15 + i * 0.05
        samples.extend(generate_tone(freq, 0.2, min(0.4, vol)))
        samples.extend(silence(0.03))
    # Final triumphant chord (layered)
    chord = generate_tone(800, 0.3, 0.3)
    chord_high = generate_tone(1200, 0.3, 0.2)
    for i in range(len(chord)):
        if i < len(chord_high):
            chord[i] = max(-32767, min(32767, chord[i] + chord_high[i]))
    samples.extend(chord)
    write_wav(os.path.join(out_dir, "coma_wake.wav"), samples)


def gen_evolve(out_dir):
    """Evolve: celebration ascending scale with harmony."""
    samples = []
    scale = [523, 587, 659, 784, 1047]  # C5 D5 E5 G5 C6
    for freq in scale:
        samples.extend(generate_tone(freq, 0.15, 0.3))
        samples.extend(silence(0.02))
    # Hold final note longer
    samples.extend(generate_tone(1047, 0.4, 0.35))
    write_wav(os.path.join(out_dir, "evolve.wav"), samples)


def gen_alert(out_dir):
    """Alert: short warning beep (repeatable)."""
    samples = generate_tone(700, 0.1, 0.4)
    samples.extend(silence(0.05))
    samples.extend(generate_tone(700, 0.1, 0.4))
    write_wav(os.path.join(out_dir, "alert.wav"), samples)


def gen_boot(out_dir):
    """Boot: short startup jingle."""
    samples = []
    for freq in [523, 659, 784]:  # C E G major triad
        samples.extend(generate_tone(freq, 0.12, 0.3))
        samples.extend(silence(0.03))
    write_wav(os.path.join(out_dir, "boot.wav"), samples)


def gen_sleep(out_dir):
    """Sleep: soft single fading tone."""
    n = int(SAMPLE_RATE * 0.8)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        fade = 1.0 - (i / n)  # linear fade out
        value = 0.2 * fade * math.sin(2 * math.pi * 350 * t)
        samples.append(int(value * 32767))
    write_wav(os.path.join(out_dir, "sleep.wav"), samples)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR
    os.makedirs(out_dir, exist_ok=True)

    generators = [
        gen_feed, gen_clean, gen_hungry, gen_happy_chirp, gen_sad_tone,
        gen_thinking, gen_response, gen_coma_enter, gen_coma_wake,
        gen_evolve, gen_alert, gen_boot, gen_sleep,
    ]

    for gen in generators:
        name = gen.__name__.replace("gen_", "")
        gen(out_dir)
        path = os.path.join(out_dir, "{}.wav".format(name))
        size = os.path.getsize(path)
        print("  {} ({} bytes)".format(name, size))

    print("\nGenerated {} sounds in {}".format(len(generators), out_dir))


if __name__ == "__main__":
    main()
