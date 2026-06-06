"""Open-Meteo client — global current conditions + hourly forecast.

Free, no API key, no auth. Docs: https://open-meteo.com/en/docs

Rate limits: 10,000 calls/day free tier. We poll hourly per location =
24 calls/day per location. Even 50 locations stays well under the
limit.

We request the structured `current` block + `hourly` with a small
variable set. Heavy variables (radiation, soil, etc.) skipped — not
relevant for the sky-watching use case + keeps payload small.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request


log = logging.getLogger("plugin.weather.open_meteo")


BASE_URL = "https://api.open-meteo.com/v1/forecast"


# WMO weather code → human-readable text. Subset of the full spec;
# fall back to "code N" for codes we don't list.
_WMO_TEXT = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy",
    3: "Overcast", 45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle (light)", 57: "Freezing drizzle (dense)",
    61: "Slight rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain (light)", 67: "Freezing rain (heavy)",
    71: "Slight snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def conditions_text(weather_code) -> str:
    if weather_code is None:
        return ""
    try:
        return _WMO_TEXT.get(int(weather_code), f"code {weather_code}")
    except (ValueError, TypeError):
        return ""


def fetch_current(lat: float, lon: float, *, timezone: str = "UTC",
                   timeout: float = 20.0) -> dict:
    """Hit Open-Meteo and return a parsed dict ready to write to
    weather_observations. Raises on network / parse failure — caller
    catches and logs.

    Return shape:
      {
        observed_at: ISO UTC (from API timestamp),
        local_observed_at: ISO with offset,
        source: 'open-meteo',
        temp_c, temp_f, feels_like_c, humidity_pct, pressure_hpa,
        wind_speed_mps, wind_direction_deg, wind_gust_mps,
        cloud_cover_pct, weather_code, conditions_text,
        precipitation_mm, visibility_m,
        raw_json: <full response>
      }
    """
    params = {
        "latitude": str(lat),
        "longitude": str(lon),
        "timezone": timezone or "UTC",
        # current block — what we need RIGHT NOW
        "current": ",".join((
            "temperature_2m",
            "relative_humidity_2m",
            "apparent_temperature",
            "is_day",
            "precipitation",
            "weather_code",
            "cloud_cover",
            "pressure_msl",
            "wind_speed_10m",
            "wind_direction_10m",
            "wind_gusts_10m",
        )),
        # Hourly variables — used by Phase 2 forecast cards.
        "hourly": ",".join((
            "temperature_2m",
            "weather_code",
            "cloud_cover",
            "precipitation_probability",
        )),
        "forecast_days": "3",
        "wind_speed_unit": "ms",
        "temperature_unit": "celsius",
    }
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "cortex-core-weather/0.1 "
                       "(https://github.com/turfptax/cortex-core)",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    cur = raw.get("current") or {}

    # Open-Meteo current.time is local-tz string like "2026-06-01T17:00"
    # — interpret in the configured timezone so observed_at carries the
    # right meaning. We store UTC in observed_at and local-tz in
    # local_observed_at.
    import datetime as _dt
    cur_time = cur.get("time") or ""
    observed_at = ""
    local_observed_at = ""
    if cur_time:
        try:
            # Naive ISO timestamp in the timezone Open-Meteo echoed.
            naive = _dt.datetime.fromisoformat(cur_time)
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(timezone or "UTC")
                aware = naive.replace(tzinfo=tz)
            except Exception:
                aware = naive.replace(tzinfo=_dt.timezone.utc)
            local_observed_at = aware.isoformat(timespec="seconds")
            observed_at = aware.astimezone(
                _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            log.warning("could not parse current.time %r: %s",
                        cur_time, e)

    temp_c = cur.get("temperature_2m")
    temp_f = None
    try:
        if temp_c is not None:
            temp_f = round(float(temp_c) * 9 / 5 + 32, 1)
    except Exception:
        pass

    return {
        "observed_at": observed_at or _dt.datetime.utcnow().strftime(
            "%Y-%m-%d %H:%M:%S"),
        "local_observed_at": local_observed_at,
        "source": "open-meteo",
        "temp_c": temp_c,
        "temp_f": temp_f,
        "feels_like_c": cur.get("apparent_temperature"),
        "humidity_pct": cur.get("relative_humidity_2m"),
        "pressure_hpa": cur.get("pressure_msl"),
        "wind_speed_mps": cur.get("wind_speed_10m"),
        "wind_direction_deg": cur.get("wind_direction_10m"),
        "wind_gust_mps": cur.get("wind_gusts_10m"),
        "cloud_cover_pct": cur.get("cloud_cover"),
        "weather_code": cur.get("weather_code"),
        "conditions_text": conditions_text(cur.get("weather_code")),
        "precipitation_mm": cur.get("precipitation"),
        "visibility_m": None,   # Open-Meteo current doesn't expose this
        "raw_json": json.dumps(raw),
    }
