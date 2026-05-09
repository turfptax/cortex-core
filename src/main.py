#!/usr/bin/env python3
"""Cortex Core — main entry point.

Initializes hardware and services, then delegates all state management
to StateManager (states.py). See states.py for the full state machine.
"""

import logging
import os
import subprocess
import sys
import time

# Configure logging so all module loggers (pet, heartbeat, stt, etc.) output
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-12s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)

# Add WhisPlay driver and app source to path
_app_dir = os.path.dirname(os.path.abspath(__file__))
_user_home = os.path.dirname(os.path.dirname(_app_dir))
sys.path.insert(0, os.path.join(_user_home, "Whisplay", "Driver"))
sys.path.insert(0, _app_dir)
# Slice 11 (2026-05-09): the entire pet plugin (pet.py, heartbeat.py,
# body_shell.py, tamagotchi_display.py, sprite.py, voxel_animator.py
# and pet_db / pet_config) was extracted out of cortex-core into the
# standalone repo https://github.com/turfptax/cortex-pet. cortex-core
# is now memory + overseer only. If you want the pet on this device,
# clone cortex-pet and point plugin discovery at it (out-of-tree
# plugin model — TBD in plugins_runtime).
#
# Note: this commit removes the pet code; main.py still references
# TamagotchiDisplay below as a placeholder. A follow-up slice replaces
# it with a generic display abstraction so cortex-core can run headless
# or behind any display plugin.

from WhisPlay import WhisPlayBoard

from config import (
    BACKLIGHT_BRIGHTNESS,
    CORTEX_DB_PATH, HTTP_ENABLED, GAMEPAD_ENABLED,
    BATTERY_ENABLED, BATTERY_POLL_INTERVAL_S, BATTERY_I2C_BUS,
    BLE_ENABLED, PLUGINS_ENABLED,
)
from recorder import Recorder
# Slice 12: cortex-core uses the in-tree `Display` class (PIL-rendered
# 240x280 RGB565 frames blitted via WhisPlayBoard.draw_image). The pet's
# TamagotchiDisplay went with cortex-pet; that's fine because Display
# was always the lower-level renderer the pet wrapped.
from display import Display
from button import ButtonHandler
from led import LEDManager
from logger import ActivityLogger
from stt import STTEngine
from ble_client import BLEClient
from cortex_db import CortexDB
from cortex_protocol import CortexProtocol
from gamepad import GamepadInput
from menu import MenuSystem, build_menu_tree
from states import StateContext, StateManager


