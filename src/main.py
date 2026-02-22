#!/usr/bin/env python3
"""Wearable Audio Recorder + Cortex Core — main entry point.

State machine (STT mode is the primary/default interface):
    STT_IDLE -> (short press) -> STT_LISTENING (push-to-talk)
    STT_LISTENING -> voice "note" -> NOTE_TAKING
    STT_LISTENING -> voice "record" -> RECORDING
    STT_LISTENING -> silence timeout -> STT_IDLE
    STT_LISTENING -> (short press) -> STT_IDLE (cancel)
    NOTE_TAKING -> silence timeout or (short press) -> save -> STT_IDLE
    RECORDING -> (short press) -> PAUSED
    PAUSED -> (short press) -> RECORDING
    RECORDING/PAUSED -> (long press) -> STT_IDLE
    Any -> (5s hold) -> shutdown

Cortex protocol:
    CMD:<command>:<json> messages arrive via BLE and are processed by
    CortexProtocol, which persists data to SQLite and returns
    RSP:/ACK:/ERR: responses.
"""

import glob
import os
import sys
import time
import subprocess
from datetime import datetime

# Add WhisPlay driver and app source to path
_app_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/Whisplay/Driver"))
sys.path.insert(0, _app_dir)

from WhisPlay import WhisPlayBoard

import json

from config import (
    DISPLAY_TIMEOUT_S, BACKLIGHT_BRIGHTNESS, DISPLAY_UPDATE_HZ,
    STT_LISTEN_TIMEOUT_S, STT_NOTE_SILENCE_S, NOTES_DIR,
    CORTEX_DB_PATH,
)
from recorder import Recorder
from display import Display
from button import ButtonHandler
from led import LEDManager
from logger import ActivityLogger
from stt import STTEngine
from ble_client import BLEClient
from cortex_db import CortexDB
from cortex_protocol import CortexProtocol


