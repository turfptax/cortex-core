"""Power management helpers."""

import subprocess

from config import HTTP_ENABLED


def wifi_off():
    """Disable WiFi radio to save power. No-op if HTTP server is enabled."""
    if HTTP_ENABLED:
        return  # WiFi must stay on for HTTP API
    subprocess.run(["sudo", "rfkill", "block", "wifi"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wifi_on():
    """Enable WiFi radio."""
    subprocess.run(["sudo", "rfkill", "unblock", "wifi"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
