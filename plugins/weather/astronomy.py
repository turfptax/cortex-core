"""Offline astronomy — moon phase only.

Moon phase uses a simple synodic-month model (accurate to ~2h, fine
for phase-name + illumination buckets).

Sun + twilight times moved to sources/sunrise_sunset.py — the offline
sunrise-equation has subtle longitude/reference-epoch gotchas that
ate session time to debug. The sunrise-sunset.org API gives accurate
values globally with zero auth. We cache once per day per location so
we don't hammer it.
"""

from __future__ import annotations

import datetime as _dt
import math


# ── Moon phase ──────────────────────────────────────────────────────


_SYNODIC_MONTH_DAYS = 29.530588853
# Reference new moon: 2000 Jan 6 18:14 UTC
_REF_NEW_MOON_JD = 2451550.1
_JD_REF_UTC = _dt.datetime(2000, 1, 6, 18, 14, tzinfo=_dt.timezone.utc)


def moon_phase(at: _dt.datetime | None = None) -> dict:
    """Return moon phase metrics for `at` (default now-UTC).

    Returns {
      phase:               0.0 .. 1.0 (0=new, 0.5=full)
      illumination_pct:    0.0 .. 100.0
      phase_name:          'new' | 'waxing-crescent' | ... | 'waning-crescent'
    }
    """
    if at is None:
        at = _dt.datetime.now(_dt.timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=_dt.timezone.utc)
    delta_days = (at - _JD_REF_UTC).total_seconds() / 86400.0
    phase = (delta_days % _SYNODIC_MONTH_DAYS) / _SYNODIC_MONTH_DAYS
    # Illumination is roughly (1 - cos(2π·phase)) / 2 — sinusoidal model
    illum = (1 - math.cos(2 * math.pi * phase)) / 2.0
    return {
        "phase": round(phase, 4),
        "illumination_pct": round(illum * 100.0, 1),
        "phase_name": _phase_name(phase),
    }


def _phase_name(phase: float) -> str:
    """8-bucket name. Edge buckets are narrower (±3%) so 'full' actually
    means full, not 'within 3 days of full'."""
    p = phase % 1.0
    if p < 0.03 or p >= 0.97:
        return "new"
    if p < 0.22:
        return "waxing-crescent"
    if p < 0.28:
        return "first-quarter"
    if p < 0.47:
        return "waxing-gibbous"
    if p < 0.53:
        return "full"
    if p < 0.72:
        return "waning-gibbous"
    if p < 0.78:
        return "last-quarter"
    return "waning-crescent"


# Sun + twilight times moved to sources/sunrise_sunset.py (API-backed).
# The functions below remain for callers that want the raw Julian-day
# math, but the plugin loop uses the API source for accuracy.


# ── Sun + twilight (deprecated; kept for reference) ─────────────────


def _julian_day(d: _dt.date) -> float:
    """Julian day number for `d` at 00:00 UTC."""
    a = (14 - d.month) // 12
    y = d.year + 4800 - a
    m = d.month + 12 * a - 3
    return (
        d.day + ((153 * m + 2) // 5)
        + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    ) - 0.5


def _solar_event_time(
    lat: float, lon: float, day: _dt.date,
    *, zenith_deg: float, rising: bool,
) -> _dt.datetime | None:
    """Standard sunrise-equation solver for arbitrary zenith.

    Implements Wikipedia "Sunrise equation". Longitude convention:
    east-positive (matches Open-Meteo / standard meteorology).

    Zenith conventions:
      90.833 = official sunrise/sunset (accounts for refraction)
      96     = civil twilight
      102    = nautical twilight
      108    = astronomical twilight (sky is truly dark)
    """
    # Wikipedia's `n` is the integer day count since the J2000 epoch
    # adjusted by longitude (west-positive). We work in east-positive
    # so flip the sign with -lon/360.
    jd = _julian_day(day)
    n = jd - 2451545.0 + 0.0008
    j_star = n + (-lon) / 360.0
    # Mean anomaly MUST be computed from the FULL Julian day J*, not
    # just the offset from J2000 — dropping the 2451545 base
    # introduces a ~134° phase error in M and breaks the whole chain.
    j_star_full = j_star + 2451545.0
    m = (357.5291 + 0.98560028 * j_star_full) % 360
    m_rad = math.radians(m)
    c = (1.9148 * math.sin(m_rad)
          + 0.0200 * math.sin(2 * m_rad)
          + 0.0003 * math.sin(3 * m_rad))
    L = (m + c + 180 + 102.9372) % 360
    j_transit = j_star_full + 0.0053 * math.sin(m_rad) \
                  - 0.0069 * math.sin(2 * math.radians(L))
    L_rad = math.radians(L)
    decl = math.asin(math.sin(L_rad) * math.sin(math.radians(23.44)))
    cos_h = (
        (math.cos(math.radians(zenith_deg))
          - math.sin(math.radians(lat)) * math.sin(decl))
        / (math.cos(math.radians(lat)) * math.cos(decl))
    )
    if cos_h > 1 or cos_h < -1:
        # Polar day / night — no event for this zenith on this date.
        return None
    h_deg = math.degrees(math.acos(cos_h))
    if rising:
        j_event = j_transit - (h_deg / 360.0)
    else:
        j_event = j_transit + (h_deg / 360.0)
    # Convert Julian day → UTC datetime
    jd = j_event
    jd_int = int(jd + 0.5)
    f = (jd + 0.5) - jd_int
    a_val = jd_int + 32044
    b_val = (4 * a_val + 3) // 146097
    c_val = a_val - (146097 * b_val) // 4
    d_val = (4 * c_val + 3) // 1461
    e_val = c_val - (1461 * d_val) // 4
    m_val = (5 * e_val + 2) // 153
    day_n = e_val - (153 * m_val + 2) // 5 + 1
    month_n = m_val + 3 - 12 * (m_val // 10)
    year_n = 100 * b_val + d_val - 4800 + (m_val // 10)
    total_seconds = f * 86400
    h = int(total_seconds // 3600)
    rest = total_seconds - h * 3600
    minute = int(rest // 60)
    sec = int(rest - minute * 60)
    try:
        return _dt.datetime(
            year_n, month_n, day_n, h, minute, sec,
            tzinfo=_dt.timezone.utc,
        )
    except ValueError:
        return None


def sun_times(lat: float, lon: float,
                date: _dt.date | None = None) -> dict:
    """Return key solar event timestamps for the given date.

    All values UTC ISO strings (or empty if the location is in polar
    day/night for that zenith).
    """
    d = date or _dt.datetime.utcnow().date()

    def _iso(dt):
        return dt.isoformat(timespec="seconds") if dt else ""

    return {
        "sunrise_utc": _iso(_solar_event_time(
            lat, lon, d, zenith_deg=90.833, rising=True)),
        "sunset_utc": _iso(_solar_event_time(
            lat, lon, d, zenith_deg=90.833, rising=False)),
        "civil_twilight_end_utc": _iso(_solar_event_time(
            lat, lon, d, zenith_deg=96.0, rising=False)),
        "astronomical_twilight_end_utc": _iso(_solar_event_time(
            lat, lon, d, zenith_deg=108.0, rising=False)),
    }
