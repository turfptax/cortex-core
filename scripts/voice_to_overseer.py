#!/usr/bin/env python3
"""voice_to_overseer.py — push-to-talk demo: record → STT → overseer
chat → TTS reply.

Built for the Pi Zero 2W (.132) demo at a friend event. Uses cloud
STT/TTS for quality (Groq Whisper-turbo + ElevenLabs eleven_v3) with
on-device fallbacks (Vosk + espeak-ng) so the script keeps working
offline.

Flow:
  1. press Enter → start recording from WM8960 mic
  2. press Enter again → stop recording
  3. STT: Groq Whisper-large-v3-turbo (free tier, ~30 RPM)
  4. POST transcript to overseer chat (local cortex-core HTTP API)
  5. TTS: ElevenLabs eleven_v3 default voice; play via ffplay

Secrets are read from ~/.cortex/secrets.toml (mode 600, gitignored):
  [openrouter]   api_key = "sk-or-..."   (used by overseer chat itself)
  [groq]         api_key = "gsk_..."     stt_model = "whisper-large-v3-turbo"
  [elevenlabs]   api_key = "sk_..."      model = "eleven_v3"  voice_id = "..."

Either provider can be missing — the script falls back. If both fail,
prints reply text to stdout (no voice).

Override via env: CORTEX_HOST, CORTEX_PORT, ALSA_CARD,
TTS_BACKEND={elevenlabs|espeak|none}, STT_BACKEND={groq|vosk}.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
import wave
import pathlib
import tomllib
import mimetypes
import uuid

# ── Config / secrets ────────────────────────────────────────────

SECRETS_PATH = pathlib.Path.home() / ".cortex" / "secrets.toml"

def load_secrets() -> dict:
    if not SECRETS_PATH.is_file():
        return {}
    try:
        with open(SECRETS_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"warn: could not read secrets.toml: {e}", file=sys.stderr)
        return {}

SECRETS = load_secrets()

GROQ_API_KEY = (
    os.environ.get("GROQ_API_KEY")
    or SECRETS.get("groq", {}).get("api_key")
)
GROQ_STT_MODEL = (
    os.environ.get("GROQ_STT_MODEL")
    or SECRETS.get("groq", {}).get("stt_model")
    or "whisper-large-v3-turbo"
)
ELEVENLABS_API_KEY = (
    os.environ.get("ELEVENLABS_API_KEY")
    or SECRETS.get("elevenlabs", {}).get("api_key")
)
ELEVENLABS_MODEL = (
    os.environ.get("ELEVENLABS_MODEL")
    or SECRETS.get("elevenlabs", {}).get("model")
    or "eleven_v3"
)
# Rachel — ElevenLabs' canonical default voice. Tory can override
# in secrets.toml [elevenlabs].voice_id.
ELEVENLABS_VOICE_ID = (
    os.environ.get("ELEVENLABS_VOICE_ID")
    or SECRETS.get("elevenlabs", {}).get("voice_id")
    or "21m00Tcm4TlvDq8ikWAM"
)

# Backend selection — auto by default, env override for testing.
STT_BACKEND = os.environ.get(
    "STT_BACKEND", "groq" if GROQ_API_KEY else "vosk")
TTS_BACKEND = os.environ.get(
    "TTS_BACKEND", "elevenlabs" if ELEVENLABS_API_KEY else "espeak")

VOSK_MODEL_PATH = os.environ.get(
    "VOSK_MODEL_PATH", "/home/turfptax/vosk-model-small")
CORTEX_HOST = os.environ.get("CORTEX_HOST", "127.0.0.1")
CORTEX_PORT = int(os.environ.get("CORTEX_PORT", "8420"))
CORTEX_USER = os.environ.get("CORTEX_USER", "cortex")
CORTEX_PASS = os.environ.get("CORTEX_PASS", "cortex")

ALSA_CARD = os.environ.get("ALSA_CARD", "plughw:1,0")  # WM8960 default
TMP_DIR = "/tmp"
SOUNDS_DIR = "/home/turfptax/cortex-core/assets/sounds"

# Reply text cap for TTS — overseer often emits 1500+ chars; cap to a
# sensible spoken length (full reply still prints on stdout). 1500
# chars = ~10 ElevenLabs char-units; well within free-tier 10k/mo if
# used a few dozen times.
TTS_MAX_CHARS = 1200

OVERSEER_TIMEOUT_S = 180


# ── ANSI colors (only on a TTY) ──────────────────────────────────
_TTY = sys.stdout.isatty()
def _c(code):
    return f"\033[{code}m" if _TTY else ""
C_BOLD = _c("1"); C_DIM = _c("2")
C_GREEN = _c("32"); C_YELLOW = _c("33"); C_BLUE = _c("34")
C_CYAN = _c("36"); C_RED = _c("31"); C_RESET = _c("0")


# ── Sound helpers ────────────────────────────────────────────────

def play_wav(name: str):
    path = os.path.join(SOUNDS_DIR, name + ".wav")
    if not os.path.exists(path):
        return
    subprocess.Popen(
        ["aplay", "-q", "-D", ALSA_CARD, path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def play_mp3(path: str):
    """Play an mp3 file synchronously, blocking until finished."""
    if shutil.which("mpg123"):
        subprocess.run(
            ["mpg123", "-q", "-a", ALSA_CARD, path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return
    if shutil.which("ffplay"):
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return
    # last-ditch fallback: convert to wav with ffmpeg, then aplay
    if shutil.which("ffmpeg"):
        wav = path + ".wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-ar", "22050", "-ac", "1", wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        subprocess.run(
            ["aplay", "-q", "-D", ALSA_CARD, wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        try:
            os.unlink(wav)
        except OSError:
            pass
        return
    print(f"  {C_DIM}(no mp3 player; reply printed only){C_RESET}")


# ── Recording (arecord on WM8960) ────────────────────────────────

def start_recording(out_path: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "arecord", "-q", "-D", ALSA_CARD,
            "-f", "S16_LE", "-r", "16000", "-c", "1",
            out_path,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stop_recording(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


# ── STT — Groq Whisper turbo (cloud) ─────────────────────────────

def stt_groq(wav_path: str) -> str:
    """Upload the wav to Groq's Whisper-large-v3-turbo. Returns the
    transcribed text. Raises on any HTTP / network error so the caller
    can fall back to Vosk."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")
    boundary = "----CortexBoundary" + uuid.uuid4().hex
    parts: list[bytes] = []

    def add_field(name: str, value: str):
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode())
        parts.append(b"\r\n")

    def add_file(name: str, filename: str, data: bytes, mime: str):
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'.encode())
        parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
        parts.append(data)
        parts.append(b"\r\n")

    with open(wav_path, "rb") as f:
        wav_bytes = f.read()
    add_file("file", os.path.basename(wav_path), wav_bytes, "audio/wav")
    add_field("model", GROQ_STT_MODEL)
    add_field("language", "en")
    add_field("response_format", "json")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return (data.get("text") or "").strip()
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"groq HTTP {e.code}: {err_body or e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"groq network: {e.reason}")


