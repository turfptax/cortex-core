"""Gamepad input handler using evdev for 8BitDo Micro controller.

Non-blocking poll-based input. Scans /dev/input/ for matching device,
reads D-pad and button events, and returns action strings.

The 8BitDo Micro doesn't support Bluetooth bonding, so pairing is lost
on every disconnect. When no gamepad is found, _try_pair_gamepad() runs
the full BR/EDR discovery -> pair -> trust -> connect cycle automatically.

Falls back gracefully to no-op when no gamepad is connected.
"""

import subprocess
import time

try:
    import evdev
    from evdev import ecodes
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False

from config import (
    GAMEPAD_ENABLED, GAMEPAD_DEVICE_NAME, GAMEPAD_MAC,
    GAMEPAD_REPEAT_DELAY_S, GAMEPAD_REPEAT_RATE_S,
)


# Button code -> action string mapping
# 8BitDo Micro in Android/D-input mode
BUTTON_MAP = {
    304: "a",       # BTN_SOUTH (A)
    305: "b",       # BTN_EAST (B)
    307: "x",       # BTN_NORTH (X)
    308: "y",       # BTN_WEST (Y)
    310: "l",       # BTN_TL (L1)
    311: "r",       # BTN_TR (R1)
    312: "l2",      # BTN_TL2 (L2)
    313: "r2",      # BTN_TR2 (R2)
    314: "select",  # BTN_SELECT (star)
    315: "start",   # BTN_START (heart)
}

# D-pad threshold for ABS_X / ABS_Y (0-255 range, center=127)
_DPAD_LOW = 50    # below this = left/up
_DPAD_HIGH = 200  # above this = right/down


