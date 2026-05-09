"""overseer_companion.py — Slice 12 voice runtime for cortex-core.

Provides the button-driven Overseer companion flows on devices with
WM8960 audio (verified on the Pi Zero 2W at 10.0.0.132).

Three primary actions, dispatched by button vocabulary that the
StateManager maps:

  1. **Flag-this-moment** (button tap, release < SHORT_PRESS_MAX_MS):
     → record 5s of post-press audio, transcribe, save as a
       human_journal_entries row with entry_type='flag-moment'.
     LED: amber double-blink → solid amber while recording → off.

  2. **Hold-to-record** (button held, release after SHORT_PRESS_MAX_MS):
     → record while held, transcribe, route based on wake-phrase:
        - text contains 'hey overseer' / 'overseer' wake → POST chat
        - else → save as journal (entry_type='free').
     LED: solid green while recording → pulsing blue while
     transcribing/asking → off (or speak reply if overseer chat).

  3. **Notification preview** (separate, polled in StateManager.tick):
     Soft white LED shift; LCD preview; dismiss on next short-press.

All cloud paths (Groq STT, ElevenLabs TTS) gracefully degrade:
- STT: Groq → Vosk (already-installed offline model)
- TTS: ElevenLabs → espeak-ng

Offline-degrade per overseer's Slice 12 directive: if cortex-core's
own HTTP API is unreachable (shouldn't happen since this module runs
INSIDE cortex-core, but sanity), queue journal entries to a local
JSONL spool file and flush on next successful POST.

Wake-phrase routing per overseer's Slice 12 Q1 directive:
- transcript contains 'hey overseer' OR starts with 'overseer ' → chat
- everything else → journal
- VAD-equivalent: if STT returns empty after 1.5s of recording,
  treat as silent journal entry (save audio path, no text body).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("overseer_companion")

# ── Paths / config ──────────────────────────────────────────────

CORTEX_API = "http://127.0.0.1:8420"
CORTEX_AUTH = base64.b64encode(b"cortex:cortex").decode("ascii")
SECRETS_PATH = Path.home() / ".cortex" / "secrets.toml"
VOSK_MODEL_PATH = "/home/turfptax/vosk-model-small"
ALSA_CARD = "plughw:1,0"  # WM8960
TMP_DIR = "/tmp/cortex-companion"
QUEUE_DIR = Path.home() / ".cortex" / "journal_queue"

# Recording knobs
FLAG_RECORD_S = 5            # short-tap captures this many seconds post-press
HOLD_MIN_S = 0.3             # below this on release → tap, not hold
WAKE_PHRASES = ("hey overseer", "overseer ", "overseer,",
                "hi overseer", "talk to overseer")

# Reply-text TTS cap (avoid speaking 1500-char essays)
TTS_MAX_CHARS = 800


def _ensure_dirs():
    """Lazy directory creation — called on first use so module is
    importable as any user. Suppresses PermissionError so import never
    fails (the actual write later will fail visibly if dirs aren't
    writable, with a useful error path)."""
    try:
        Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    except PermissionError:
        pass
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        pass

_secrets_cache: dict | None = None

def _secrets() -> dict:
    """Lazy-load ~/.cortex/secrets.toml. Returns empty dict if missing."""
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache
    if not SECRETS_PATH.is_file():
        _secrets_cache = {}
        return _secrets_cache
    try:
        import tomllib
        with open(SECRETS_PATH, "rb") as f:
            _secrets_cache = tomllib.load(f)
    except Exception as e:
        log.warning("could not parse %s: %s", SECRETS_PATH, e)
        _secrets_cache = {}
    return _secrets_cache


# ── Audio capture (arecord subprocess) ──────────────────────────

class AudioCapture:
    """Wraps arecord. start() begins capture to a wav file; stop() ends.
    Designed to be called from button.on_press / on_release handlers,
    but can also be used as a context manager for fixed-duration captures."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.path: str | None = None
        self.start_ts: float | None = None

    def start(self) -> str:
        """Spawn arecord. Returns the wav path. Caller stops via stop()."""
        if self.proc is not None:
            log.warning("AudioCapture.start called while already recording")
            self.stop()
        _ensure_dirs()
        self.path = os.path.join(
            TMP_DIR, f"capture_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav")
        self.start_ts = time.monotonic()
        self.proc = subprocess.Popen(
            ["arecord", "-q", "-D", ALSA_CARD,
             "-f", "S16_LE", "-r", "16000", "-c", "1",
             self.path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return self.path

    def stop(self) -> tuple[str | None, float]:
        """Stop and return (path, duration_s). path is None if nothing
        was recorded (e.g. stop() called without start())."""
        if self.proc is None:
            return None, 0.0
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        dur = time.monotonic() - (self.start_ts or time.monotonic())
        path = self.path
        self.proc = None
        self.path = None
        self.start_ts = None
        if path and (not os.path.exists(path) or os.path.getsize(path) < 4096):
            return None, dur
        return path, dur

    def fixed_capture(self, seconds: float) -> str | None:
        """Block-record for `seconds`, then return path."""
        path = self.start()
        time.sleep(seconds)
        out, _ = self.stop()
        return out


# ── STT: Groq → Vosk fallback ───────────────────────────────────

_vosk_model = None

def _stt_vosk(wav_path: str) -> str:
    global _vosk_model
    import wave
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)
    if _vosk_model is None:
        _vosk_model = Model(VOSK_MODEL_PATH)
    wf = wave.open(wav_path, "rb")
    rec = KaldiRecognizer(_vosk_model, wf.getframerate())
    parts: list[str] = []
    while True:
        d = wf.readframes(4000)
        if not d:
            break
        if rec.AcceptWaveform(d):
            parts.append(json.loads(rec.Result()).get("text", ""))
    parts.append(json.loads(rec.FinalResult()).get("text", ""))
    wf.close()
    return " ".join(p for p in parts if p).strip()


def _stt_groq(wav_path: str) -> str:
    key = _secrets().get("groq", {}).get("api_key")
    if not key:
        raise RuntimeError("groq api_key missing")
    boundary = "----CortexBoundary" + uuid.uuid4().hex
    parts: list[bytes] = []

    def _f(name: str, value: str):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode())
        parts.append(value.encode())
        parts.append(b"\r\n")

    def _file(name: str, fname: str, data: bytes, mime: str):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\nContent-Type: {mime}\r\n\r\n".encode())
        parts.append(data)
        parts.append(b"\r\n")

    with open(wav_path, "rb") as f:
        _file("file", os.path.basename(wav_path), f.read(), "audio/wav")
    _f("model", _secrets().get("groq", {}).get("stt_model", "whisper-large-v3-turbo"))
    _f("language", "en")
    _f("response_format", "json")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return (json.loads(resp.read()).get("text") or "").strip()