# ── STT — Vosk fallback (offline, smaller model) ─────────────────

_vosk_model = None

def stt_vosk(wav_path: str) -> str:
    global _vosk_model
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)
    if _vosk_model is None:
        if not os.path.isdir(VOSK_MODEL_PATH):
            raise RuntimeError(f"Vosk model not found at {VOSK_MODEL_PATH}")
        _vosk_model = Model(VOSK_MODEL_PATH)
    wf = wave.open(wav_path, "rb")
    rec = KaldiRecognizer(_vosk_model, wf.getframerate())
    parts: list[str] = []
    while True:
        data = wf.readframes(4000)
        if not data:
            break
        if rec.AcceptWaveform(data):
            parts.append(json.loads(rec.Result()).get("text", ""))
    parts.append(json.loads(rec.FinalResult()).get("text", ""))
    wf.close()
    return " ".join(p for p in parts if p).strip()


def transcribe(wav_path: str) -> tuple[str, str]:
    """Returns (text, backend-used)."""
    backends = [STT_BACKEND]
    if STT_BACKEND == "groq":
        backends.append("vosk")  # fallback
    last_err = None
    for b in backends:
        try:
            if b == "groq":
                return stt_groq(wav_path), "groq"
            if b == "vosk":
                return stt_vosk(wav_path), "vosk"
        except Exception as e:
            last_err = e
            print(f"  {C_DIM}{b} STT failed: {e}{C_RESET}")
            continue
    raise RuntimeError(f"all STT backends failed: {last_err}")


# ── Overseer chat call ───────────────────────────────────────────

def overseer_chat(message: str) -> dict:
    auth = base64.b64encode(
        f"{CORTEX_USER}:{CORTEX_PASS}".encode("utf-8")).decode("ascii")
    body = json.dumps({"message": message}).encode("utf-8")
    url = f"http://{CORTEX_HOST}:{CORTEX_PORT}/plugins/overseer/chat"
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OVERSEER_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_s = ""
        try:
            body_s = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {body_s or e.reason}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"network: {e.reason}"}


# ── TTS — ElevenLabs (cloud, high quality) ───────────────────────

