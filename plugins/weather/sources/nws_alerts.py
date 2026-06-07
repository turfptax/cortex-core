"""NWS active weather alerts (api.weather.gov).

Public US National Weather Service API — no auth, but NWS policy
requires a descriptive User-Agent. Fetches active alerts for a lat/lon
point and returns normalized dicts ready for WeatherDB.upsert_alert.

Never raises — returns [] on any error so a flaky alerts endpoint (or a
non-US point, which NWS doesn't cover) can't break the weather poll
cycle. NWS timestamps carry a tz offset and are stored verbatim; the DB
layer evaluates expiry in Python.
"""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger("plugin.weather.nws_alerts")

# NWS asks for a contact in the UA string (their docs); identifies Cortex.
_UA = "cortex-weather (torylogos@gmail.com)"
# weather_alerts.severity is an enum: minor | moderate | severe | extreme.
_SEVERITIES = ("minor", "moderate", "severe", "extreme")


def fetch_active(lat, lon, *, timeout=20):
    """Return a list of normalized active-alert dicts for the point.

    Each dict: source_alert_id, severity (lowercased to the table enum),
    event, headline, description, effective_at, expires_at, raw (the full
    GeoJSON feature). Non-US points / no alerts -> [].
    """
    url = ("https://api.weather.gov/alerts/active?status=actual"
           "&point={:.4f},{:.4f}".format(float(lat), float(lon)))
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/geo+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("nws_alerts fetch failed for %s,%s: %s", lat, lon, e)
        return []

    out = []
    for feat in (data.get("features") or []):
        props = feat.get("properties") or {}
        aid = props.get("id") or feat.get("id")
        if not aid:
            continue
        sev = (props.get("severity") or "").strip().lower()
        if sev not in _SEVERITIES:
            sev = "minor"  # NWS 'Unknown'/None -> floor at the enum minimum
        out.append({
            "source_alert_id": str(aid),
            "severity": sev,
            "event": props.get("event") or "",
            "headline": props.get("headline") or "",
            "description": props.get("description") or "",
            "effective_at": props.get("effective") or props.get("onset"),
            "expires_at": props.get("expires") or props.get("ends"),
            "raw": feat,
        })
    return out
