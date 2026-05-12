"""State machine for Cortex Core.

Extracts all state-management logic from main.py into a class-based
structure. StateContext holds hardware/service references (no behavior).
StateManager owns all state transitions, gamepad dispatch, button
callbacks, and per-frame tick logic.
"""

import glob
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

# Slice 12: overseer companion mode wires the on-device button to
# Record-Journal / Talk-to-Overseer / Flag-this-moment flows. This
# import is best-effort — if the companion module fails to load
# (e.g., missing arecord), the device falls back to legacy memory-mode
# behavior (HOME → STT_LISTENING) without breaking boot.
try:
    import overseer_companion as oc
    _OC_AVAILABLE = True
except Exception as _oc_err:
    logging.getLogger("states").warning(
        "overseer_companion unavailable; falling back to legacy HOME flow: %s",
        _oc_err,
    )
    oc = None
    _OC_AVAILABLE = False

from config import (
    BACKLIGHT_BRIGHTNESS, DISPLAY_TIMEOUT_S, DISPLAY_UPDATE_HZ,
    STT_LISTEN_TIMEOUT_S, STT_NOTE_SILENCE_S, NOTES_DIR,
    CORTEX_DB_PATH, GAMES_ENABLED, GAME_FPS, PONG_MODEL_PATH,
    RECORDING_DIR,
    HTTP_PORT, HTTP_USERNAME, HTTP_PASSWORD,
    BLE_ENABLED, DISPLAY_WIDTH, DISPLAY_HEIGHT,
    HOME, SHORT_PRESS_MAX_MS,
)
from note_utils import save_note
from display_state import DisplayState, BLEInfo
from debug import dbg

SETTINGS_FILE = os.path.join(HOME, "cortex-settings.json")


class StateContext:
    """Holds references to all hardware and services — no logic."""

    __slots__ = (
        "board", "recorder", "display", "button", "led", "logger",
        "stt", "ble", "cortex_db", "cortex", "gamepad",
        "menu", "sound", "http_server", "battery",
    )

    def __init__(self, *, board, recorder, display, button, led, logger,
                 stt, ble, cortex_db, cortex, gamepad, menu, sound,
                 http_server, battery=None):
        self.board = board
        self.recorder = recorder
        self.display = display
        self.button = button
        self.led = led
        self.logger = logger
        self.stt = stt
        self.ble = ble
        self.cortex_db = cortex_db
        self.cortex = cortex
        self.gamepad = gamepad
        self.menu = menu
        self.sound = sound
        self.http_server = http_server
        self.battery = battery


