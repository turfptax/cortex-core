# BLE Protocol Specification: Pi Zero ↔ ESP32-S3 KeyMaster Bridge

## Overview

The ESP32-S3 "KeyMaster" acts as a **bidirectional BLE bridge** between a computer (connected via USB serial) and a Raspberry Pi Zero 2 W (connected via BLE). The Pi Zero runs a wearable recorder/log server that stores incoming data for AI-assisted processing.

```
Computer ←→ [USB Serial] ←→ ESP32-S3 ←→ [BLE] ←→ Pi Zero 2 W
              (CDC/UART)     KeyMaster    (GATT)    (bleak client)
```

## BLE GATT Service

The ESP32 is the **BLE peripheral** (server). The Pi Zero is the **BLE central** (client).

| Item | Value |
|------|-------|
| Advertising Name | `KeyMaster` |
| Service UUID | `a0e1b2c3-d4e5-f6a7-b8c9-0a1b2c3d4e50` |
| TX Characteristic | `a0e1b2c3-d4e5-f6a7-b8c9-0a1b2c3d4e51` |
| TX Properties | READ, NOTIFY |
| TX Direction | ESP32 → Pi Zero (data from the computer to Pi) |
| RX Characteristic | `a0e1b2c3-d4e5-f6a7-b8c9-0a1b2c3d4e52` |
| RX Properties | WRITE |
| RX Direction | Pi Zero → ESP32 (data from Pi to the computer) |
| Pairing | "Just Works" (NoInputNoOutput, bond=True) |
| Advertising Interval | 250ms |

**Note:** "TX" and "RX" are named from the ESP32's perspective (TX = ESP32 transmits to client).

## Message Framing

All messages are **newline-delimited UTF-8 strings**:
- Each message ends with `\n` (0x0A)
- Max message length: **512 bytes** (before newline)
- Encoding: UTF-8
- No binary data, no length prefix, no escape characters

### BLE MTU and Chunking

BLE has a limited MTU (typically 20-247 bytes per notification/write). Messages longer than the negotiated MTU must be handled:

- **Sending (TX notify):** If a message + `\n` exceeds the MTU, the ESP32 should split it across multiple notification events. The Pi Zero buffers incoming bytes and reconstructs messages by splitting on `\n`.
- **Receiving (RX write):** The Pi Zero sends complete messages (including `\n`) in a single `write_gatt_char()` call. BlueZ handles GATT-level fragmentation automatically for writes.

**Important:** The Pi Zero client buffers all incoming notification bytes and only processes complete lines (split on `\n`). Partial messages across notifications are handled correctly.

## Message Types

Messages are auto-detected by their first character:

### 1. JSON Message — starts with `{`

Structured data with a `type` field for routing.

**Computer → Pi Zero (via ESP32 TX):**
```
{"type":"note","text":"Meeting with Sarah at 3pm"}\n
{"type":"bookmark","url":"https://example.com","title":"Reference doc"}\n
{"type":"sensor","temp":22.5,"humidity":45}\n
{"type":"status_request"}\n
```

**Pi Zero → Computer (via ESP32 RX):**
```
{"type":"status_response","state":"STT_IDLE","battery":"unknown","disk_free_gb":21.7,"remaining_hours":195.2,"uptime_s":3600.5}\n
{"type":"note_saved","text":"Meeting notes...","timestamp":"2026-02-22T14:30:00"}\n
```

All JSON messages are logged on the Pi Zero. Messages with `"type":"status_request"` trigger an automatic status response.

### 2. Command — starts with `CMD:`

Simple control commands and their responses.

**Computer → Pi Zero:**
```
CMD:ping\n
CMD:status\n
CMD:start_recording\n
CMD:stop_recording\n
```

**Pi Zero → Computer (responses):**
```
CMD:pong\n
{"type":"status_response",...}\n
CMD:ack:start_recording\n
CMD:err:already_recording\n
CMD:ack:stop_recording\n
CMD:err:not_recording\n
CMD:err:unknown:somecommand\n
```

**Command reference:**

| Command | Description | Success Response | Error Response |
|---------|-------------|------------------|----------------|
| `CMD:ping` | Connectivity check | `CMD:pong` | — |
| `CMD:status` | Request device status | JSON `status_response` | — |
| `CMD:start_recording` | Start audio recording | `CMD:ack:start_recording` | `CMD:err:already_recording` |
| `CMD:stop_recording` | Stop audio recording | `CMD:ack:stop_recording` | `CMD:err:not_recording` |

### 3. Plain Text — anything else

Any message that doesn't start with `{` or `CMD:` is treated as a plain text note. It is saved as a `.txt` file on the Pi Zero and logged.

```
Remember to buy groceries\n
Call dentist tomorrow\n
```

## ESP32 Firmware Requirements

The ESP32 needs to act as a **transparent USB serial ↔ BLE bridge**:

### USB Serial → BLE (Computer to Pi)

1. Read lines from USB serial (CDC/UART)
2. Forward each complete line as a TX notification to the connected BLE client
3. Add `\n` terminator if not already present
4. If the line exceeds the BLE MTU, send it across multiple notification events (the Pi buffers and reassembles)

### BLE → USB Serial (Pi to Computer)

1. When the Pi writes to the RX characteristic, decode the UTF-8 data
2. Print/write the data to USB serial (so the computer program can read it)
3. Preserve the `\n` terminator

### Connection Management

- Continue advertising when no BLE client is connected
- Accept one connection at a time
- When a BLE client disconnects, resume advertising
- When USB serial data arrives but no BLE client is connected, optionally buffer or discard (implementer's choice)

### Coexistence with Existing Features

The ESP32 KeyMaster currently has:
- A button-driven menu for key management
- An SD card key store
- A display showing status

The serial bridge should coexist with these features:
- Only forward data on USB serial when it looks like a protocol message (starts with `{`, `CMD:`, or plain text)
- Alternatively, use a dedicated serial prefix or baud rate to distinguish bridge data from REPL input
- The simplest approach: **any line received on USB serial is forwarded over BLE**, and REPL access is only available when the bridge task is not running

## Pi Zero Client Behavior

For reference, here's what the Pi Zero BLE client does (already implemented):

1. **Auto-scan** for a device named "KeyMaster"
2. **Connect** and subscribe to TX notifications
3. **Auto-reconnect** on disconnect (5-second interval)
4. **Buffer** notification bytes and split on `\n`
5. **Process** each complete message by type (JSON/CMD/text)
6. **Send** responses by writing to the RX characteristic

## Testing with Current Firmware

The current KeyMaster firmware **echoes** any data written to RX back via TX notify. This can be used to test the Pi Zero client:

1. Pi connects to KeyMaster
2. Pi writes `CMD:ping\n` to RX
3. KeyMaster echoes `CMD:ping` back via TX notify
4. Pi receives the echo (confirms connectivity)

After the serial bridge is implemented, the echo behavior should be replaced with serial forwarding.

## Example Session

```
[Computer sends via USB serial]  →  {"type":"note","text":"Buy milk"}\n
[ESP32 forwards via BLE TX]      →  Pi Zero receives, saves note, logs event
[Pi Zero replies via BLE RX]     →  (no reply for notes)

[Computer sends]                 →  CMD:status\n
[ESP32 forwards via BLE TX]      →  Pi Zero receives
[Pi Zero replies via BLE RX]     →  {"type":"status_response","state":"STT_IDLE","disk_free_gb":21.7}\n
[ESP32 forwards via USB serial]  →  Computer receives status JSON

[Computer sends]                 →  Hello from the desktop\n
[ESP32 forwards via BLE TX]      →  Pi Zero saves as text note
```