def transcribe(wav_path: str) -> tuple[str, str]:
    """Returns (text, backend_used). Tries groq → vosk."""
    if not wav_path or not os.path.exists(wav_path):
        return "", "none"
    try:
        return _stt_groq(wav_path), "groq"
    except Exception as e:
        log.warning("groq failed (%s); falling back to vosk", e)
    try:
        return _stt_vosk(wav_path), "vosk"
    except Exception as e:
        log.error("vosk also failed: %s", e)
        return "", "fail"


# ── TTS: ElevenLabs → espeak-ng fallback ────────────────────────

def _tts_elevenlabs(text: str) -> str | None:
    cfg = _secrets().get("elevenlabs", {})
    key = cfg.get("api_key")
    if not key:
        return None
    snippet = text.strip()
    if len(snippet) > TTS_MAX_CHARS:
        cut = snippet.rfind(".", 0, TTS_MAX_CHARS) + 1
        if cut < TTS_MAX_CHARS // 2:
            cut = TTS_MAX_CHARS
        snippet = snippet[:cut]
    voice = cfg.get("voice_id", "SAz9YHcvj6GT2YYXdXww")  # River
    model = cfg.get("model", "eleven_v3")
    body = json.dumps({
        "text": snippet,
        "model_id": model,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode("utf-8")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128"
    req = urllib.request.Request(
        url, data=body,
        headers={"xi-api-key": key, "Content-Type": "application/json",
                 "Accept": "audio/mpeg"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            mp3 = resp.read()
    except Exception as e:
        log.warning("elevenlabs failed: %s", e)
        return None
    out = os.path.join(TMP_DIR, f"tts_{uuid.uuid4().hex[:8]}.mp3")
    with open(out, "wb") as f:
        f.write(mp3)
    return out


def _play_mp3(path: str):
    if shutil.which("ffplay"):
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
            check=False,
        )
        return
    if shutil.which("mpg123"):
        subprocess.run(
            ["mpg123", "-q", "-a", ALSA_CARD, path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )


def speak(text: str):
    """Speak text via ElevenLabs (cloud) → espeak-ng (offline) fallback.
    Blocks until playback finishes."""
    if not text or not text.strip():
        return
    mp3 = _tts_elevenlabs(text)
    if mp3:
        try:
            _play_mp3(mp3)
        finally:
            try:
                os.unlink(mp3)
            except OSError:
                pass
        return
    if shutil.which("espeak-ng"):
        snippet = text.strip()[:TTS_MAX_CHARS]
        subprocess.run(
            ["espeak-ng", "-s", "175", "-v", "en+f3", "-a", "180", snippet],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        )


# ── Routing (wake-phrase detection) ─────────────────────────────

def has_wake_phrase(text: str) -> bool:
    if not text:
        return False
    t = text.lower().strip()
    return any(p in t for p in WAKE_PHRASES)


# ── HTTP calls into our own cortex-core ─────────────────────────

def _post_local(path: str, body: dict, timeout: int = 60) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{CORTEX_API}{path}",
        data=data,
        headers={
            "Authorization": f"Basic {CORTEX_AUTH}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_s = ""
        try:
            body_s = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {body_s or e.reason}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"network: {e.reason}"}


def post_journal(text: str, entry_type: str = "free") -> dict:
    """POST to /plugins/overseer/human-journal. Falls back to local
    queue if unreachable."""
    if not text or not text.strip():
        return {"ok": False, "error": "empty journal text"}
    body = {"text": text.strip(), "entry_type": entry_type}
    result = _post_local("/plugins/overseer/human-journal", body, timeout=15)
    if not result.get("ok"):
        # Queue locally for later flush
        _ensure_dirs()
        spool_path = QUEUE_DIR / f"{int(time.time())}_{uuid.uuid4().hex[:6]}.json"
        with open(spool_path, "w", encoding="utf-8") as f:
            json.dump({"text": text, "entry_type": entry_type,
                       "queued_at": time.time()}, f)
        log.warning("journal queue → %s (reason: %s)",
                    spool_path, result.get("error"))
        result["queued_path"] = str(spool_path)
    return result


def chat_overseer(text: str) -> dict:
    """POST to /plugins/overseer/chat."""
    if not text or not text.strip():
        return {"ok": False, "error": "empty message"}
    return _post_local("/plugins/overseer/chat", {"message": text.strip()},
                       timeout=180)


def flush_journal_queue() -> int:
    """Try to flush any spooled journal entries. Returns count flushed."""
    flushed = 0
    for f in sorted(QUEUE_DIR.glob("*.json")):
        try:
            with open(f) as fh:
                row = json.load(fh)
            r = _post_local(
                "/plugins/overseer/human-journal",
                {"text": row.get("text", ""),
                 "entry_type": row.get("entry_type", "free")},
                timeout=10,
            )
            if r.get("ok"):
                f.unlink()
                flushed += 1
            else:
                log.debug("flush still failing %s: %s", f.name, r.get("error"))
                break  # stop on first failure to avoid hammering
        except Exception as e:
            log.warning("flush error on %s: %s", f.name, e)
            continue
    return flushed


# ── Top-level dispatch ──────────────────────────────────────────

def handle_flag_moment(audio: AudioCapture, on_state=None) -> dict:
    """Short-tap: record 5s post-press audio, transcribe, save as
    flag-moment. Returns result dict.

    on_state(name) callback fires for LED/display sync at each phase.
    Phases: 'flag-recording' → 'flag-transcribing' → 'flag-saved'."""
    if on_state:
        on_state("flag-recording")
    wav = audio.fixed_capture(FLAG_RECORD_S)
    if on_state:
        on_state("flag-transcribing")
    if not wav:
        return {"ok": False, "error": "no audio captured"}
    text, backend = transcribe(wav)
    ts = time.strftime("%H:%M:%S")
    body = f"[flag at {ts}] {text}" if text else f"[flag at {ts}] (silent)"
    result = post_journal(body, entry_type="flag-moment")
    result["transcript"] = text
    result["stt_backend"] = backend
    if on_state:
        on_state("flag-saved")
    try:
        os.unlink(wav)
    except OSError:
        pass
    return result


def handle_hold_release(wav_path: str, duration: float, on_state=None) -> dict:
    """Hold-and-release: take the captured wav, transcribe, route to
    overseer chat (if wake-phrase) or save as journal. Returns result.

    Phases: 'transcribing' → 'asking-overseer' or 'saving-journal' →
    'reply-speaking' (chat only) or 'saved' → 'idle'."""
    if not wav_path or not os.path.exists(wav_path):
        return {"ok": False, "error": "no audio captured"}
    if on_state:
        on_state("transcribing")
    text, backend = transcribe(wav_path)
    if not text:
        # Silent recording — VAD-equivalent: save to journal as silent
        result = post_journal(
            "[silent journal entry, no speech detected]",
            entry_type="free",
        )
        result["routed"] = "journal-silent"
        result["stt_backend"] = backend
        if on_state:
            on_state("saved")
        try:
            os.unlink(wav_path)
        except OSError:
            pass
        return result

    if has_wake_phrase(text):
        if on_state:
            on_state("asking-overseer")
        chat_result = chat_overseer(text)
        if on_state:
            on_state("reply-speaking" if chat_result.get("ok") else "idle")
        if chat_result.get("ok"):
            reply = chat_result.get("reply", "")
            speak(reply)
        chat_result["routed"] = "overseer-chat"
        chat_result["transcript"] = text
        chat_result["stt_backend"] = backend
        if on_state:
            on_state("idle")
        try:
            os.unlink(wav_path)
        except OSError:
            pass
        return chat_result

    # Default: journal entry
    if on_state:
        on_state("saving-journal")
    result = post_journal(text, entry_type="free")
    result["routed"] = "journal"
    result["transcript"] = text
    result["stt_backend"] = backend
    if on_state:
        on_state("saved")
    try:
        os.unlink(wav_path)
    except OSError:
        pass
    return result


# ── Notification poll ───────────────────────────────────────────

class NotificationWatcher:
    """Polls /plugins/overseer/status and surfaces deltas in
    notifications_unread. Designed to run in a background thread; the
    StateManager's tick checks .pending_preview to drive LCD/LED.

    Per overseer's Slice 12 directive: soft LED color shift, NO chirp,
    no flash for normal pattern observations. URGENT-tier (loop dead,
    cap hit) gets the brighter cue but that's not implemented yet."""

    def __init__(self, poll_interval_s: float = 30.0):
        self.poll_interval_s = poll_interval_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_unread = 0
        self.pending_preview: list[dict] = []
        self.lock = threading.Lock()

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="overseer-notif-watch", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def acknowledge_all(self):
        """Called when the user dismisses (any short-press during
        notification preview state)."""
        with self.lock:
            self.pending_preview = []

    def _loop(self):
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(
                    f"{CORTEX_API}/plugins/overseer/status",
                    headers={"Authorization": f"Basic {CORTEX_AUTH}"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                unread = data.get("overseer_db", {}).get(
                    "notifications_unread", 0)
                if unread > self._last_unread:
                    # Fetch the new ones
                    delta = unread - self._last_unread
                    self._fetch_recent(delta)
                self._last_unread = unread
            except Exception as e:
                log.debug("notification poll error: %s", e)
            self._stop.wait(self.poll_interval_s)

    def _fetch_recent(self, n: int):
        """Pull the N most recent unread notifications for preview."""
        try:
            req = urllib.request.Request(
                f"{CORTEX_API}/plugins/overseer/notifications",
                headers={"Authorization": f"Basic {CORTEX_AUTH}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            new = (data.get("notifications") or [])[:n]
            with self.lock:
                self.pending_preview = new + self.pending_preview
                self.pending_preview = self.pending_preview[:5]
        except Exception as e:
            log.debug("notification fetch error: %s", e)