class StateManager:
    """Owns all application state and transitions."""

    def __init__(self, ctx: StateContext):
        self.ctx = ctx

        # ── State variables ─────────────────────────────────────
        self.app_state = "HOME"
        self.last_interaction = time.monotonic()
        self.backlight_on = True
        self.pause_start_mono = None
        self.note_text = ""
        self.note_start_mono = None
        self.stt_initiated_from = None

        # Settings adjustment state
        self.setting_name = ""
        self.setting_value = 0
        self.setting_min = 0
        self.setting_max = 100
        saved = self._load_settings()
        self.brightness_level = saved.get("brightness", BACKLIGHT_BRIGHTNESS)
        self.volume_level = saved.get("volume", 80)
        self.display_hz = saved.get("display_hz", DISPLAY_UPDATE_HZ)

        # Game state
        self.pong_game = None
        self.pong_ai = None
        self.pong_renderer = None
        self.pong_last_tick = 0.0
        self.loop_interval = 1.0 / max(1, self.display_hz)

        # ── Slice 12: overseer companion mode ────────────────────
        # When True, button events on HOME route to overseer flows
        # (flag-moment / hold-record-route) instead of legacy STT.
        # Defaults True if the module loaded; override-able via env var.
        self.companion_mode = (
            _OC_AVAILABLE
            and os.environ.get("CORTEX_COMPANION_MODE", "1") != "0"
        )
        self.companion_status = ""  # short status string for LED/LCD
        self.companion_message = ""  # last reply / journal text for LCD
        self.companion_routed = ""  # "journal" | "overseer-chat" | "flag-moment"
        self._oc_audio = oc.AudioCapture() if _OC_AVAILABLE else None
        self._oc_busy = False  # True while a flag/hold thread is running
        self.notification_watcher = None  # set later in main.py boot
        # Slice 12.1 wake-on-press: when the user presses while the
        # backlight is off, that press is treated as a screen-wake only
        # — no audio capture, no state transition, the matching release
        # is swallowed. This flag is set by main._on_any_press based on
        # backlight state, then consumed by handle_short_press.
        self._oc_wake_only_press = False
        # Slice 12.1.2 toggle-record state: True from the moment the
        # user taps to START a recording until the moment they tap to
        # STOP it. The recording continues regardless of button state
        # (no need to keep finger down). _oc_record_start_mono drives
        # the on-screen timer; auto-capped at OC_MAX_RECORD_S in tick().
        self._oc_recording = False
        self._oc_record_start_mono = None
        self.OC_MAX_RECORD_S = 300.0  # 5 min cap — auto-stop + save
        self.OC_MIN_RECORD_S = 0.4    # below this, discard (probably misclick)

    # ── Helpers ──────────────────────────────────────────────────

    def wake_display(self):
        self.last_interaction = time.monotonic()
        if not self.backlight_on:
            self.ctx.board.set_backlight(self.brightness_level)
            self.backlight_on = True

    def _go_home(self):
        self.app_state = "HOME"
        self.stt_initiated_from = None
        self.ctx.led.set_state("stt_idle")

    def _save_note(self, text):
        save_note(text, NOTES_DIR, self.ctx.cortex_db,
                  source="voice", note_type="voice",
                  session_id=self.ctx.cortex.get_active_session_id())

    def _count_today_notes(self):
        today = datetime.now().strftime("%Y%m%d")
        try:
            return len(glob.glob(os.path.join(NOTES_DIR, f"{today}_*.txt")))
        except OSError:
            return 0

    def _count_today_recs(self):
        today = datetime.now().strftime("%Y%m%d")
        try:
            return len(glob.glob(os.path.join(RECORDING_DIR, f"{today}_*.wav")))
        except OSError:
            return 0

    def get_cortex_context(self):
        """Build context dict for Cortex protocol status responses."""
        legacy_state = self.app_state
        if legacy_state in ("HOME", "MENU", "CONFIRM_SHUTDOWN", "GAME_PONG"):
            legacy_state = "STT_IDLE"
        disk_used, disk_free, _ = self.ctx.recorder.get_disk_usage()
        ctx_dict = {
            "app_state": legacy_state,
            "uptime_s": round(time.monotonic(), 1),
            "disk_free_gb": round(disk_free / 1_073_741_824, 1),
            "ble_connected": self.ctx.ble.is_connected(),
        }
        if self.ctx.battery is not None:
            ctx_dict["battery"] = self.ctx.battery.get_status()
        return ctx_dict

    def _send_cortex_response(self, response):
        if len(response.encode("utf-8")) > 480:
            for chunk in self.ctx.cortex.chunk_response(response):
                self.ctx.ble.send(chunk)
        else:
            self.ctx.ble.send(response)

    # ── Gamepad ──────────────────────────────────────────────────

    def handle_gamepad(self, action):
        """Process a gamepad input action string."""
        self.wake_display()
        menu = self.ctx.menu
        logger = self.ctx.logger

        if self.app_state == "HOME":
            if action in ("start", "down"):
                menu.open()
                self.app_state = "MENU"
                logger.log("menu_opened")
            elif action == "a":
                self.handle_short_press()

        elif self.app_state == "MENU":
            if action in ("up", "down"):
                menu.navigate(action)
            elif action == "a":
                result = menu.navigate("select")
                if result:
                    self._handle_menu_action(result)
            elif action in ("b", "start"):
                menu.navigate("back")
                if not menu.is_open():
                    self.app_state = "HOME"
                    logger.log("menu_closed")

        elif self.app_state == "STT_LISTENING":
            if action == "b":
                self.ctx.stt.stop_listening()
                self._go_home()
                logger.log("stt_listening_cancelled")

        elif self.app_state == "NOTE_TAKING":
            if action == "b":
                self.ctx.stt.stop_listening()
                self._go_home()

        elif self.app_state == "SETTING_ADJUST":
            if action in ("right", "up"):
                step = 10
                self.setting_value = min(self.setting_max,
                                         self.setting_value + step)
                self._apply_setting()
            elif action in ("left", "down"):
                step = 10
                self.setting_value = max(self.setting_min,
                                         self.setting_value - step)
                self._apply_setting()
            elif action in ("b", "start", "a"):
                self._go_home()

        elif self.app_state == "INFO_SCREEN":
            if action in ("b", "a", "start"):
                self._go_home()

        elif self.app_state == "CONFIRM_SHUTDOWN":
            if action == "a":
                self.shutdown()
            elif action == "b":
                self._go_home()

        elif self.app_state == "RECORDING":
            if action == "b":
                self.handle_short_press()
            elif action in ("start", "a"):
                self.handle_long_press()

        elif self.app_state == "PAUSED":
            if action == "a":
                self.handle_short_press()
            elif action in ("b", "start"):
                self.handle_long_press()

        elif self.app_state == "GAME_PONG":
            if action in ("start", "b"):
                self.pong_game = None
                self.pong_ai = None
                self.pong_renderer = None
                self.loop_interval = 1.0 / DISPLAY_UPDATE_HZ
                self._go_home()
                self.ctx.logger.log("game_pong_exit")
            elif action == "a" and self.pong_game and self.pong_game.game_over:
                self.pong_game.reset()
                self.ctx.logger.log("game_pong_restart")

    # ── Menu ─────────────────────────────────────────────────────

    def _handle_menu_action(self, action):
        menu = self.ctx.menu
        logger = self.ctx.logger
        stt = self.ctx.stt
        menu.close()

        if action == "take_note":
            self.stt_initiated_from = "take_note"
            stt.start_listening()
            self.note_text = ""
            self.note_start_mono = time.monotonic()
            self.app_state = "NOTE_TAKING"
            self.ctx.led.set_state("note_taking")
            logger.log("note_started")

        elif action == "record":
            session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            logger.set_session(session_id)
            self.ctx.recorder.start()
            self.ctx.recorder.check_new_segment()
            self.app_state = "RECORDING"
            self.ctx.led.set_state("recording")
            logger.log("mic_started")

        elif action == "confirm_shutdown":
            self.app_state = "CONFIRM_SHUTDOWN"

        elif action == "info_screen":
            self.app_state = "INFO_SCREEN"
            self._populate_info("Notes & Recs", [
                ("Today's Notes", str(self._count_today_notes())),
                ("Today's Recs", str(self._count_today_recs())),
            ])

        elif action == "adj_brightness":
            self.setting_name = "Brightness"
            self.setting_value = self.brightness_level
            self.setting_min = 10
            self.setting_max = 100
            self.app_state = "SETTING_ADJUST"

        elif action == "adj_volume":
            self.setting_name = "Volume"
            self.setting_value = self.volume_level
            self.setting_min = 0
            self.setting_max = 100
            self.app_state = "SETTING_ADJUST"

        elif action == "adj_hz":
            self.setting_name = "Display Hz"
            self.setting_value = self.display_hz
            self.setting_min = 1
            self.setting_max = 10
            self.app_state = "SETTING_ADJUST"

        elif action == "wifi_info":
            self.app_state = "INFO_SCREEN"
            self._populate_wifi_info()

        elif action == "ble_info":
            self.app_state = "INFO_SCREEN"
            ble = self.ctx.ble
            lines = [
                ("Status", "Connected" if ble.is_connected() else "Disconnected"),
                ("BLE Client", "Enabled" if BLE_ENABLED else "Disabled"),
            ]
            if ble.is_connected():
                lines.append(("Device", ble.device_name))
                lines.append(("Address", ble.get_address()))
                lines.append(("MTU", str(ble.mtu_size)))
            self._populate_info("BLE Info", lines)

        elif action == "about":
            self.app_state = "INFO_SCREEN"
            lines = [
                ("Cortex Core", "v1.0"),
                ("Display", f"{DISPLAY_WIDTH}x{DISPLAY_HEIGHT}"),
                ("API Port", str(HTTP_PORT)),
                ("Gamepad", "Connected" if self.ctx.gamepad.is_connected() else "Scanning"),
            ]
            self._populate_info("About", lines)

        elif action == "game_pong" and GAMES_ENABLED:
            from games.pong import PongGame
            from games.pong_ai import PongAI
            from games.pong_renderer import PongRenderer
            self.pong_game = PongGame()
            self.pong_ai = PongAI(model_path=PONG_MODEL_PATH)
            self.pong_renderer = PongRenderer(self.ctx.display)
            self.pong_last_tick = time.monotonic()
            self.loop_interval = 1.0 / GAME_FPS
            self.app_state = "GAME_PONG"
            logger.log("game_pong_start", {"ai_mode": self.pong_ai.mode})

        else:
            self._go_home()

    # ── Settings / Info helpers ──────────────────────────────────

    def _load_settings(self):
        """Load saved settings from disk, returning defaults on any error."""
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_settings(self):
        """Persist current settings to disk."""
        data = {
            "brightness": self.brightness_level,
            "volume": self.volume_level,
            "display_hz": self.display_hz,
        }
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    def _apply_setting(self):
        """Apply the current setting value to hardware and save to disk."""
        if self.setting_name == "Brightness":
            self.brightness_level = self.setting_value
            self.ctx.board.set_backlight(self.setting_value)
        elif self.setting_name == "Volume":
            self.volume_level = self.setting_value
            try:
                subprocess.run(
                    ["amixer", "-c", "wm8960soundcard", "sset",
                     "Speaker", f"{self.setting_value}%"],
                    capture_output=True, timeout=3,
                )
            except Exception:
                pass
        elif self.setting_name == "Display Hz":
            self.display_hz = self.setting_value
            self.loop_interval = 1.0 / max(1, self.setting_value)
        self._save_settings()

    def _populate_info(self, title, lines):
        """Set up info screen data. lines = [(label, value), ...]."""
        self._info_title = title
        self._info_lines = lines

    def _populate_wifi_info(self):
        """Gather WiFi info for display."""
        import socket
        lines = []
        # IP address
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "No connection"
        lines.append(("IP", ip))

        # Hostname
        try:
            lines.append(("Host", socket.gethostname()))
        except Exception:
            pass

        # SSID via nmcli or iwgetid
        ssid = ""
        try:
            r = subprocess.run(
                ["iwgetid", "-r"],
                capture_output=True, text=True, timeout=3,
            )
            ssid = r.stdout.strip()
        except Exception:
            pass
        lines.append(("SSID", ssid or "Unknown"))

        # Signal strength
        try:
            r = subprocess.run(
                ["iwconfig", "wlan0"],
                capture_output=True, text=True, timeout=3,
            )
            for part in r.stdout.split():
                if part.startswith("level="):
                    lines.append(("Signal", part.split("=")[1] + " dBm"))
                    break
        except Exception:
            pass

        lines.append(("API", f":{HTTP_PORT}"))
        self._populate_info("WiFi Info", lines)

    # ── Button callbacks (Slice 12.1 redesign) ───────────────────
    #
    # Companion-mode button vocabulary (single physical button):
    #
    #   press                          → any_press → companion_press_start()
    #                                    starts arecord + status="pressed"
    #                                    + yellow LED + force-render
    #
    #   release before SHORT_PRESS_MAX  → short_press → handle_short_press()
    #                                    discards audio + INSTANT flag
    #                                    (no recording — just timestamp)
    #
    #   held past SHORT_PRESS_MAX       → hold_threshold → handle_hold_threshold()
    #                                    status flips to "recording-armed"
    #                                    + LED green + force-render. Audio
    #                                    keeps recording.
    #
    #   release after threshold         → hold_release → handle_hold_release()
    #                                    stops arecord, transcribe + route
    #                                    (journal OR overseer chat by wake
    #                                    phrase). Runs on background thread.
    #
    #   held past SHUTDOWN_PRESS_MS     → shutdown → sm.shutdown()
    #
    # Why this is different from Slice 12: the v12 design fired long_press
    # AT 1.5s while still held, which truncated audio mid-hold. v12.1 uses
    # release timing only (hold_release sees actual hold duration).

    def _force_render(self):
        """Push an immediate frame to the LCD so the user sees state
        transitions within ~10ms instead of waiting up to 125ms for
        the next tick. Safe to call from any thread (PIL render is
        fast, ~15ms typical on Pi Zero 2W)."""
        try:
            t0 = time.monotonic()
            state = self.build_display_state()
            if state is not None:
                self.ctx.display.render(state)
            dbg.event("RENDER force",
                      ms=round((time.monotonic() - t0) * 1000, 1))
        except Exception as _re:
            logging.getLogger("states.companion").debug(
                "force-render failed: %s", _re)

    def companion_press_start(self):
        """any_press hook — Slice 12.1.2: now a no-op in toggle-record mode.

        Kept as a method (still wired in main._on_any_press for wake-only
        press logic) but does nothing here. All recording logic moved to
        _toggle_record_start / _toggle_record_stop, dispatched from
        handle_short_press on the actual button RELEASE."""
        return

    def _toggle_record_start(self):
        """Start a new recording. Sets _oc_recording, starts arecord,
        flips screen to the active-recording timer view, LED green."""
        if self._oc_busy:
            dbg.event("RECORD start IGNORED busy")
            return
        try:
            self._oc_audio.start()
            self._oc_recording = True
            self._oc_record_start_mono = time.monotonic()
            self.companion_status = "recording"
            self.companion_routed = ""
            self.companion_message = ""
            self.last_interaction = time.monotonic()
            self.ctx.board.set_rgb(0, 200, 0)  # solid green
            self.ctx.logger.log("companion_record_start")
            dbg.event("RECORD start")
            self._force_render()
        except Exception as e:
            self._oc_recording = False
            self._oc_record_start_mono = None
            self.companion_status = ""
            self.ctx.board.set_rgb(0, 0, 0)
            logging.getLogger("states.companion").exception(
                "_toggle_record_start failed: %s", e)
            dbg.event("RECORD start FAILED", error=str(e))
            self._force_render()

    def _toggle_record_stop(self, reason="user-tap"):
        """Stop the active recording. Get the wav, clear state, kick off
        the transcribe+route thread (which reuses _companion_run_hold)."""
        if not self._oc_recording:
            return
        wav, actual_dur = self._oc_audio.stop()
        self._oc_recording = False
        elapsed = (
            time.monotonic() - self._oc_record_start_mono
            if self._oc_record_start_mono else 0
        )
        self._oc_record_start_mono = None
        wav_bytes = (os.path.getsize(wav)
                     if wav and os.path.exists(wav) else 0)
        dbg.event("RECORD stop", reason=reason,
                  elapsed=round(elapsed, 2),
                  actual_dur=round(actual_dur, 2),
                  wav_bytes=wav_bytes)
        # Discard misclick / very-short recordings (no point transcribing
        # 200ms of audio).
        if not wav or actual_dur < self.OC_MIN_RECORD_S:
            self.companion_status = ""
            self.companion_message = ""
            self.ctx.board.set_rgb(0, 0, 0)
            self.ctx.logger.log("companion_record_too_short", {
                "duration_s": round(actual_dur, 2),
                "reason": reason,
            })
            self._force_render()
            return
        # Hand off to the transcribe+route worker thread (same path as
        # the old hold-release flow).
        threading.Thread(
            target=self._companion_run_hold,
            args=(wav, actual_dur),
            name="oc-toggle", daemon=True,
        ).start()
        self.ctx.logger.log("companion_record_stop", {
            "duration_s": round(actual_dur, 1),
            "reason": reason,
        })

    # Slice 12.1.2: hold_threshold and hold_release callbacks are no
    # longer wired to the physical button (see main.py). The toggle
    # model uses release timing only — short_press fires on every release
    # < SHORT_PRESS_MAX_MS, and that's all we need.
    def handle_hold_threshold(self):
        """No-op stub — kept for any caller still referencing it."""
        return

    def handle_hold_release(self, duration_s: float):
        """No-op stub — kept for any caller still referencing it."""
        return

    def _companion_run_flag(self):
        """Run the instant-flag flow on a background thread.

        Slice 12.1: no audio capture — the flag is a timestamp marker.
        See overseer_companion.handle_flag_moment for the rationale."""
        try:
            self._oc_busy = True
            self.companion_status = "flag-saved"
            self.companion_routed = "flag-moment"
            self.ctx.board.set_rgb(220, 0, 180)  # magenta flash
            dbg.event("STATE -> flag-saved")
            self._force_render()

            result = oc.handle_flag_moment(
                on_state=lambda s: setattr(self, "companion_status", s),
            )
            self.companion_message = (
                f"flagged at {time.strftime('%H:%M:%S')}"
            )
            self.ctx.logger.log("flag_moment", {
                "ok": result.get("ok"),
                "error": result.get("error"),
            })
            dbg.event("FLAG saved", ok=result.get("ok"))
            self._force_render()
            time.sleep(1.5)  # short hold — flag is instant
        finally:
            self._oc_busy = False
            self.companion_status = ""
            self.companion_message = ""
            self.companion_routed = ""
            self.ctx.board.set_rgb(0, 0, 0)
            self._force_render()

    def _companion_run_hold(self, wav_path, duration):
        """Run the hold-release flow on a background thread.

        Slice 12.1: now force-renders after every status change so the
        user sees 'transcribing…' / 'asking overseer…' / 'saved' within
        ~50ms of each transition."""
        try:
            self._oc_busy = True

            def _state_cb(name):
                self.companion_status = name
                if name == "transcribing":
                    self.ctx.board.set_rgb_fade(0, 100, 200, 200)
                elif name == "asking-overseer":
                    self.ctx.board.set_rgb_fade(0, 60, 220, 200)
                elif name == "reply-speaking":
                    self.ctx.board.set_rgb_fade(0, 200, 200, 300)  # cyan
                elif name in ("saving-journal", "saved"):
                    self.ctx.board.set_rgb_fade(0, 220, 80, 300)
                elif name == "idle":
                    self.ctx.board.set_rgb(0, 0, 0)
                dbg.event("STATE -> " + name)
                self._force_render()

            result = oc.handle_hold_release(
                wav_path, duration, on_state=_state_cb)
            routed = result.get("routed", "")
            self.companion_routed = routed
            if routed == "overseer-chat":
                self.companion_message = (result.get("reply") or
                                           "(empty reply)")
            else:
                self.companion_message = (result.get("transcript") or
                                           "(silent journal)")
            self.ctx.logger.log("hold_release", {
                "ok": result.get("ok"),
                "routed": routed,
                "transcript_chars": len(result.get("transcript", "") or ""),
                "reply_chars": len(result.get("reply", "") or ""),
                "stt_backend": result.get("stt_backend"),
                "duration_s": round(duration, 1),
                "error": result.get("error"),
            })
            dbg.event("HOLD complete", routed=routed,
                      ok=result.get("ok"),
                      stt=result.get("stt_backend"))
            # Hold the completion screen for 3 seconds so user can read it
            self.companion_status = (
                "saved" if routed != "overseer-chat" else "reply-speaking"
            )
            self._force_render()
            time.sleep(3.0)
        finally:
            self._oc_busy = False
            self.companion_status = ""
            self.companion_message = ""
            self.companion_routed = ""
            self.ctx.board.set_rgb(0, 0, 0)
            self._force_render()

    def handle_short_press(self):
        self.wake_display()
        logger = self.ctx.logger
        stt = self.ctx.stt
        recorder = self.ctx.recorder

        # Slice 12.1 wake-press: if this press was just a screen wake,
        # swallow the release and do nothing else.
        if self._oc_wake_only_press:
            self._oc_wake_only_press = False
            dbg.event("SHORT-PRESS swallowed (wake-only press)")
            return

        # Slice 12.1.2 toggle-record:
        #   - not recording → start recording (timer + green LED)
        #   - already recording → stop, transcribe, route, save
        # The transcribe+route work happens on a background thread so the
        # button handler returns immediately.
        if (
            self.companion_mode
            and _OC_AVAILABLE
            and self.app_state == "HOME"
            and self._oc_audio is not None
        ):
            # If a previous transcribe/save is still running (rare —
            # only if the user taps very fast right after stopping),
            # ignore the press to avoid clobbering state.
            if self._oc_busy:
                dbg.event("SHORT-PRESS IGNORED busy")
                return
            if self._oc_recording:
                self._toggle_record_stop(reason="user-tap")
            else:
                self._toggle_record_start()
            return

        if self.app_state == "HOME":
            stt.start_listening()
            self.app_state = "STT_LISTENING"
            self.ctx.led.set_state("stt_listening")
            logger.log("stt_listening_started")

        elif self.app_state == "MENU":
            result = self.ctx.menu.navigate("select")
            if result:
                self._handle_menu_action(result)

        elif self.app_state == "STT_LISTENING":
            stt.stop_listening()
            self._go_home()
            logger.log("stt_listening_cancelled")

        elif self.app_state == "NOTE_TAKING":
            remaining = stt.get_all_finals()
            if remaining:
                self.note_text = f"{self.note_text} {remaining}".strip() if self.note_text else remaining
            stt.stop_listening()
            dur = round(time.monotonic() - self.note_start_mono, 1) if self.note_start_mono else 0
            self._save_note(self.note_text)
            logger.log("note_saved", {"text": self.note_text[:500], "duration_s": dur})
            self._go_home()
            self.note_text = ""
            self.note_start_mono = None

        elif self.app_state == "CONFIRM_SHUTDOWN":
            self._go_home()

        elif self.app_state == "RECORDING":
            recorder.stop()
            self.app_state = "PAUSED"
            self.ctx.led.set_state("paused")
            logger.log("mic_paused", {
                "elapsed_seconds": round(recorder.get_session_elapsed(), 1),
            })
            self.pause_start_mono = time.monotonic()

        elif self.app_state == "PAUSED":
            pause_dur = round(time.monotonic() - self.pause_start_mono, 1) if self.pause_start_mono else 0
            recorder.start()
            recorder.check_new_segment()
            self.app_state = "RECORDING"
            self.ctx.led.set_state("recording")
            logger.log("mic_resumed", {"pause_duration_seconds": pause_dur})
            self.pause_start_mono = None

    def handle_long_press(self):
        """Legacy long_press handler — companion-mode path moved to
        handle_hold_release in Slice 12.1. The physical button no longer
        wires to long_press in companion mode (see main.py). This is
        still called by gamepad action handlers in legacy RECORDING/
        PAUSED app states.
        """
        self.wake_display()

        if self.app_state in ("RECORDING", "PAUSED"):
            elapsed = round(self.ctx.recorder.get_session_elapsed(), 1)
            seg_count = self.ctx.recorder.get_segment_count()
            self.ctx.recorder.stop()
            self.ctx.recorder.reset_session()
            self._go_home()
            self.ctx.logger.log("mic_stopped", {
                "total_segments": seg_count,
                "total_elapsed_seconds": elapsed,
            })
            self.ctx.logger.set_session(None)
            self.pause_start_mono = None

    def shutdown(self):
        ctx = self.ctx
        ctx.gamepad.cleanup()
        if ctx.http_server:
            ctx.http_server.shutdown()
        if ctx.battery is not None:
            ctx.battery.stop()
        ctx.ble.stop()
        ctx.stt.stop_listening()
        ctx.recorder.stop()
        self.app_state = "SHUTDOWN"
        ctx.led.set_state("shutdown")
        ctx.logger.log("shutdown", {
            "reason": "long_hold",
            "uptime_seconds": round(time.monotonic(), 1),
        })
        ctx.cortex_db.close()
        ctx.logger.close()
        ctx.display.render(DisplayState(
            app_state="HOME",
            time_str=time.strftime("%H:%M"),
        ))
        ctx.board.set_backlight(BACKLIGHT_BRIGHTNESS)
        time.sleep(1)
        ctx.board.set_backlight(0)
        ctx.board.set_rgb(0, 0, 0)
        ctx.stt.cleanup()
        ctx.board.cleanup()
        subprocess.run(["sudo", "shutdown", "-h", "now"])
        sys.exit(0)

    # ── Per-frame tick ───────────────────────────────────────────

    def tick(self):
        """Run all per-frame logic: games, STT, recording, BLE, display."""
        ctx = self.ctx
        stt = ctx.stt
        logger = ctx.logger
        recorder = ctx.recorder

        # LED animation
        ctx.led.tick()

        # ── Game tick ────────────────────────────────────────────
        if (self.app_state == "GAME_PONG" and self.pong_game
                and not self.pong_game.game_over):
            now = time.monotonic()
            dt = now - self.pong_last_tick
            self.pong_last_tick = now

            held = ctx.gamepad.get_held_directions()
            player_dir = 0
            if "up" in held:
                player_dir = -1
            elif "down" in held:
                player_dir = 1
            self.pong_game.move_paddle(1, player_dir, dt)

            ai_action = self.pong_ai.get_action(self.pong_game.get_state())
            self.pong_game.move_paddle(2, ai_action, dt)
            self.pong_game.tick(dt)

        # ── STT listening ────────────────────────────────────────
        if self.app_state == "STT_LISTENING":
            final = stt.get_final()
            if final:
                lower = final.lower()
                if "note" in lower:
                    logger.log("stt_command", {"text": "note", "raw": final})
                    self.note_text = ""
                    self.note_start_mono = time.monotonic()
                    self.app_state = "NOTE_TAKING"
                    ctx.led.set_state("note_taking")
                    logger.log("note_started")
                elif "record" in lower:
                    logger.log("stt_command", {"text": "record", "raw": final})
                    stt.stop_listening()
                    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                    logger.set_session(session_id)
                    recorder.start()
                    recorder.check_new_segment()
                    self.app_state = "RECORDING"
                    ctx.led.set_state("recording")
                    logger.log("mic_started")
                    self.pause_start_mono = None

            if self.app_state == "STT_LISTENING" and stt.seconds_since_voice() > STT_LISTEN_TIMEOUT_S:
                stt.stop_listening()
                self._go_home()
                logger.log("stt_listening_timeout")

        # ── Note taking ──────────────────────────────────────────
        elif self.app_state == "NOTE_TAKING":
            final = stt.get_all_finals()
            if final:
                self.note_text = f"{self.note_text} {final}".strip() if self.note_text else final

            if stt.seconds_since_voice() > STT_NOTE_SILENCE_S:
                partial = stt.get_partial()
                if partial:
                    self.note_text = f"{self.note_text} {partial}".strip() if self.note_text else partial
                stt.stop_listening()
                dur = round(time.monotonic() - self.note_start_mono, 1) if self.note_start_mono else 0
                text = self.note_text.strip()
                if not text:
                    # No speech detected — go home silently
                    logger.log("stt_empty_timeout", {"mode": "note", "duration_s": dur})
                    self._go_home()
                else:
                    self._save_note(text)
                    logger.log("note_saved", {"text": text[:500], "duration_s": dur})
                    self._go_home()
                self.note_text = ""
                self.note_start_mono = None

        # ── Recording watchdog ───────────────────────────────────
        elif self.app_state == "RECORDING":
            if not recorder.is_alive():
                exit_code = recorder.proc.returncode if recorder.proc else None
                logger.log("watchdog_restart", {"reason": "arecord_exited", "exit_code": exit_code})
                ctx.led.set_state("error")
                time.sleep(0.5)
                recorder.start()
                recorder.check_new_segment()
                ctx.led.set_state("recording")

            new_seg = recorder.check_new_segment()
            if new_seg:
                logger.rotate_now()
                logger.log("segment_started", {
                    "segment_file": new_seg,
                    "segment_number": recorder.get_segment_count(),
                })

        # ── BLE messages ─────────────────────────────────────────
        if self.app_state != "GAME_PONG":
            for msg in ctx.ble.poll_messages():
                logger.log("ble_received", {"raw": msg[:500]})
                self.wake_display()
                try:
                    if msg.startswith("CMD:") or msg.startswith("CHUNK:"):
                        if msg.startswith("CMD:"):
                            cmd_name = msg[4:].split(":")[0].strip().lower()
                            if cmd_name in ("start_recording", "stop_recording"):
                                self._handle_local_command(cmd_name)
                                continue

                        response = ctx.cortex.handle_message(
                            msg, context=self.get_cortex_context(),
                        )
                        if response is not None:
                            self._send_cortex_response(response)
                            logger.log("cortex_response", {
                                "cmd": msg[:80], "rsp": response[:200],
                            })
                    else:
                        self._save_note(msg)
                        logger.log("ble_text_note", {"text": msg[:500]})
                except Exception as e:
                    logger.log("ble_error", {"error": str(e), "raw": msg[:200]})

        # ── Slice 12.1.2: toggle-record auto-cap ─────────────────
        # If the user starts a recording and forgets / walks away, we
        # don't want to record forever. Auto-stop + save at the cap.
        if self.companion_mode and self._oc_recording and self._oc_record_start_mono:
            elapsed = time.monotonic() - self._oc_record_start_mono
            if elapsed > self.OC_MAX_RECORD_S:
                dbg.event("RECORD auto-stop (cap)",
                          elapsed=round(elapsed, 1))
                self._toggle_record_stop(reason="max-duration-cap")

        # ── Display auto-off ─────────────────────────────────────
        # Slice 12: keep the screen awake while a companion flow is
        # in progress (recording / transcribing / asking-overseer /
        # speaking). Otherwise the 60s timeout can blank the LCD
        # mid-flow on a long overseer reply.
        if self.companion_mode and (
            self.companion_status or self._oc_busy or self._oc_recording
        ):
            self.last_interaction = time.monotonic()
        if self.backlight_on and (time.monotonic() - self.last_interaction > DISPLAY_TIMEOUT_S):
            ctx.board.set_backlight(0)
            self.backlight_on = False

    # ── Display ──────────────────────────────────────────────────

    def build_display_state(self):
        """Build a DisplayState for the current frame."""
        ctx = self.ctx
        recorder = ctx.recorder

        if self.app_state == "GAME_PONG" and self.pong_renderer and self.pong_game:
            self.pong_renderer.render(
                self.pong_game.get_state(),
                ai_mode=self.pong_ai.mode if self.pong_ai else "rule",
            )
            return None  # Game renders directly

        disk_used, disk_free, _ = recorder.get_disk_usage()

        state = DisplayState(
            app_state=self.app_state,
            time_str=time.strftime("%H:%M"),
            note_count=self._count_today_notes(),
            rec_count=self._count_today_recs(),
            disk_free=disk_free,
            remaining_hours=recorder.get_remaining_hours(),
            ble_connected=ctx.ble.is_connected(),
            battery_info=(ctx.battery.get_status()
                          if ctx.battery is not None else None),
            ble_info=BLEInfo(
                name=ctx.ble.device_name,
                address=ctx.ble.get_address(),
                mtu=ctx.ble.mtu_size,
                rssi=ctx.ble.rssi,
            ) if ctx.ble.is_connected() else None,
            stt_partial=ctx.stt.get_partial() if ctx.stt.is_listening() else "",
            note_text=self.note_text,
            idle_since=self.last_interaction,
            session_elapsed=recorder.get_session_elapsed(),
            segment_elapsed=recorder.get_segment_elapsed(),
            segment_count=recorder.get_segment_count(),
            disk_used=disk_used,
        )

        if self.app_state == "MENU" and ctx.menu.is_open():
            items, cursor = ctx.menu.get_visible_items()
            state.menu_items = items
            state.menu_cursor = cursor
            state.menu_breadcrumb = ctx.menu.get_breadcrumb()

        # ── Slice 12: companion-mode digest into the LCD frame ───
        if self.companion_mode:
            state.companion_status = self.companion_status or ""
            state.companion_message = getattr(
                self, "companion_message", "") or ""
            state.companion_routed = getattr(
                self, "companion_routed", "") or ""
            # Slice 12.1.2: live timer for the toggle-record screen
            if self._oc_recording and self._oc_record_start_mono:
                state.recording_elapsed_s = (
                    time.monotonic() - self._oc_record_start_mono
                )
            else:
                state.recording_elapsed_s = 0
            try:
                state.journal_queue_depth = oc.journal_queue_depth()
            except Exception:
                state.journal_queue_depth = 0
            nw = getattr(self, "notification_watcher", None)
            if nw is not None:
                with nw.lock:
                    digest = dict(nw.last_digest)
                    preview_items = list(nw.pending_preview)
                state.overseer_loop_running = digest.get(
                    "loop_running", False)
                state.overseer_last_tick = digest.get("last_tick_at", "")
                state.overseer_notes_total = digest.get("notes_total", 0)
                state.overseer_unread = digest.get(
                    "notifications_unread", 0)
                state.overseer_pending = digest.get("pending_review", 0)
                if preview_items:
                    first = preview_items[0]
                    title = (first.get("title") or "").strip()
                    body = (first.get("body") or "").strip()
                    state.notification_preview = (
                        f"{title}: {body}" if title and body
                        else (title or body or "")
                    )

        # Settings / Info screen data
        if self.app_state == "SETTING_ADJUST":
            state.setting_name = self.setting_name
            state.setting_value = self.setting_value
            state.setting_min = self.setting_min
            state.setting_max = self.setting_max

        if self.app_state == "INFO_SCREEN":
            state.info_title = getattr(self, "_info_title", "")
            state.info_lines = getattr(self, "_info_lines", [])

        return state

    # ── Local BLE commands ───────────────────────────────────────

    def _handle_local_command(self, cmd):
        ble = self.ctx.ble
        logger = self.ctx.logger
        if cmd == "start_recording":
            if self.app_state == "RECORDING":
                ble.send("ERR:start_recording:already recording")
                logger.log("ble_command", {"cmd": "start_recording", "result": "err:already_recording"})
            else:
                self.handle_short_press()
                logger.log("ble_command", {"cmd": "start_recording", "result": "ack"})
                ble.send("ACK:start_recording")
        elif cmd == "stop_recording":
            if self.app_state in ("RECORDING", "PAUSED"):
                self.handle_long_press()
                logger.log("ble_command", {"cmd": "stop_recording", "result": "ack"})
                ble.send("ACK:stop_recording")
            else:
                ble.send("ERR:stop_recording:not recording")
                logger.log("ble_command", {"cmd": "stop_recording", "result": "err:not_recording"})
