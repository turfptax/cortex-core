"""Weather plugin schema + DB helpers.

Forward-compatible shape: every observation carries a `raw_json` field
holding the source response so we can extract additional fields later
without re-polling. The structured columns are the indexable subset.

Tables
------
locations
  Named lat/lon points the plugin polls. Multi-location is the
  primary use case — home + travel + remote. is_primary picks the
  one Hub shows by default.

weather_observations
  Per-poll snapshot of current conditions. One row per (location,
  observed_at). Source-tagged so multi-source overlays are possible
  later.

sky_observations
  Per-poll snapshot of sky-watching signals: moon phase + illumination,
  twilight times, Kp index + aurora forecast (geomagnetic), cloud
  cover %. Separate from weather because the cadence + dimensions
  differ — moon phase doesn't change hourly, Kp can spike between
  polls.

weather_alerts (Phase 2 scaffold — table created empty for now)
  NWS / equivalent alerts. Deduped by source_alert_id. dismissed_at
  is set when the user acks via Hub.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path


log = logging.getLogger("plugin.weather.db")


WEATHER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,            -- url-safe key, e.g. 'lincoln-ne'
    name TEXT NOT NULL,                   -- display name 'Lincoln, NE'
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC', -- IANA tz
    is_primary INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',       -- free-text — 'home', 'next trip', etc.
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_polled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_locations_primary ON locations(is_primary);

CREATE TABLE IF NOT EXISTS weather_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,             -- UTC ISO
    local_observed_at TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,                  -- 'open-meteo' | 'nws' | ...
    -- Indexable structured fields. NULL when source doesn't supply.
    temp_c REAL,
    temp_f REAL,
    feels_like_c REAL,
    humidity_pct REAL,
    pressure_hpa REAL,
    wind_speed_mps REAL,
    wind_direction_deg REAL,
    wind_gust_mps REAL,
    cloud_cover_pct REAL,
    visibility_m REAL,
    precipitation_mm REAL,
    weather_code INTEGER,                  -- WMO code
    conditions_text TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (location_id) REFERENCES locations(id)
);
CREATE INDEX IF NOT EXISTS idx_obs_loc_time
    ON weather_observations(location_id, observed_at);

CREATE TABLE IF NOT EXISTS sky_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    local_observed_at TEXT NOT NULL DEFAULT '',
    -- Astronomy (computed offline, no source)
    moon_phase REAL,                       -- 0.0=new, 0.5=full, 1.0=new again
    moon_phase_name TEXT NOT NULL DEFAULT '',
    moon_illumination_pct REAL,
    sunrise_utc TEXT,
    sunset_utc TEXT,
    civil_twilight_end_utc TEXT,
    astronomical_twilight_end_utc TEXT,    -- "real dark" boundary
    -- Space weather (NOAA SWPC)
    kp_index REAL,
    kp_category TEXT NOT NULL DEFAULT '',  -- quiet/unsettled/active/storm/severe
    aurora_forecast TEXT NOT NULL DEFAULT '',
    solar_wind_speed_kms REAL,
    -- Sky visibility (from weather source — duplicated here for fast lookup)
    cloud_cover_pct REAL,
    visibility_m REAL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (location_id) REFERENCES locations(id)
);
CREATE INDEX IF NOT EXISTS idx_sky_loc_time
    ON sky_observations(location_id, observed_at);

-- Phase 2 scaffold — table exists, no source populates it yet.
CREATE TABLE IF NOT EXISTS weather_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL,
    source TEXT NOT NULL,                  -- 'nws' | ...
    source_alert_id TEXT NOT NULL,
    severity TEXT NOT NULL,                -- minor | moderate | severe | extreme
    event TEXT NOT NULL,                   -- 'Severe Thunderstorm Warning'
    headline TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    effective_at TEXT,
    expires_at TEXT,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    dismissed_at TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (location_id) REFERENCES locations(id),
    UNIQUE (source, source_alert_id)
);
CREATE INDEX IF NOT EXISTS idx_alerts_active
    ON weather_alerts(location_id, dismissed_at, expires_at);
"""


def _norm_slug(s: str) -> str:
    """url-safe lowercase kebab. Drops anything non-alnum-dash."""
    import re
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-") or "unnamed"