def main():
    # Initialize hardware
    board = WhisPlayBoard()
    board.set_backlight(BACKLIGHT_BRIGHTNESS)

    recorder = Recorder()
    display = Display(board)
    button = ButtonHandler(board)
    led = LEDManager(board)
    logger = ActivityLogger()

    # Load Vosk model (one-time, ~500ms)
    stt = STTEngine()

    # Cortex database and protocol handler
    cortex_db = CortexDB(CORTEX_DB_PATH)
    cortex = CortexProtocol(cortex_db)

    # BLE client for ESP32 KeyMaster bridge
    def _on_ble_connect(address):
        logger.log("ble_connected", {"device": "KeyMaster", "address": address})
        led.ble_flash("connect")

    def _on_ble_disconnect():
        logger.log("ble_disconnected", {})
        led.ble_flash("disconnect")

    ble = BLEClient(on_connect=_on_ble_connect, on_disconnect=_on_ble_disconnect)
    ble.start()

    app_state = "STT_IDLE"
    last_interaction = time.monotonic()
    backlight_on = True
    pause_start_mono = None
    note_text = ""          # accumulated note transcript
    note_start_mono = None  # when note-taking started

    def wake_display():
        nonlocal last_interaction, backlight_on
        last_interaction = time.monotonic()
        if not backlight_on:
            board.set_backlight(BACKLIGHT_BRIGHTNESS)
            backlight_on = True

    def _count_today_notes():
        """Count note files saved today."""
        today = datetime.now().strftime("%Y%m%d")
        try:
            return len(glob.glob(os.path.join(NOTES_DIR, f"{today}_*.txt")))
        except OSError:
            return 0

    def _count_today_recs():
        """Count WAV recording segments created today."""
        today = datetime.now().strftime("%Y%m%d")
        try:
            from config import RECORDING_DIR
            return len(glob.glob(os.path.join(RECORDING_DIR, f"{today}_*.wav")))
        except OSError:
            return 0

    def _save_note(text):
        """Save note text to a file and to Cortex DB."""
        if not text.strip():
            return
        os.makedirs(NOTES_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(NOTES_DIR, f"{stamp}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.strip() + "\n")
        except OSError:
            pass
        # Dual-write to Cortex DB
        try:
            cortex_db.insert_note(
                content=text.strip(),
                source="voice",
                note_type="voice",
                session_id=cortex.get_active_session_id(),
            )
        except Exception:
            pass

    def _get_cortex_context():
        """Build runtime context dict for Cortex protocol status responses."""
        disk_used, disk_free, _ = recorder.get_disk_usage()
        return {
            "app_state": app_state,
            "uptime_s": round(time.monotonic(), 1),
            "disk_free_gb": round(disk_free / 1_073_741_824, 1),
            "ble_connected": ble.is_connected(),
        }

    def _send_cortex_response(response):
        """Send a Cortex protocol response, chunking if needed."""
        if len(response.encode("utf-8")) > 480:
            for chunk in cortex.chunk_response(response):
                ble.send(chunk)
        else:
            ble.send(response)

    def on_short_press():
        nonlocal app_state, pause_start_mono, note_text, note_start_mono
        wake_display()

        if app_state == "STT_IDLE":
            # Start push-to-talk listening
            stt.start_listening()
            app_state = "STT_LISTENING"
            led.set_state("stt_listening")
            logger.log("stt_listening_started")

        elif app_state == "STT_LISTENING":
            # Cancel listening, go back to idle
            stt.stop_listening()
            app_state = "STT_IDLE"
            led.set_state("stt_idle")
            logger.log("stt_listening_cancelled")

        elif app_state == "NOTE_TAKING":
            # Save note and go back to idle
            # Grab any remaining final results
            remaining = stt.get_all_finals()
            if remaining:
                note_text = f"{note_text} {remaining}".strip() if note_text else remaining
            stt.stop_listening()
            dur = round(time.monotonic() - note_start_mono, 1) if note_start_mono else 0
            _save_note(note_text)
            logger.log("note_saved", {
                "text": note_text[:500],
                "duration_s": dur,
            })
            note_text = ""
            note_start_mono = None
            app_state = "STT_IDLE"
            led.set_state("stt_idle")

        elif app_state == "RECORDING":
            # Pause recording
            recorder.stop()
            app_state = "PAUSED"
            led.set_state("paused")
            logger.log("mic_paused", {
                "elapsed_seconds": round(recorder.get_session_elapsed(), 1),
            })
            pause_start_mono = time.monotonic()

        elif app_state == "PAUSED":
            # Resume recording
            pause_dur = round(time.monotonic() - pause_start_mono, 1) if pause_start_mono else 0
            recorder.start()
            recorder.check_new_segment()
            app_state = "RECORDING"
            led.set_state("recording")
            logger.log("mic_resumed", {
                "pause_duration_seconds": pause_dur,
            })
            pause_start_mono = None

    def on_long_press():
        nonlocal app_state, pause_start_mono
        wake_display()
        if app_state in ("RECORDING", "PAUSED"):
            elapsed = round(recorder.get_session_elapsed(), 1)
            seg_count = recorder.get_segment_count()
            recorder.stop()
            recorder.reset_session()
            app_state = "STT_IDLE"  # Return to STT home, not old IDLE
            led.set_state("stt_idle")
            logger.log("mic_stopped", {
                "total_segments": seg_count,
                "total_elapsed_seconds": elapsed,
            })
            logger.set_session(None)
            pause_start_mono = None

    def on_shutdown():
        nonlocal app_state
        # Stop everything
        ble.stop()
        stt.stop_listening()
        recorder.stop()
        app_state = "SHUTDOWN"
        led.set_state("shutdown")
        logger.log("shutdown", {
            "reason": "long_hold",
            "uptime_seconds": round(time.monotonic(), 1),
        })
        cortex_db.close()
        logger.close()
        # Show brief shutdown on display
        display.render({
            "app_state": "STT_IDLE",
            "time_str": time.strftime("%H:%M"),
            "note_count": 0,
            "rec_count": 0,
            "disk_free": 0,
            "remaining_hours": 0,
        })
        board.set_backlight(BACKLIGHT_BRIGHTNESS)
        time.sleep(1)
        board.set_backlight(0)
        board.set_rgb(0, 0, 0)
        stt.cleanup()
        board.cleanup()
        subprocess.run(["sudo", "shutdown", "-h", "now"])
        sys.exit(0)

    button.on("short_press", on_short_press)
    button.on("long_press", on_long_press)
    button.on("shutdown", on_shutdown)
    button.on("any_press", wake_display)

    led.set_state("stt_idle")
    logger.log("app_started", {"cortex_db": CORTEX_DB_PATH})

    # Main loop
    loop_interval = 1.0 / DISPLAY_UPDATE_HZ
    try:
        while True:
            loop_start = time.monotonic()

            # Check button hold state
            button.check_held()

            # LED animation
            led.tick()

            # ---- STT_LISTENING: poll for voice commands ----
            if app_state == "STT_LISTENING":
                final = stt.get_final()
                if final:
                    # Check for commands
                    lower = final.lower()
                    if "note" in lower:
                        # Transition to NOTE_TAKING
                        logger.log("stt_command", {"text": "note", "raw": final})
                        note_text = ""
                        note_start_mono = time.monotonic()
                        app_state = "NOTE_TAKING"
                        led.set_state("note_taking")
                        logger.log("note_started")
                    elif "record" in lower:
                        # Transition to RECORDING
                        logger.log("stt_command", {"text": "record", "raw": final})
                        stt.stop_listening()
                        # Start recording session
                        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                        logger.set_session(session_id)
                        recorder.start()
                        recorder.check_new_segment()
                        app_state = "RECORDING"
                        led.set_state("recording")
                        logger.log("mic_started")
                        pause_start_mono = None
                    # else: unrecognized command, stay listening

                # Silence timeout
                if app_state == "STT_LISTENING" and stt.seconds_since_voice() > STT_LISTEN_TIMEOUT_S:
                    stt.stop_listening()
                    app_state = "STT_IDLE"
                    led.set_state("stt_idle")
                    logger.log("stt_listening_timeout")

            # ---- NOTE_TAKING: accumulate transcript, check silence ----
            elif app_state == "NOTE_TAKING":
                final = stt.get_all_finals()
                if final:
                    note_text = f"{note_text} {final}".strip() if note_text else final

                # Silence timeout -> auto-save
                if stt.seconds_since_voice() > STT_NOTE_SILENCE_S:
                    # Grab partial as well
                    partial = stt.get_partial()
                    if partial:
                        note_text = f"{note_text} {partial}".strip() if note_text else partial
                    stt.stop_listening()
                    dur = round(time.monotonic() - note_start_mono, 1) if note_start_mono else 0
                    _save_note(note_text)
                    logger.log("note_saved", {
                        "text": note_text[:500],
                        "duration_s": dur,
                    })
                    note_text = ""
                    note_start_mono = None
                    app_state = "STT_IDLE"
                    led.set_state("stt_idle")

            # ---- RECORDING: watchdog + segment detection ----
            elif app_state == "RECORDING":
                if not recorder.is_alive():
                    exit_code = recorder.proc.returncode if recorder.proc else None
                    logger.log("watchdog_restart", {
                        "reason": "arecord_exited",
                        "exit_code": exit_code,
                    })
                    led.set_state("error")
                    time.sleep(0.5)
                    recorder.start()
                    recorder.check_new_segment()
                    led.set_state("recording")

                new_seg = recorder.check_new_segment()
                if new_seg:
                    logger.rotate_now()
                    logger.log("segment_started", {
                        "segment_file": new_seg,
                        "segment_number": recorder.get_segment_count(),
                    })

            # ---- BLE: poll incoming messages ----
            for msg in ble.poll_messages():
                logger.log("ble_received", {"raw": msg[:500]})
                wake_display()
                try:
                    if msg.startswith("CMD:") or msg.startswith("CHUNK:"):
                        # Device-local commands that need app_state access
                        if msg.startswith("CMD:"):
                            cmd_name = msg[4:].split(":")[0].strip().lower()
                            if cmd_name in ("start_recording", "stop_recording"):
                                _handle_local_command(
                                    cmd_name, ble, app_state, recorder,
                                    logger, on_short_press, on_long_press,
                                )
                                continue

                        # All CMD:/CHUNK: → Cortex protocol handler
                        response = cortex.handle_message(
                            msg, context=_get_cortex_context(),
                        )
                        if response is not None:
                            _send_cortex_response(response)
                            logger.log("cortex_response", {
                                "cmd": msg[:80],
                                "rsp": response[:200],
                            })
                    else:
                        # Plain text — save as voice note (file + DB)
                        _save_note(msg)
                        logger.log("ble_text_note", {"text": msg[:500]})

                except Exception as e:
                    logger.log("ble_error", {"error": str(e), "raw": msg[:200]})

            # Display auto-off
            if backlight_on and (time.monotonic() - last_interaction > DISPLAY_TIMEOUT_S):
                board.set_backlight(0)
                backlight_on = False

            # Build state dict and render display
            if app_state in ("STT_IDLE", "STT_LISTENING", "NOTE_TAKING"):
                disk_used, disk_free, _ = recorder.get_disk_usage()
                render_state = {
                    "app_state": app_state,
                    "time_str": time.strftime("%H:%M"),
                    "stt_partial": stt.get_partial() if stt.is_listening() else "",
                    "note_text": note_text,
                    "note_count": _count_today_notes(),
                    "rec_count": _count_today_recs(),
                    "disk_free": disk_free,
                    "remaining_hours": recorder.get_remaining_hours(),
                    "ble_connected": ble.is_connected(),
                    "ble_info": {
                        "name": ble.device_name,
                        "address": ble.get_address(),
                        "mtu": ble.mtu_size,
                        "rssi": ble.rssi,
                    } if ble.is_connected() else None,
                }
            else:
                disk_used, disk_free, _ = recorder.get_disk_usage()
                render_state = {
                    "app_state": app_state,
                    "time_str": time.strftime("%H:%M"),
                    "session_elapsed": recorder.get_session_elapsed(),
                    "segment_elapsed": recorder.get_segment_elapsed(),
                    "segment_count": recorder.get_segment_count(),
                    "disk_used": disk_used,
                    "disk_free": disk_free,
                    "remaining_hours": recorder.get_remaining_hours(),
                }

            # Only render to SPI if backlight is on
            if backlight_on:
                display.render(render_state)

            # Sleep for remainder of frame interval
            elapsed = time.monotonic() - loop_start
            sleep_time = loop_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        pass
    finally:
        ble.stop()
        stt.stop_listening()
        stt.cleanup()
        recorder.stop()
        cortex_db.close()
        logger.log("app_stopped")
        logger.close()
        board.set_backlight(0)
        board.set_rgb(0, 0, 0)
        board.cleanup()




def _handle_local_command(cmd, ble, app_state, recorder, logger,
                          on_short_press, on_long_press):
    """Handle device-local BLE commands that need app_state access."""
    if cmd == "start_recording":
        if app_state == "RECORDING":
            ble.send("ERR:start_recording:already recording")
            logger.log("ble_command", {"cmd": "start_recording", "result": "err:already_recording"})
        else:
            on_short_press()
            logger.log("ble_command", {"cmd": "start_recording", "result": "ack"})
            ble.send("ACK:start_recording")
    elif cmd == "stop_recording":
        if app_state in ("RECORDING", "PAUSED"):
            on_long_press()
            logger.log("ble_command", {"cmd": "stop_recording", "result": "ack"})
            ble.send("ACK:stop_recording")
        else:
            ble.send("ERR:stop_recording:not recording")
            logger.log("ble_command", {"cmd": "stop_recording", "result": "err:not_recording"})


if __name__ == "__main__":
    main()
