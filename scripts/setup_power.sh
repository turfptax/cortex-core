#!/bin/bash
# One-time power optimization for the wearable recorder.
# Run with: sudo bash setup_power.sh

set -e

echo "=== Disabling unnecessary services ==="

# Desktop (not needed — display is SPI userspace)
systemctl disable --now lightdm 2>/dev/null || true

# Printing
systemctl disable --now cups cups-browsed 2>/dev/null || true

# Modem (no cellular)
systemctl disable --now ModemManager 2>/dev/null || true

# mDNS discovery
systemctl disable --now avahi-daemon 2>/dev/null || true

# Bluetooth (re-enable when ESP32 Keymaster needed)
systemctl disable --now bluetooth hciuart 2>/dev/null || true

# Hotkey daemon (no keyboard attached)
systemctl disable --now triggerhappy 2>/dev/null || true

# Apt auto-update timers (prevent random CPU spikes)
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

# Man page cache rebuild
systemctl disable --now man-db.timer 2>/dev/null || true

echo "=== Updating boot config ==="

CONFIG="/boot/firmware/config.txt"

# Add power optimizations if not already present
grep -q "gpu_mem=16" "$CONFIG" || echo "gpu_mem=16" >> "$CONFIG"
grep -q "camera_auto_detect=0" "$CONFIG" || sed -i 's/camera_auto_detect=1/camera_auto_detect=0/' "$CONFIG"
grep -q "display_auto_detect=0" "$CONFIG" || sed -i 's/display_auto_detect=1/display_auto_detect=0/' "$CONFIG"
grep -q "act_led_trigger=none" "$CONFIG" || echo "dtparam=act_led_trigger=none" >> "$CONFIG"
grep -q "act_led_activelow=on" "$CONFIG" || echo "dtparam=act_led_activelow=on" >> "$CONFIG"

echo "=== Done ==="
echo "Reboot to apply boot config changes."
echo "To re-enable WiFi: sudo rfkill unblock wifi"
echo "To re-enable Bluetooth: sudo systemctl start bluetooth"