def tts_elevenlabs(text: str) -> str | None:
    """POST text → mp3, save to /tmp, return path. Returns None on failure."""
    if not ELEVENLABS_API_KEY:
        return None
    snippet = text.strip()
    if len(snippet) > TTS_MAX_CHARS:
        cut = snippet.rfind(".", 0, TTS_MAX_CHARS) + 1
        if cut < TTS_MAX_CHARS // 2:
            cut = TTS_MAX_CHARS
        snippet = snippet[:cut]
    body = json.dumps({
        "text": snippet,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
        },
    }).encode("utf-8")
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        f"?output_format=mp3_44100_128"
    )
    req = urllib.request.Request(
        url, data=body,
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            mp3_bytes = resp.read()
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        print(f"  {C_DIM}elevenlabs HTTP {e.code}: {err_body}{C_RESET}")
        return None
    except urllib.error.URLError as e:
        print(f"  {C_DIM}elevenlabs network: {e.reason}{C_RESET}")
        return None
    out_path = os.path.join(
        TMP_DIR, f"overseer_tts_{uuid.uuid4().hex[:8]}.mp3")
    with open(out_path, "wb") as f:
        f.write(mp3_bytes)
    return out_path


def tts_espeak(text: str):
    if not shutil.which("espeak-ng"):
        return
    snippet = text.strip()[:TTS_MAX_CHARS]
    try:
        subprocess.run(
            ["espeak-ng", "-s", "175", "-v", "en+f3", "-a", "180", snippet],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        )
    except subprocess.TimeoutExpired:
        pass


def speak(text: str) -> str:
    """Returns the backend used, or 'none'."""
    if not text or not text.strip():
        return "none"
    if TTS_BACKEND == "elevenlabs":
        mp3 = tts_elevenlabs(text)
        if mp3:
            try:
                play_mp3(mp3)
            finally:
                try:
                    os.unlink(mp3)
                except OSError:
                    pass
            return "elevenlabs"
        # fall through to espeak
        print(f"  {C_DIM}(falling back to espeak-ng){C_RESET}")
    tts_espeak(text)
    return "espeak"


# ── Main loop ────────────────────────────────────────────────────

def banner():
    print(f"\n{C_BOLD}{C_CYAN}┌─ Voice → Overseer ─{'─' * 40}┐{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}│{C_RESET}  Cortex node : {C_BOLD}{CORTEX_HOST}:{CORTEX_PORT}{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}│{C_RESET}  Mic device  : {ALSA_CARD}")
    print(f"{C_BOLD}{C_CYAN}│{C_RESET}  STT backend : {C_BOLD}{STT_BACKEND}"
          f"{C_RESET} {C_DIM}({GROQ_STT_MODEL if STT_BACKEND=='groq' else VOSK_MODEL_PATH}){C_RESET}")
    print(f"{C_BOLD}{C_CYAN}│{C_RESET}  TTS backend : {C_BOLD}{TTS_BACKEND}"
          f"{C_RESET} {C_DIM}({ELEVENLABS_MODEL if TTS_BACKEND=='elevenlabs' else 'espeak-ng'}){C_RESET}")
    print(f"{C_BOLD}{C_CYAN}└{'─' * 60}┘{C_RESET}\n")


def main():
    banner()
    print(f"{C_DIM}Press {C_RESET}{C_BOLD}Enter{C_RESET}{C_DIM} to START recording, "
          f"Enter again to STOP. Ctrl+C to quit.{C_RESET}\n")
    play_wav("boot")

    turn = 0
    while True:
        turn += 1
        try:
            input(f"{C_BOLD}{C_GREEN}[turn {turn}] press Enter to talk...{C_RESET} ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        wav_path = os.path.join(TMP_DIR, f"voice_{turn:03d}.wav")
        print(f"  {C_YELLOW}● recording...{C_RESET} (Enter to stop)")
        rec_t0 = time.monotonic()
        proc = start_recording(wav_path)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print()
            stop_recording(proc)
            return 0
        stop_recording(proc)
        rec_dur = time.monotonic() - rec_t0
        if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 4096:
            print(f"  {C_DIM}(no audio captured, try again){C_RESET}\n")
            continue

        print(f"  {C_DIM}recorded {rec_dur:.1f}s, transcribing...{C_RESET}")
        play_wav("thinking")
        try:
            t0 = time.monotonic()
            transcript, stt_used = transcribe(wav_path)
            stt_ms = int((time.monotonic() - t0) * 1000)
        except Exception as e:
            print(f"  {C_RED}STT error: {e}{C_RESET}\n")
            continue
        if not transcript:
            print(f"  {C_DIM}(no speech detected, try again){C_RESET}\n")
            continue
        print(f"  {C_BLUE}YOU{C_RESET} {C_DIM}({stt_used} {stt_ms}ms):{C_RESET} {transcript}")

        print(f"  {C_DIM}asking overseer...{C_RESET}")
        t0 = time.monotonic()
        result = overseer_chat(transcript)
        chat_ms = int((time.monotonic() - t0) * 1000)
        if not result.get("ok"):
            print(f"  {C_RED}✗ overseer error: {result.get('error')}{C_RESET}\n")
            continue
        reply = (result.get("reply") or "").strip()
        cost = result.get("cost_usd") or 0.0
        tools = result.get("tool_calls") or []
        backend = result.get("backend") or "?"
        model = (result.get("model") or "?").split("/")[-1]
        print(
            f"\n{C_BOLD}{C_CYAN}OVERSEER{C_RESET} "
            f"{C_DIM}({backend}/{model} · {chat_ms}ms · ${cost:.4f}"
            f"{f' · {len(tools)} tools' if tools else ''}){C_RESET}\n"
        )
        print(reply)
        if tools:
            print(f"\n{C_DIM}tools used: {', '.join(t['name'] for t in tools)}{C_RESET}")
        print()
        t0 = time.monotonic()
        tts_used = speak(reply)
        tts_ms = int((time.monotonic() - t0) * 1000)
        print(f"  {C_DIM}(spoken via {tts_used} in {tts_ms}ms){C_RESET}\n")

        # Cleanup recording
        try:
            os.unlink(wav_path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
