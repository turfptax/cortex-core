"""sunrise-sunset.org client — accurate sun + twilight times globally.

Free, no auth, no key, no rate limit listed. Docs:
https://sunrise-sunset.org/api

Returns UTC ISO strings for sunrise, sunset, civil_twilight,
nautical_twilight, astronomical_twilight + solar noon + day length.

Used in preference to a hand-rolled formula because subtle convention
issues (longitude sign, reference epoch, equation-of-time) make
offline computation a debugging trap. The API returns values accurate
to the second.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import urllib.parse
import urllib.request


log = logging.getLogger("plugin.weather.sunrise_sunset")


BASE = "https://api.sunrise-sunset.org/json"


def _parse_iso(s: str) -> str:
    """The API returns timestamps in `formatted=0` mode as ISO UTC
    like '2026-06-05T11:34:15+00:00'. Return as-is if parseable,
    else empty string."""
    if not s:
        return ""
    try:
        d = _dt.datetime.fromisoformat(s)
        return d.astimezone(_dt.timezone.utc).isoformat(
            timespec="seconds")
    except Exception:
        return ""


def fetch_sun_times(lat: float, lon: float,
                     date: _dt.date | None = None,
                     timeout: float = 15.0) -> dict:
    """Fetch sun + twilight times for one lat/lon on `date` (default
    today UTC). Returns dict with UTC ISO strings + day_length_s.
    Raises on network failure."""
    d = date or _dt.datetime.utcnow().date()
    params = {
        "lat": str(lat),
        "lng": str(lon),
        "date": d.isoformat(),
        "formatted": "0",   # ISO output
    }
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "cortex-core-weather/0.1 "
                       "(https://github.com/turfptax/cortex-core)",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    if raw.get("status") != "OK":
        raise RuntimeError(
            f"sunrise-sunset.org: status={raw.get('status')!r}")
    res = raw.get("results") or {}
    return {
        "sunrise_utc": _parse_iso(res.get("sunrise") or ""),
        "sunset_utc": _parse_iso(res.get("sunset") or ""),
        "solar_noon_utc": _parse_iso(res.get("solar_noon") or ""),
        "civil_twilight_begin_utc": _parse_iso(
            res.get("civil_twilight_begin") or ""),
        "civil_twilight_end_utc": _parse_iso(
            res.get("civil_twilight_end") or ""),
        "nautical_twilight_end_utc": _parse_iso(
            res.get("nautical_twilight_end") or ""),
        "astronomical_twilight_end_utc": _parse_iso(
            res.get("astronomical_twilight_end") or ""),
        "day_length_s": int(res.get("day_length") or 0),
        "raw_json": json.dumps(raw),
    }