def main():
    # ── Hardware init ────────────────────────────────────────────
    board = WhisPlayBoard()
    board.set_backlight(BACKLIGHT_BRIGHTNESS)  # temporary default until StateManager loads

    recorder = Recorder()
    display = Display(board)
    button = ButtonHandler(board)
    led = LEDManager(board)
    logger = ActivityLogger()
    gamepad = GamepadInput()

    from sound import SoundManager
    sound = SoundManager()
    sound.play("boot")

    menu = MenuSystem(build_menu_tree())
    stt = STTEngine()
    cortex_db = CortexDB(CORTEX_DB_PATH)

    # Battery monitor (init early — heartbeat needs it)
    battery = None
    if BATTERY_ENABLED:
        try:
            from battery import BatteryMonitor
            battery = BatteryMonitor(
                bus_num=BATTERY_I2C_BUS,
                poll_interval=BATTERY_POLL_INTERVAL_S,
            )
            battery.start()
        except Exception as e:
            print("Battery monitor failed to initialize: {}".format(e))

    # ── Plugins ──────────────────────────────────────────────────
    # The pet plugin's on_load() does the one-time migration of pet rows
    # from cortex.db into pet.db, then constructs PetEngine and starts
    # Heartbeat. Slice 2c2c1: cortex_protocol no longer needs pet/heartbeat
    # references — the plugin serves the full pet surface via its own
    # /plugins/pet/* HTTP routes.
    plugin_registry = None
    if PLUGINS_ENABLED:
        try:
            from plugins_runtime import PluginRegistry
            plugin_registry = PluginRegistry(
                sound_manager=sound,
                battery=battery,
                cortex_db_path=CORTEX_DB_PATH,
            ).discover_and_load()
        except Exception as e:
            print("Plugin runtime failed to initialize: {}".format(e))

    cortex = CortexProtocol(cortex_db, plugin_registry=plugin_registry)

    http_server = None
    if HTTP_ENABLED:
        try:
            from http_server import start_http_server
            # Slice 2c2b: hand the HTTP server any plugin routes the
            # registry collected. Each is mounted at
            # /plugins/<plugin_name>/<route_path>.
            plugin_routes = (plugin_registry.get_http_routes()
                             if plugin_registry is not None else [])
            _http_thread, http_server = start_http_server(
                cortex_protocol=cortex,
                context_fn=None,  # will be set after StateManager init
                plugin_routes=plugin_routes,
            )
        except Exception as e:
            print("HTTP server failed to start: {}".format(e))

    # BLE client
    def _on_ble_connect(address):
        logger.log("ble_connected", {"device": "KeyMaster", "address": address})
        led.ble_flash("connect")

    def _on_ble_disconnect():
        logger.log("ble_disconnected", {})
        led.ble_flash("disconnect")

    ble = BLEClient(on_connect=_on_ble_connect, on_disconnect=_on_ble_disconnect)
    if BLE_ENABLED:
        ble.start()
    else:
        print("BLE disabled (no Bluetooth hardware)")

    # ── State manager ────────────────────────────────────────────
    # pet + heartbeat are passed to CortexProtocol above (Hub still uses
    # the pet CMD handlers there; slice 2c2 will move them to plugin
    # HTTP routes). StateContext no longer references pet — the device's
    # main UI is memory-system-only after slice 2c1b.
    ctx = StateContext(
        board=board, recorder=recorder, display=display, button=button,
        led=led, logger=logger, stt=stt, ble=ble, cortex_db=cortex_db,
        cortex=cortex, gamepad=gamepad, menu=menu, sound=sound,
        http_server=http_server, battery=battery,
    )
    sm = StateManager(ctx)

    # Apply saved settings (brightness/volume) now that StateManager loaded them
    board.set_backlight(sm.brightness_level)
    try:
        subprocess.run(
            ["amixer", "-c", "wm8960soundcard", "sset",
             "Speaker", f"{sm.volume_level}%"],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass

    # Wire HTTP server context function now that sm exists
    if http_server:
        http_server.context_fn = sm.get_cortex_context

    # Wire button callbacks
    button.on("short_press", sm.handle_short_press)
    button.on("long_press", sm.handle_long_press)
    button.on("shutdown", sm.shutdown)

    # Slice 12: kick off the notification watcher (best-effort).
    # Polls /plugins/overseer/status every 30s for unread-count
    # delta. State machine's tick() will read pending_preview and
    # render to LCD/LED on next frame.
    try:
        from overseer_companion import NotificationWatcher
        sm.notification_watcher = NotificationWatcher(poll_interval_s=30.0)
        sm.notification_watcher.start()
    except Exception as _nw_err:
        sm.notification_watcher = None

    def _on_any_press():
        # Slice 12: any_press both wakes the display AND starts the
        # AudioCapture for companion mode. The capture is canceled
        # (via stop+discard) by handle_short_press if the release fires
        # the tap path; or stopped+routed by handle_long_press if the
        # release fires the hold path.
        sm.wake_display()
        sm.companion_press_start()
    button.on("any_press", _on_any_press)

    led.set_state("stt_idle")
    logger.log("app_started", {
        "cortex_db": CORTEX_DB_PATH,
        "gamepad": gamepad.is_connected(),
    })

    # ── Main loop ────────────────────────────────────────────────
    try:
        while True:
            loop_start = time.monotonic()

            button.check_held()

            for action in gamepad.poll():
                sm.handle_gamepad(action)

            sm.tick()

            # Render display
            if sm.backlight_on:
                render_state = sm.build_display_state()
                if render_state is not None:
                    display.render(render_state)

            elapsed = time.monotonic() - loop_start
            sleep_time = sm.loop_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        pass
    finally:
        # Plugin unload handles its own pet engine + heartbeat cleanup.
        if plugin_registry is not None:
            plugin_registry.unload_all()
        gamepad.cleanup()
        if http_server:
            http_server.shutdown()
        if battery is not None:
            battery.stop()
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


if __name__ == "__main__":
    main()
