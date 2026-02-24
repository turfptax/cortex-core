"""BLE central client for ESP32-S3 KeyMaster bridge.

Runs bleak (asyncio) in a background thread. Main loop communicates
via thread-safe queues. Auto-scans, connects, and reconnects.

Protocol: newline-delimited UTF-8 messages (\n terminated).
"""

import asyncio
import json
import logging
import queue
import socket
import threading
import time

from bleak import BleakClient, BleakScanner

from config import (
    BLE_DEVICE_NAME, BLE_SERVICE_UUID, BLE_TX_UUID, BLE_RX_UUID,
    BLE_RECONNECT_INTERVAL_S, BLE_MAX_MESSAGE_LEN,
    HTTP_ENABLED, HTTP_PORT, HTTP_TOKEN_PATH,
)

log = logging.getLogger("ble_client")


def _get_local_ip():
    """Get the Pi's local IP address (wlan0 preferred)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("10.255.255.255", 1))  # doesn't actually send anything
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _build_discovery_payload():
    """Build DISCOVER: message with Pi's IP, HTTP port, and auth token."""
    if not HTTP_ENABLED:
        return None
    ip = _get_local_ip()
    if not ip:
        return None
    token = ""
    try:
        with open(HTTP_TOKEN_PATH, "r") as f:
            token = f.read().strip()
    except (FileNotFoundError, OSError):
        pass
    payload = {"ip": ip, "port": HTTP_PORT}
    if token:
        payload["token"] = token
    return "DISCOVER:" + json.dumps(payload, separators=(",", ":"))