class WeatherDB:
    """SQLite wrapper for the weather plugin's DB.

    Stores at plugins/weather/data/weather.db. Independent from the
    overseer DB so the weather plugin can be dropped/recreated without
    touching corpus data.
    """

    def __init__(self, db_path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._write_lock = threading.RLock()
        self._conn.executescript(WEATHER_SCHEMA_SQL)
        self._safe_commit()

    def _safe_commit(self):
        with self._write_lock:
            self._conn.commit()

    # ── locations ───────────────────────────────────────────────

    def upsert_location(self, *, slug, name, lat, lon,
                          timezone="UTC", is_primary=False, notes=""):
        """Insert if new (by slug), update lat/lon/name/tz if exists.
        Returns the row id."""
        slug = _norm_slug(slug)
        existing = self.get_location_by_slug(slug)
        if existing:
            self._conn.execute(
                "UPDATE locations SET name=?, lat=?, lon=?, "
                "  timezone=?, is_primary=?, notes=? WHERE slug=?",
                (str(name), float(lat), float(lon),
                 str(timezone),
                 1 if is_primary else 0, str(notes), slug),
            )
            self._safe_commit()
            return existing["id"]
        cur = self._conn.execute(
            "INSERT INTO locations "
            "(slug, name, lat, lon, timezone, is_primary, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slug, str(name), float(lat), float(lon),
             str(timezone), 1 if is_primary else 0, str(notes)),
        )
        self._safe_commit()
        return cur.lastrowid

    def get_location_by_slug(self, slug):
        row = self._conn.execute(
            "SELECT * FROM locations WHERE slug = ?",
            (_norm_slug(slug),),
        ).fetchone()
        return dict(row) if row else None

    def get_location_by_id(self, loc_id):
        row = self._conn.execute(
            "SELECT * FROM locations WHERE id = ?", (int(loc_id),),
        ).fetchone()
        return dict(row) if row else None

    def list_locations(self):
        rows = self._conn.execute(
            "SELECT * FROM locations "
            "ORDER BY is_primary DESC, name ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_location(self, slug):
        cur = self._conn.execute(
            "DELETE FROM locations WHERE slug = ?",
            (_norm_slug(slug),),
        )
        self._safe_commit()
        return cur.rowcount

    def touch_location_polled(self, loc_id):
        self._conn.execute(
            "UPDATE locations SET last_polled_at = datetime('now') "
            "WHERE id = ?", (int(loc_id),),
        )
        self._safe_commit()

    # ── weather_observations ────────────────────────────────────

    def add_weather_observation(self, *, location_id, observed_at,
                                  local_observed_at="",
                                  source, raw_json="{}", **fields):
        """Insert a weather observation. Structured fields are passed
        as kwargs (temp_c, humidity_pct, etc.) — unknown kwargs are
        ignored so callers can pass partial dicts."""
        known = (
            "temp_c", "temp_f", "feels_like_c", "humidity_pct",
            "pressure_hpa", "wind_speed_mps", "wind_direction_deg",
            "wind_gust_mps", "cloud_cover_pct", "visibility_m",
            "precipitation_mm", "weather_code", "conditions_text",
        )
        cols = ["location_id", "observed_at", "local_observed_at",
                "source", "raw_json"]
        vals = [int(location_id), str(observed_at),
                str(local_observed_at), str(source), str(raw_json)]
        for k in known:
            if k in fields and fields[k] is not None:
                cols.append(k)
                vals.append(fields[k])
        placeholders = ",".join("?" * len(cols))
        col_list = ",".join(cols)
        cur = self._conn.execute(
            f"INSERT INTO weather_observations ({col_list}) "
            f"VALUES ({placeholders})",
            tuple(vals),
        )
        self._safe_commit()
        return cur.lastrowid

    def latest_weather(self, location_id):
        row = self._conn.execute(
            "SELECT * FROM weather_observations "
            "WHERE location_id = ? "
            "ORDER BY observed_at DESC LIMIT 1",
            (int(location_id),),
        ).fetchone()
        return dict(row) if row else None

    def recent_weather(self, location_id, *, hours=24, limit=200):
        rows = self._conn.execute(
            "SELECT * FROM weather_observations "
            "WHERE location_id = ? "
            "  AND observed_at >= datetime('now', ?) "
            "ORDER BY observed_at DESC LIMIT ?",
            (int(location_id), f"-{int(hours)} hours", int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── weather_alerts (NWS) ────────────────────────────────────

    def upsert_alert(self, *, location_id, source, source_alert_id,
                      severity, event, headline, description="",
                      effective_at=None, expires_at=None, raw_json="{}"):
        """Insert a new alert; dedup on UNIQUE(source, source_alert_id).
        Returns True if a NEW row was inserted, False if already stored
        (lets the poll cycle detect newly-arrived alerts)."""
        with self._write_lock:
            try:
                self._conn.execute(
                    "INSERT INTO weather_alerts (location_id, source, "
                    " source_alert_id, severity, event, headline, "
                    " description, effective_at, expires_at, raw_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (int(location_id), source, str(source_alert_id),
                     severity, event, headline, description,
                     effective_at, expires_at, raw_json),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def active_alerts(self, location_id=None):
        """Non-dismissed alerts that haven't expired, newest first.

        Expiry is evaluated in Python: NWS timestamps carry a tz offset
        (e.g. ...T07:30:00-05:00) which SQLite's datetime() can't compare
        reliably against a UTC 'now'."""
        sql = "SELECT * FROM weather_alerts WHERE dismissed_at IS NULL"
        params = []
        if location_id is not None:
            sql += " AND location_id = ?"
            params.append(int(location_id))
        sql += " ORDER BY received_at DESC LIMIT 500"
        rows = [dict(r) for r in
                self._conn.execute(sql, params).fetchall()]
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        out = []
        for r in rows:
            exp = r.get("expires_at")
            if exp:
                try:
                    if _dt.datetime.fromisoformat(exp) < now:
                        continue  # expired
                except Exception:
                    pass  # unparseable -> keep, let consumer decide
            out.append(r)
        return out[:200]

    # ── sky_observations ────────────────────────────────────────

    def add_sky_observation(self, *, location_id, observed_at,
                              local_observed_at="", raw_json="{}",
                              **fields):
        known = (
            "moon_phase", "moon_phase_name", "moon_illumination_pct",
            "sunrise_utc", "sunset_utc", "civil_twilight_end_utc",
            "astronomical_twilight_end_utc",
            "kp_index", "kp_category", "aurora_forecast",
            "solar_wind_speed_kms",
            "cloud_cover_pct", "visibility_m",
        )
        cols = ["location_id", "observed_at", "local_observed_at",
                "raw_json"]
        vals = [int(location_id), str(observed_at),
                str(local_observed_at), str(raw_json)]
        for k in known:
            if k in fields and fields[k] is not None:
                cols.append(k)
                vals.append(fields[k])
        placeholders = ",".join("?" * len(cols))
        col_list = ",".join(cols)
        cur = self._conn.execute(
            f"INSERT INTO sky_observations ({col_list}) "
            f"VALUES ({placeholders})",
            tuple(vals),
        )
        self._safe_commit()
        return cur.lastrowid

    def latest_sky(self, location_id):
        row = self._conn.execute(
            "SELECT * FROM sky_observations "
            "WHERE location_id = ? "
            "ORDER BY observed_at DESC LIMIT 1",
            (int(location_id),),
        ).fetchone()
        return dict(row) if row else None

    # ── prune ───────────────────────────────────────────────────

    def prune_old(self, retention_days):
        """Drop observations older than N days. Alerts kept indefinitely
        (they're small + audit-relevant)."""
        d = int(retention_days)
        if d <= 0:
            return {"weather_deleted": 0, "sky_deleted": 0}
        w = self._conn.execute(
            "DELETE FROM weather_observations "
            "WHERE observed_at < datetime('now', ?)",
            (f"-{d} days",),
        ).rowcount
        s = self._conn.execute(
            "DELETE FROM sky_observations "
            "WHERE observed_at < datetime('now', ?)",
            (f"-{d} days",),
        ).rowcount
        self._safe_commit()
        return {"weather_deleted": w, "sky_deleted": s}

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
