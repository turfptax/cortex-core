# 8BitDo Micro Gamepad Setup (Pi Zero 2 W)

Controller: **8BitDo Micro Bluetooth Gamepad**
Pi: **Raspberry Pi Zero 2 W** (BCM43436 Bluetooth, BlueZ 5.82)

## Hardware Info

| Field | Value |
|-------|-------|
| MAC (Android mode) | `E4:17:D8:68:7C:ED` |
| MAC (Keyboard mode) | `E4:17:D8:BE:80:BB` |
| OUI | `E4:17:D8` (8BitDo) |
| Device class | `0x002508` (Gamepad) |
| Modalias | `usb:v2DC8p9020d0100` |
| Input devices | `/dev/input/event2`, `/dev/input/js0` |

## Mode Selection

The 8BitDo Micro has a physical mode switch. **Use Android (D-input) mode** for the Pi.

| Mode | Protocol | Works with Pi? |
|------|----------|----------------|
| Android (D-input) | Classic Bluetooth (BR/EDR) HID | Yes |
| Keyboard | BLE proprietary (UUID `0x0000ff10`) | No (pairing fails) |
| Switch | Nintendo Switch protocol | No |

## Prerequisites (One-Time Pi Setup)

### 1. Load HIDP kernel module

```bash
sudo modprobe hidp
echo "hidp" | sudo tee /etc/modules-load.d/bluetooth-hid.conf
```

### 2. Configure BlueZ input plugin

Edit `/etc/bluetooth/input.conf`:

```ini
[General]
UserspaceHID=true
ClassicBondedOnly=false
```

- `UserspaceHID=true` — Use UHID (userspace HID). Kernel HIDP (`false`) does not work with this controller on BlueZ 5.82.
- `ClassicBondedOnly=false` — Allow HID connections from paired-but-not-bonded devices. Without this, BlueZ rejects the gamepad's HID connection.

### 3. Restart bluetooth

```bash
sudo systemctl restart bluetooth
```

## Pairing Procedure

### Step 1: Put controller in Android pairing mode

1. Set the physical mode switch to **Android/D-input**
2. Hold the pairing button until the LED flashes rapidly

### Step 2: Discover via BR/EDR scan

`bluetoothctl scan on` only finds BLE devices on the Pi Zero 2 W. Use `hcitool` for Classic Bluetooth discovery:

```bash
sudo hcitool inq --length=10
```

Look for `E4:17:D8:68:7C:ED` with class `0x002508`.

### Step 3: Create HCI connection + pair

`bluetoothctl` can't see BR/EDR-only devices until an HCI connection exists:

```bash
sudo hcitool cc E4:17:D8:68:7C:ED
```

### Step 4: Pair, trust, connect

```bash
bluetoothctl pair E4:17:D8:68:7C:ED
bluetoothctl trust E4:17:D8:68:7C:ED
bluetoothctl connect E4:17:D8:68:7C:ED
```

### Step 5: Verify

```bash
# Should show event2 and js0
ls /dev/input/

# Should show "8BitDo Micro gamepad"
cat /proc/bus/input/devices | grep -A5 "8BitDo"

# Test button inputs
sudo evtest /dev/input/event2
```

## Quick Reconnect

After initial pairing, the controller is trusted. To reconnect:

1. Turn on the controller (Android mode)
2. Run: `bluetoothctl connect E4:17:D8:68:7C:ED`

Or it may auto-connect if the controller was the last device paired.

## Full Input Map

### Buttons

| Code | evdev Name | Physical | Action |
|------|-----------|----------|--------|
| 304 | BTN_SOUTH | A | `a` |
| 305 | BTN_EAST | B | `b` |
| 307 | BTN_NORTH | X | `x` |
| 308 | BTN_WEST | Y | `y` |
| 310 | BTN_TL | L1 | `l` |
| 311 | BTN_TR | R1 | `r` |
| 312 | BTN_TL2 | L2 | `l2` |
| 313 | BTN_TR2 | R2 | `r2` |
| 314 | BTN_SELECT | Star | `select` |
| 315 | BTN_START | Heart | `start` |

### Axes

| Code | evdev Name | Usage | Range |
|------|-----------|-------|-------|
| 0 | ABS_X | D-Pad Left/Right | 0=Left, 127=Center, 255=Right |
| 1 | ABS_Y | D-Pad Up/Down | 0=Up, 127=Center, 255=Down |
| 9 | ABS_GAS | L2 analog | 0-255 |
| 10 | ABS_BRAKE | R2 analog | 0-255 |

**Note:** D-Pad maps to `ABS_X`/`ABS_Y` (analog stick axes), NOT `ABS_HAT0X`/`ABS_HAT0Y`. The gamepad.py handler uses threshold-based detection (< 50 = pressed, > 200 = pressed, 127 = center).

## Bonding Limitation

The 8BitDo Micro **does not support Bluetooth bonding** in Android mode. Pairing state is lost on every disconnect/power cycle. Cortex Core handles this automatically via `gamepad.py`:

1. On boot (or gamepad disconnect), scans for the 8BitDo MAC via BR/EDR inquiry
2. If found, runs the full pair -> trust -> connect cycle
3. Retries every 5 seconds until the controller appears

To use: just turn on the controller in Android mode and Cortex Core will auto-connect within ~10 seconds.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `hcitool inq` finds nothing | Ensure controller is in Android mode and LED is flashing |
| `bluetoothctl pair` says "not available" | Run `sudo hcitool cc <MAC>` first to create HCI connection |
| Paired but no `/dev/input/event*` | Check `UserspaceHID=true` in input.conf, restart bluetooth |
| "Rejected connection from !bonded device" | Set `ClassicBondedOnly=false` in input.conf |
| `bluetoothctl scan on` doesn't find it | Normal -- use `sudo hcitool inq` for Classic BT devices |