class GamepadInput:
    """Non-blocking gamepad reader using evdev.

    Usage:
        gp = GamepadInput()
        # In main loop:
        for action in gp.poll():
            handle(action)  # "up", "down", "left", "right", "a", "b", etc.
    """

    def __init__(self):
        self.device = None
        self._last_scan = 0
        self._scan_interval = 10.0  # seconds between reconnect attempts

        # D-pad state for auto-repeat
        self._dpad_held = {}  # direction -> monotonic time when pressed
        self._dpad_last_repeat = {}  # direction -> last repeat fire time

        if GAMEPAD_ENABLED and HAS_EVDEV:
            self._scan_for_device()

    def _scan_for_device(self):
        """Scan /dev/input/ for an 8BitDo device. Auto-pairs if not found."""
        self._last_scan = time.monotonic()
        if not HAS_EVDEV:
            return

        try:
            devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
            for dev in devices:
                if GAMEPAD_DEVICE_NAME.lower() in dev.name.lower():
                    dev.grab()  # exclusive access
                    self.device = dev
                    print(f"Gamepad connected: {dev.name} ({dev.path})")
                    return
                dev.close()
        except Exception as e:
            print(f"Gamepad scan error: {e}")

        # No device found -- try Bluetooth auto-pair (8BitDo doesn't bond,
        # so we need to re-pair after every power cycle)
        if self.device is None and GAMEPAD_MAC:
            self._try_pair_gamepad()

            # Re-scan after pairing to pick up the new device
            if self.device is None:
                time.sleep(1)
                try:
                    devices = [evdev.InputDevice(path)
                               for path in evdev.list_devices()]
                    for dev in devices:
                        if GAMEPAD_DEVICE_NAME.lower() in dev.name.lower():
                            dev.grab()
                            self.device = dev
                            print(f"Gamepad connected: {dev.name} ({dev.path})")
                            return
                        dev.close()
                except Exception as e:
                    print(f"Gamepad post-pair scan error: {e}")

    def _try_pair_gamepad(self):
        """Attempt to connect the 8BitDo via BR/EDR Bluetooth.

        The 8BitDo Micro in Android mode uses Classic Bluetooth (not BLE).
        It doesn't bond, so pairing is lost on every power cycle.

        Requires /etc/bluetooth/input.conf: ClassicBondedOnly=false
        (otherwise BlueZ rejects HID from non-bonded devices).

        Strategy:
          1. Ensure adapter is pairable
          2. Fast path: bluetoothctl connect (works if already paired/trusted)
          3. Scan via bluetoothctl to register device in BlueZ
          4. Rapid pair → connect (must be fast, controller times out in ~30s)

        Total budget: ~20 seconds.
        """
        mac = GAMEPAD_MAC
        try:
            # Ensure adapter is pairable (resets to off after bluetooth restart)
            subprocess.run(
                ["bluetoothctl", "pairable", "on"],
                capture_output=True, text=True, timeout=3,
            )

            # ── Fast path: try direct connect first (~5s) ──
            print(f"Gamepad: trying direct connect to {mac}...")
            result = subprocess.run(
                ["bluetoothctl", "connect", mac],
                capture_output=True, text=True, timeout=6,
            )
            out = (result.stdout + result.stderr).strip()
            if "Connected: yes" in out or "Connection successful" in out:
                print(f"  Direct connect succeeded!")
                time.sleep(2)  # wait for UHID input device
                return

            # ── Discovery path: scan via bluetoothctl so BlueZ registers it ──
            print(f"  Direct connect failed, scanning via bluetoothctl...")

            found = self._bluetoothctl_scan_for_mac(mac, timeout=8)
            if not found:
                print(f"  Controller not found in scan")
                return

            print(f"  Controller found via BlueZ scan! Rapid pair+connect...")

            # Trust first (idempotent, fast)
            subprocess.run(
                ["bluetoothctl", "trust", mac],
                capture_output=True, text=True, timeout=3,
            )

            # Pair — this creates the ACL link. The controller is connected
            # at L2CAP level after this succeeds.
            result = subprocess.run(
                ["bluetoothctl", "pair", mac],
                capture_output=True, text=True, timeout=10,
            )
            pair_out = (result.stdout + result.stderr).strip()
            print(f"  pair: {pair_out[:100]}")

            if "Pairing successful" not in pair_out and "Already Paired" not in pair_out:
                print(f"  Pairing failed, aborting")
                return

            # Connect immediately — triggers HID profile connection.
            # Must happen fast while controller is still awake from pairing.
            result = subprocess.run(
                ["bluetoothctl", "connect", mac],
                capture_output=True, text=True, timeout=8,
            )
            conn_out = (result.stdout + result.stderr).strip()
            print(f"  connect: {conn_out[:100]}")

            # Give UHID time to create the input device
            time.sleep(2)
            print(f"Gamepad pair attempt complete for {mac}")

        except subprocess.TimeoutExpired:
            print(f"Gamepad pair timed out for {mac}")
        except Exception as e:
            print(f"Gamepad auto-pair error: {e}")

    def _bluetoothctl_scan_for_mac(self, mac, timeout=8):
        """Run bluetoothctl scan and watch for a specific MAC address.

        Uses bluetoothctl's own scanner so the device gets registered
        in BlueZ's D-Bus device database — this is critical for
        pair/trust/connect to work afterwards.

        Returns True if the MAC was seen, False if timeout.
        """
        mac_lower = mac.lower()
        try:
            # Launch bluetoothctl in interactive-ish mode
            proc = subprocess.Popen(
                ["bluetoothctl", "--timeout", str(timeout), "scan", "on"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            deadline = time.monotonic() + timeout
            found = False

            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                line_lower = line.strip().lower()
                if mac_lower in line_lower:
                    print(f"  Scan found: {line.strip()}")
                    found = True
                    break

            # Stop scanning
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

            # Even if we found it during scan, give BlueZ a moment to
            # fully register the device
            if found:
                time.sleep(0.5)

            return found

        except Exception as e:
            print(f"  Scan error: {e}")
            return False

    def is_connected(self):
        """Check if a gamepad device is currently available."""
        return self.device is not None

    def poll(self):
        """Non-blocking poll for gamepad events. Returns list of action strings.

        Actions: "up", "down", "left", "right", "a", "b", "x", "y",
                 "l", "r", "start", "select"
        """
        if not GAMEPAD_ENABLED or not HAS_EVDEV:
            return []

        # Auto-reconnect if no device
        if self.device is None:
            now = time.monotonic()
            if now - self._last_scan > self._scan_interval:
                self._scan_for_device()
            return []

        actions = []

        # Read all pending events (non-blocking)
        try:
            while True:
                event = self.device.read_one()
                if event is None:
                    break

                # D-pad -- 8BitDo Micro reports D-pad as ABS_X/ABS_Y
                # (0-255 range, 127=center) instead of HAT0X/HAT0Y.
                # Also handle HAT axes for compatibility with other controllers.
                if event.type == ecodes.EV_ABS:
                    if event.code in (ecodes.ABS_X, ecodes.ABS_HAT0X):
                        if event.code == ecodes.ABS_X:
                            # Analog axis: threshold-based
                            if event.value < _DPAD_LOW:
                                actions.append("left")
                                self._dpad_pressed("left")
                                self._dpad_released("right")
                            elif event.value > _DPAD_HIGH:
                                actions.append("right")
                                self._dpad_pressed("right")
                                self._dpad_released("left")
                            else:
                                self._dpad_released("left")
                                self._dpad_released("right")
                        else:
                            # HAT axis: -1/0/1
                            if event.value == -1:
                                actions.append("left")
                                self._dpad_pressed("left")
                            elif event.value == 1:
                                actions.append("right")
                                self._dpad_pressed("right")
                            else:
                                self._dpad_released("left")
                                self._dpad_released("right")
                    elif event.code in (ecodes.ABS_Y, ecodes.ABS_HAT0Y):
                        if event.code == ecodes.ABS_Y:
                            if event.value < _DPAD_LOW:
                                actions.append("up")
                                self._dpad_pressed("up")
                                self._dpad_released("down")
                            elif event.value > _DPAD_HIGH:
                                actions.append("down")
                                self._dpad_pressed("down")
                                self._dpad_released("up")
                            else:
                                self._dpad_released("up")
                                self._dpad_released("down")
                        else:
                            if event.value == -1:
                                actions.append("up")
                                self._dpad_pressed("up")
                            elif event.value == 1:
                                actions.append("down")
                                self._dpad_pressed("down")
                            else:
                                self._dpad_released("up")
                                self._dpad_released("down")

                # Face buttons and shoulder buttons
                elif event.type == ecodes.EV_KEY:
                    if event.value == 1:  # key down
                        action = BUTTON_MAP.get(event.code)
                        if action:
                            actions.append(action)

        except OSError:
            # Device disconnected
            print("Gamepad disconnected")
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
            self._dpad_held.clear()
            self._dpad_last_repeat.clear()
            return actions

        # Auto-repeat for held D-pad directions
        actions.extend(self._check_dpad_repeat())

        return actions

    def _dpad_pressed(self, direction):
        """Mark a D-pad direction as held."""
        now = time.monotonic()
        self._dpad_held[direction] = now
        self._dpad_last_repeat[direction] = now

    def _dpad_released(self, direction):
        """Mark a D-pad direction as released."""
        self._dpad_held.pop(direction, None)
        self._dpad_last_repeat.pop(direction, None)

    def _check_dpad_repeat(self):
        """Generate repeat events for held D-pad directions."""
        if not self._dpad_held:
            return []

        now = time.monotonic()
        repeats = []
        for direction, press_time in list(self._dpad_held.items()):
            held_duration = now - press_time
            if held_duration < GAMEPAD_REPEAT_DELAY_S:
                continue
            last_repeat = self._dpad_last_repeat.get(direction, press_time)
            if now - last_repeat >= GAMEPAD_REPEAT_RATE_S:
                repeats.append(direction)
                self._dpad_last_repeat[direction] = now

        return repeats

    def get_held_directions(self):
        """Return set of currently held D-pad directions.

        Use this for continuous game input instead of event-based polling,
        which fires discrete press + auto-repeat events (wrong for games).
        """
        return set(self._dpad_held.keys())

    def cleanup(self):
        """Release the device."""
        if self.device is not None:
            try:
                self.device.ungrab()
                self.device.close()
            except Exception:
                pass
            self.device = None
