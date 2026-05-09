"""PiSugar 3 battery monitor via I2C.

Reads battery percentage, voltage, charging status, and temperature
from the PiSugar 3 UPS HAT over I2C bus.

Usage:
    from battery import BatteryMonitor

    bat = BatteryMonitor()       # auto-detects I2C bus
    bat.start()                  # start background polling (every 30s)
    print(bat.get_status())      # {'percentage': 100, 'voltage_mv': 4200, ...}
    bat.stop()                   # stop polling

Standalone test:
    python3 battery.py
"""

import logging
import threading
import time

log = logging.getLogger("battery")

# PiSugar 3 I2C constants
PISUGAR3_ADDR = 0x57
PISUGAR3_RTC_ADDR = 0x68

# Registers
REG_POWER_STATUS = 0x02
REG_TEMPERATURE = 0x04
REG_VOLTAGE_HI = 0x22
REG_VOLTAGE_LO = 0x23
REG_BATTERY_PCT = 0x2A


def _find_pisugar_bus():
    """Auto-detect which I2C bus the PiSugar 3 is on."""
    try:
        import smbus2
    except ImportError:
        try:
            import smbus as smbus2
        except ImportError:
            return None

    for bus_num in range(10):
        try:
            bus = smbus2.SMBus(bus_num)
            bus.read_byte_data(PISUGAR3_ADDR, REG_BATTERY_PCT)
            bus.close()
            return bus_num
        except Exception:
            try:
                bus.close()
            except Exception:
                pass
    return None


class BatteryMonitor:
    """Monitors PiSugar 3 battery over I2C."""

    def __init__(self, bus_num=None, poll_interval=30):
        self._bus_num = bus_num
        self._poll_interval = poll_interval
        self._bus = None
        self._running = False
        self._thread = None

        # Cached readings
        self.percentage = -1
        self.voltage_mv = 0
        self.is_charging = False
        self.external_power = False
        self.temperature_c = 0
        self.available = False

        self._init_bus()

    def _init_bus(self):
        try:
            import smbus2
        except ImportError:
            try:
                import smbus as smbus2
            except ImportError:
                log.info("Battery: smbus2/smbus not available")
                return

        if self._bus_num is None:
            self._bus_num = _find_pisugar_bus()

        if self._bus_num is None:
            log.info("Battery: PiSugar 3 not found on any I2C bus")
            return

        try:
            self._bus = smbus2.SMBus(self._bus_num)
            self._read_once()
            self.available = True
            log.info("Battery: PiSugar 3 on i2c-%d -- %d%% %dmV",
                     self._bus_num, self.percentage, self.voltage_mv)
        except Exception as e:
            log.warning("Battery: Failed to init PiSugar 3 on i2c-%d: %s",
                        self._bus_num, e)
            self._bus = None

    def _read_once(self):
        if not self._bus:
            return
        for attempt in range(3):
            try:
                self.percentage = self._bus.read_byte_data(
                    PISUGAR3_ADDR, REG_BATTERY_PCT)
                v_hi = self._bus.read_byte_data(
                    PISUGAR3_ADDR, REG_VOLTAGE_HI)
                v_lo = self._bus.read_byte_data(
                    PISUGAR3_ADDR, REG_VOLTAGE_LO)
                self.voltage_mv = (v_hi << 8) | v_lo
                status = self._bus.read_byte_data(
                    PISUGAR3_ADDR, REG_POWER_STATUS)
                self.external_power = bool(status & 0x80)
                self.is_charging = bool(status & 0x40)
                temp_raw = self._bus.read_byte_data(
                    PISUGAR3_ADDR, REG_TEMPERATURE)
                self.temperature_c = temp_raw - 40
                return  # success
            except Exception:
                time.sleep(0.5)

    def start(self):
        if not self.available or self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._bus:
            try:
                self._bus.close()
            except Exception:
                pass

    def _poll_loop(self):
        while self._running:
            self._read_once()
            time.sleep(self._poll_interval)

    def get_status(self):
        """Return dict of current battery status."""
        return {
            "available": self.available,
            "percentage": self.percentage,
            "voltage_mv": self.voltage_mv,
            "voltage_v": round(self.voltage_mv / 1000, 3) if self.voltage_mv else 0,
            "charging": self.is_charging,
            "external_power": self.external_power,
            "temperature_c": self.temperature_c,
        }

    def __repr__(self):
        if not self.available:
            return "Battery: unavailable"
        state = "charging" if self.is_charging else "discharging"
        return "Battery: {}% {}mV ({})".format(
            self.percentage, self.voltage_mv, state)


if __name__ == "__main__":
    bat = BatteryMonitor()
    if bat.available:
        status = bat.get_status()
        print("Battery: {}%".format(status["percentage"]))
        print("Voltage: {}V ({}mV)".format(status["voltage_v"], status["voltage_mv"]))
        print("Charging: {}".format(status["charging"]))
        print("External Power: {}".format(status["external_power"]))
        print("Temperature: {}C".format(status["temperature_c"]))
    else:
        print("PiSugar 3 not detected")
    bat.stop()
