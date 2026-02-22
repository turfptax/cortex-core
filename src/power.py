"""Power management helpers."""

import subprocess


def wifi_off():
    """Disable WiFi radio to save power."""
    subprocess.run(["sudo", "rfkill", "block", "wifi"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wifi_on():
    """Enable WiFi radio."""
    subprocess.run(["sudo", "rfkill", "unblock", "wifi"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