class BLEClient:
    """BLE central client that connects to KeyMaster in a background thread."""

    def __init__(self, on_connect=None, on_disconnect=None):
        """
        Args:
            on_connect: callback(address: str) called from background thread
            on_disconnect: callback() called from background thread
        """
        self._inbound = queue.Queue()   # messages received from ESP32
        self._outbound = queue.Queue()  # messages to send to ESP32
        self._connected = False
        self._running = False
        self._thread = None
        self._loop = None
        self._address = None
        self._on_connect_cb = on_connect
        self._on_disconnect_cb = on_disconnect
        self._rx_buffer = b""  # partial notification buffer
        # Device metadata (populated on connect, cleared on disconnect)
        self.device_name = None
        self.mtu_size = None
        self.rssi = None

    def start(self):
        """Launch background thread for BLE operations."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_entry, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal thread to stop and wait."""
        self._running = False
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def send(self, message):
        """Enqueue a message to send to the ESP32 (thread-safe).

        Message should NOT include trailing newline — it will be added.
        """
        if len(message) > BLE_MAX_MESSAGE_LEN:
            message = message[:BLE_MAX_MESSAGE_LEN]
        self._outbound.put(message)

    def poll_messages(self):
        """Drain and return all received messages (called from main loop)."""
        messages = []
        while True:
            try:
                messages.append(self._inbound.get_nowait())
            except queue.Empty:
                break
        return messages

    def is_connected(self):
        """Check if currently connected to KeyMaster."""
        return self._connected

    def get_address(self):
        """Return the BLE address of the connected device, or None."""
        return self._address if self._connected else None

    # ---- Background Thread ----

    def _thread_entry(self):
        """Entry point for the background thread — create event loop and run."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_loop())
        except Exception as e:
            log.error("BLE thread crashed: %s", e)
        finally:
            self._loop.close()
            self._loop = None

    async def _run_loop(self):
        """Main async loop: scan, connect, communicate, reconnect."""
        while self._running:
            try:
                # Scan for KeyMaster
                device = await self._scan()
                if device is None:
                    await asyncio.sleep(BLE_RECONNECT_INTERVAL_S)
                    continue

                # Connect and communicate
                await self._connect_and_run(device)

            except Exception as e:
                log.warning("BLE error: %s", e)
                self._connected = False
                self._address = None
                self.device_name = None
                self.mtu_size = None
                self.rssi = None

            # Wait before reconnecting
            if self._running:
                if self._on_disconnect_cb:
                    try:
                        self._on_disconnect_cb()
                    except Exception:
                        pass
                await asyncio.sleep(BLE_RECONNECT_INTERVAL_S)

    async def _scan(self):
        """Scan for KeyMaster device. Returns BLEDevice or None."""
        if not self._running:
            return None
        try:
            devices = await BleakScanner.discover(timeout=5.0)
            for d in devices:
                if d.name and BLE_DEVICE_NAME in d.name:
                    log.info("Found %s at %s", d.name, d.address)
                    return d
        except Exception as e:
            log.warning("BLE scan error: %s", e)
        return None

    async def _connect_and_run(self, device):
        """Connect to device, subscribe to TX, handle send/receive."""
        self._rx_buffer = b""
        async with BleakClient(device.address, timeout=10.0) as client:
            if not client.is_connected:
                return

            self._connected = True
            self._address = device.address
            self.device_name = device.name
            self.rssi = getattr(device, 'rssi', None)
            log.info("Connected to %s (%s)", device.name, device.address)

            if self._on_connect_cb:
                try:
                    self._on_connect_cb(device.address)
                except Exception:
                    pass

            # Subscribe to TX notifications (ESP32 → Pi)
            # Acquire actual negotiated MTU (BlueZ negotiates 512 but bleak defaults to 23)
            try:
                await client._backend._acquire_mtu()
                log.info("BLE MTU acquired: %d (payload: %d)", client.mtu_size, client.mtu_size - 3)
                self.mtu_size = client.mtu_size
                print(f"BLE MTU: {client.mtu_size}, payload: {client.mtu_size - 3}", flush=True)
            except Exception as e:
                log.warning("MTU acquire failed: %s (using default)", e)
            await client.start_notify(BLE_TX_UUID, self._on_notify)

            # Send discovery info so the computer auto-configures WiFi bridge
            await self._send_discovery(client)

            try:
                # Communication loop
                while self._running and client.is_connected:
                    # Send any outbound messages
                    while not self._outbound.empty():
                        try:
                            msg = self._outbound.get_nowait()
                            data = (msg + "\n").encode("utf-8")
                            # Split into MTU-safe BLE writes
                            mtu_payload = max(client.mtu_size - 3, 20)
                            for ci in range(0, len(data), mtu_payload):
                                chunk = data[ci:ci + mtu_payload]
                                await client.write_gatt_char(
                                    BLE_RX_UUID, chunk, response=True,
                                )
                        except queue.Empty:
                            break
                        except Exception as e:
                            log.warning("BLE send error: %s", e)
                            break

                    await asyncio.sleep(0.1)  # 100ms poll interval

            except Exception as e:
                log.warning("BLE connection lost: %s", e)
            finally:
                self._connected = False
                self._address = None
                self.device_name = None
                self.mtu_size = None
                self.rssi = None
                try:
                    await client.stop_notify(BLE_TX_UUID)
                except Exception:
                    pass

    async def _send_discovery(self, client):
        """Send DISCOVER: message to computer via BLE → ESP32 → USB serial."""
        try:
            msg = _build_discovery_payload()
            if not msg:
                return
            data = (msg + "\n").encode("utf-8")
            mtu_payload = max(client.mtu_size - 3, 20)
            for i in range(0, len(data), mtu_payload):
                chunk = data[i:i + mtu_payload]
                await client.write_gatt_char(BLE_RX_UUID, chunk, response=True)
            log.info("Sent discovery to computer")
        except Exception as e:
            log.warning("Discovery send failed: %s", e)

    def _on_notify(self, sender, data):
        """Handle incoming BLE notification — buffer and split on newlines."""
        self._rx_buffer += data
        while b"\n" in self._rx_buffer:
            line, self._rx_buffer = self._rx_buffer.split(b"\n", 1)
            try:
                message = line.decode("utf-8").strip()
                if message:
                    self._inbound.put(message)
            except UnicodeDecodeError:
                pass

        # Safety: also handle case where device sends complete messages
        # without newlines (e.g., current echo firmware)
        if len(self._rx_buffer) > BLE_MAX_MESSAGE_LEN:
            try:
                message = self._rx_buffer.decode("utf-8").strip()
                if message:
                    self._inbound.put(message)
            except UnicodeDecodeError:
                pass
            self._rx_buffer = b""
