"""NOAA Space Weather Prediction Center client.

Geomagnetic activity (Kp index) + aurora forecast. Geomagnetic data
is GLOBAL — same Kp value for the whole planet — so we fetch once per
poll cycle and apply to every location, then layer location-specific
aurora visibility via latitude check.

Endpoints (no auth, no key):
  https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json
    — Last 24 hours of Kp values (3-hour cadence)
  https://services.swpc.noaa.gov/text/3-day-forecast.txt
    — Plain-text 3-day Kp + alerts forecast

We use the JSON endpoint for the current value + a simple lat-based
"can you see aurora?" check (oval visibility roughly Kp>=5 at 50°,
Kp>=7 at 40°N).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request


log = logging.getLogger("plugin.weather.noaa_swpc")


KP_URL = (
    "https://services.swpc.noaa.gov/products/"
    "noaa-planetary-k-index.json"
)


def _kp_category(kp) -> str:
    """NOAA G-scale narrative."""
    if kp is None:
        return ""
    try:
        k = float(kp)
    except (ValueError, TypeError):
        return ""
    if k < 4:
        return "quiet"
    if k < 5:
        return "unsettled"
    if k < 6:
        return "G1-minor-storm"
    if k < 7:
        return "G2-moderate-storm"
    if k < 8:
        return "G3-strong-storm"
    if k < 9:
        return "G4-severe-storm"
    return "G5-extreme-storm"


def _aurora_visibility_for_lat(kp, latitude) -> str:
    """Rough heuristic. NOAA publishes the actual oval; this is a
    pessimistic approximation good enough for an early signal."""
    if kp is None or latitude is None:
        return ""
    try:
        k = float(kp)
        lat = abs(float(latitude))
    except (ValueError, TypeError):
        return ""
    # Approximate equatorward boundary of typical visible aurora oval
    # by Kp (Hp 2003; rough).
    thresholds = [
        (9, 30), (8, 35), (7, 40), (6, 45),
        (5, 50), (4, 55), (3, 60), (2, 65), (1, 67),
    ]
    for k_needed, lat_floor in thresholds:
        if k >= k_needed and lat >= lat_floor:
            return (f"possible — Kp {k:.1f} can reach lat {lat_floor}°, "
                    f"you're at {lat:.1f}°")
    return f"unlikely — Kp {k:.1f} too low for lat {lat:.1f}°"


def fetch_kp(timeout: float = 20.0) -> dict:
    """Hit SWPC Kp index endpoint. Returns {kp, kp_category, raw_json,
    observed_at_utc}. observed_at_utc is the timestamp of the most
    recent reading (3h cadence).

    Schema (as of 2026-06): list of dicts, each with keys
    `time_tag`, `Kp`, `a_running`, `station_count`. Most-recent last.
    Earlier SWPC versions returned list-of-lists (header + rows); we
    tolerate both for forward/backward compat.
    """
    req = urllib.request.Request(KP_URL, headers={
        "User-Agent": "cortex-core-weather/0.1 "
                       "(https://github.com/turfptax/cortex-core)",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    if not isinstance(raw, list) or not raw:
        return {"kp": None, "kp_category": "",
                "observed_at_utc": "",
                "raw_json": json.dumps(raw)}
    last = raw[-1]
    kp_val = None
    time_tag = ""
    if isinstance(last, dict):
        # New schema (2026-06+): list-of-dicts
        kp_val = last.get("Kp")
        time_tag = last.get("time_tag") or ""
    elif isinstance(last, list) and isinstance(raw[0], list):
        # Old schema: header row + data rows
        header = raw[0]
        try:
            idx_kp = header.index("Kp")
        except ValueError:
            idx_kp = 1
        try:
            idx_time = header.index("time_tag")
        except ValueError:
            idx_time = 0
        if last is not header and len(last) > max(idx_kp, idx_time):
            kp_val = last[idx_kp]
            time_tag = str(last[idx_time] or "")
    try:
        kp_val = float(kp_val) if kp_val is not None else None
    except (ValueError, TypeError):
        kp_val = None
    return {
        "kp": kp_val,
        "kp_category": _kp_category(kp_val),
        "observed_at_utc": time_tag,
        "raw_json": json.dumps(last),
    }


def aurora_signal_for_location(kp, latitude) -> str:
    """Public — caller (loop) computes once per kp pull and applies
    per-location."""
    return _aurora_visibility_for_lat(kp, latitude)
