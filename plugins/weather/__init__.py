"""Cortex weather plugin.

Phase 1: scaffold + multi-location + Open-Meteo + NOAA SWPC + offline
moon/twilight + HTTP routes + hourly background polling.

The plugin is INDEPENDENT of the overseer plugin — runs on its own DB
under plugins/weather/data/weather.db. The overseer can read from it
via the HTTP routes if it wants to surface weather in working memory
(Phase 2 wiring).

Routes (all under /plugins/weather/):
  GET  /locations              list all named locations
  POST /locations              add or update a location
  POST /locations/delete       remove a location by slug
  GET  /current?location=slug  most-recent weather observation
  GET  /sky?location=slug      most-recent sky observation
  GET  /forecast?location=slug parsed hourly forecast (last raw_json)
  GET  /history?location=slug&hours=N  weather history
  POST /poll-now               manual poll (skips cadence wait)
  GET  /status                 plugin health + last poll times
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
import threading
import time
from pathlib import Path

# plugin_api lives in src/ relative to cortex-core root
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from plugin_api import Plugin, Route  # noqa: E402

# Local imports
sys.path.insert(0, str(_HERE))
from weather_db import WeatherDB  # noqa: E402
from sources import open_meteo, noaa_swpc, sunrise_sunset  # noqa: E402
import astronomy  # noqa: E402


log = logging.getLogger("plugin.weather")


def _as_int(payload, key, default, *, max_value=None):
    try:
        v = int((payload or {}).get(key) or default)
    except (ValueError, TypeError):
        v = default
    if max_value is not None and v > max_value:
        v = max_value
    return v


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _local_iso(tz_name: str) -> str:
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo(tz_name)).isoformat(
            timespec="seconds")
    except Exception:
        return ""


class WeatherPlugin(Plugin):
    """Lifecycle + routes + the background poll loop."""

    name = "weather"

    def __init__(self, api):
        super().__init__(api)
        self.weather_db: WeatherDB | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_kp: dict = {}     # cache one Kp pull per poll cycle
        # Sun times cache: key=(slug, date_utc) → sun_times dict.
        # Sunrise/sunset don't change within a day; one API call per
        # location per day is "won't bombard" friendly.
        self._sun_cache: dict = {}
        self._last_poll_at: str = ""
        self._poll_count: int = 0
        self._poll_errors: list = []  # last few error messages

    # ── Lifecycle ────────────────────────────────────────────────

    def on_load(self) -> None:
        cfg = self.api.config
        db_path = (Path(self.api.plugin_data) / "weather.db")
        self.weather_db = WeatherDB(db_path)
        log.info("weather: DB at %s", db_path)

        # Seed primary location on first boot.
        seed = cfg.get("seed_location") or {}
        if seed.get("slug"):
            existing = self.weather_db.get_location_by_slug(seed["slug"])
            if not existing:
                self.weather_db.upsert_location(
                    slug=seed["slug"],
                    name=seed.get("name") or seed["slug"],
                    lat=float(seed.get("lat") or 0),
                    lon=float(seed.get("lon") or 0),
                    timezone=seed.get("timezone") or "UTC",
                    is_primary=bool(seed.get("is_primary", True)),
                )
                log.info("weather: seeded primary location %s",
                          seed["slug"])

        # Prune old observations per retention policy.
        retention = int(cfg.get("history_retention_days") or 90)
        pruned = self.weather_db.prune_old(retention)
        if pruned["weather_deleted"] or pruned["sky_deleted"]:
            log.info("weather: pruned %s weather + %s sky rows older "
                      "than %s days",
                      pruned["weather_deleted"], pruned["sky_deleted"],
                      retention)

        # Start the background poll thread (heartbeat pattern — same
        # shape overseer uses). Daemon so process exit isn't blocked.
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="weather_poll", daemon=True)
        self._poll_thread.start()
        log.info("weather: poll thread started")

    def http_routes(self) -> list[Route]:
        return [
            Route("GET",  "/status",               self._http_status),
            Route("GET",  "/locations",            self._http_list_locs),
            Route("POST", "/locations",            self._http_upsert_loc),
            Route("POST", "/locations/delete",     self._http_delete_loc),
            Route("GET",  "/current",              self._http_current),
            Route("GET",  "/sky",                  self._http_sky),
            Route("GET",  "/forecast",             self._http_forecast),
            Route("GET",  "/history",              self._http_history),
            Route("POST", "/poll-now",             self._http_poll_now),
        ]

    def on_unload(self) -> None:
        self._stop_event.set()
        if self.weather_db:
            self.weather_db.close()

    # ── HTTP handlers ────────────────────────────────────────────

    def _http_status(self, payload):
        if not self.weather_db:
            return {"ok": False, "error": "not initialized"}
        return {
            "ok": True,
            "poll_count": self._poll_count,
            "last_poll_at": self._last_poll_at,
            "recent_errors": self._poll_errors[-5:],
            "locations": len(self.weather_db.list_locations()),
            "sources_enabled": {
                "open_meteo": bool(self.api.config.get(
                    "source_open_meteo_enabled", True)),
                "noaa_swpc": bool(self.api.config.get(
                    "source_noaa_swpc_enabled", True)),
                "nws": bool(self.api.config.get(
                    "source_nws_enabled", False)),
            },
        }

    def _http_list_locs(self, payload):
        if not self.weather_db:
            return {"ok": False, "error": "not initialized"}
        return {"ok": True,
                "locations": self.weather_db.list_locations()}

    def _http_upsert_loc(self, payload):
        if not self.weather_db:
            return {"ok": False, "error": "not initialized"}
        p = payload or {}
        slug = str(p.get("slug") or "").strip()
        name = str(p.get("name") or "").strip()
        lat = p.get("lat")
        lon = p.get("lon")
        if not slug or not name or lat is None or lon is None:
            return {"ok": False,
                    "error": "slug, name, lat, lon required"}
        try:
            loc_id = self.weather_db.upsert_location(
                slug=slug, name=name,
                lat=float(lat), lon=float(lon),
                timezone=str(p.get("timezone") or "UTC"),
                is_primary=bool(p.get("is_primary", False)),
                notes=str(p.get("notes") or ""),
            )
            return {"ok": True, "id": loc_id,
                    "location": self.weather_db.get_location_by_id(
                        loc_id)}
        except Exception as e:
            log.exception("upsert_location failed")
            return {"ok": False, "error": str(e)}

    def _http_delete_loc(self, payload):
        if not self.weather_db:
            return {"ok": False, "error": "not initialized"}
        slug = str((payload or {}).get("slug") or "").strip()
        if not slug:
            return {"ok": False, "error": "slug required"}
        n = self.weather_db.delete_location(slug)
        return {"ok": True, "deleted": n}

    def _resolve_loc(self, payload):
        """Helper: pull a location from payload (slug param). Returns
        (loc_dict, error_dict). Defaults to the primary location when
        no slug is supplied."""
        if not self.weather_db:
            return None, {"ok": False, "error": "not initialized"}
        slug = str((payload or {}).get("location") or "").strip()
        if slug:
            loc = self.weather_db.get_location_by_slug(slug)
            if not loc:
                return None, {"ok": False,
                              "error": f"no location: {slug}"}
            return loc, None
        # Default: primary
        locs = self.weather_db.list_locations()
        if not locs:
            return None, {"ok": False,
                          "error": "no locations configured"}
        return locs[0], None

    def _http_current(self, payload):
        loc, err = self._resolve_loc(payload)
        if err:
            return err
        latest = self.weather_db.latest_weather(loc["id"])
        return {"ok": True, "location": loc, "observation": latest}

    def _http_sky(self, payload):
        loc, err = self._resolve_loc(payload)
        if err:
            return err
        latest = self.weather_db.latest_sky(loc["id"])
        return {"ok": True, "location": loc, "sky": latest}

    def _http_forecast(self, payload):
        """Returns the hourly forecast block from the most-recent
        observation's raw_json (Open-Meteo includes hourly + daily)."""
        loc, err = self._resolve_loc(payload)
        if err:
            return err
        latest = self.weather_db.latest_weather(loc["id"])
        if not latest:
            return {"ok": True, "location": loc, "forecast": None,
                    "note": "no observation yet — call /poll-now"}
        try:
            raw = json.loads(latest.get("raw_json") or "{}")
        except Exception:
            raw = {}
        return {
            "ok": True,
            "location": loc,
            "hourly": raw.get("hourly"),
            "daily": raw.get("daily"),
            "observed_at": latest.get("observed_at"),
        }

    def _http_history(self, payload):
        loc, err = self._resolve_loc(payload)
        if err:
            return err
        hours = _as_int(payload, "hours", 24, max_value=24 * 30)
        rows = self.weather_db.recent_weather(
            loc["id"], hours=hours, limit=500)
        # Drop raw_json from history rows to keep response small.
        for r in rows:
            r.pop("raw_json", None)
        return {"ok": True, "location": loc, "hours": hours,
                "observations": rows}

    def _http_poll_now(self, payload):
        """Manual trigger — bypasses the cadence wait."""
        slug = str((payload or {}).get("location") or "").strip()
        if not slug:
            # Poll all locations
            result = self._poll_once()
        else:
            loc = self.weather_db.get_location_by_slug(slug)
            if not loc:
                return {"ok": False,
                        "error": f"no location: {slug}"}
            # Reuse loop's per-location poll
            self._refresh_kp_cache()
            try:
                result = self._poll_location(loc)
                self._last_poll_at = _utc_now_iso()
            except Exception as e:
                log.exception("poll_now failed for %s", slug)
                return {"ok": False, "error": str(e)}
        return {"ok": True, "result": result}

    # ── Polling ──────────────────────────────────────────────────

    def _poll_loop(self):
        """Background task — sleeps in small increments so unload is
        quick. Runs until on_unload sets _stop_event."""
        cfg = self.api.config
        delay = int(cfg.get("first_poll_delay_s") or 60)
        interval = int(cfg.get("poll_interval_s") or 3600)
        # Slow start
        self._sleep_with_stop(delay)
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                log.exception("weather poll cycle failed")
                self._poll_errors.append(
                    f"{_utc_now_iso()}: {e}")
                self._poll_errors = self._poll_errors[-20:]
            self._sleep_with_stop(interval)

    def _sleep_with_stop(self, seconds):
        end = time.monotonic() + max(1, int(seconds))
        while time.monotonic() < end:
            if self._stop_event.is_set():
                return
            time.sleep(min(5, end - time.monotonic()))

    def _poll_once(self) -> dict:
        """Poll every location. Returns per-location result list."""
        if not self.weather_db:
            return {"polled": 0}
        self._refresh_kp_cache()
        results = []
        for loc in self.weather_db.list_locations():
            try:
                results.append(self._poll_location(loc))
            except Exception as e:
                log.warning("poll location %s failed: %s",
                            loc.get("slug"), e)
                results.append({"slug": loc.get("slug"), "ok": False,
                                "error": str(e)})
        self._last_poll_at = _utc_now_iso()
        self._poll_count += 1
        return {"polled": len(results), "results": results}

    def _refresh_kp_cache(self):
        """Pull Kp once per cycle (geomagnetic data is GLOBAL)."""
        cfg = self.api.config
        if not bool(cfg.get("source_noaa_swpc_enabled", True)):
            self._last_kp = {}
            return
        try:
            self._last_kp = noaa_swpc.fetch_kp(
                timeout=float(cfg.get("http_timeout_s") or 20))
        except Exception as e:
            log.warning("Kp fetch failed: %s", e)
            self._last_kp = {}

    def _poll_location(self, loc) -> dict:
        """Pull weather + sky data for one location, write rows."""
        cfg = self.api.config
        timeout = float(cfg.get("http_timeout_s") or 20)
        result = {"slug": loc["slug"], "ok": True}

        # Weather (Open-Meteo)
        weather_payload = None
        if bool(cfg.get("source_open_meteo_enabled", True)):
            try:
                weather_payload = open_meteo.fetch_current(
                    lat=float(loc["lat"]),
                    lon=float(loc["lon"]),
                    timezone=loc.get("timezone") or "UTC",
                    timeout=timeout,
                )
                self.weather_db.add_weather_observation(
                    location_id=loc["id"],
                    **weather_payload,
                )
                result["weather"] = {
                    "ok": True,
                    "temp_c": weather_payload.get("temp_c"),
                    "cloud_pct": weather_payload.get("cloud_cover_pct"),
                    "conditions": weather_payload.get("conditions_text"),
                }
            except Exception as e:
                log.warning("open-meteo poll for %s failed: %s",
                            loc["slug"], e)
                result["weather"] = {"ok": False, "error": str(e)}

        # Sky observation: astronomy + Kp + cloud cover snapshot
        try:
            now_utc = _dt.datetime.now(_dt.timezone.utc)
            moon = astronomy.moon_phase(now_utc)
            # Fetch sunrise/sunset/twilight from API, cached per day.
            sun_key = (loc["slug"], now_utc.date().isoformat())
            if sun_key in self._sun_cache:
                sun = self._sun_cache[sun_key]
            else:
                try:
                    sun = sunrise_sunset.fetch_sun_times(
                        float(loc["lat"]), float(loc["lon"]),
                        date=now_utc.date(),
                        timeout=timeout,
                    )
                    self._sun_cache[sun_key] = sun
                    # Trim cache so it can't grow unbounded — keep
                    # most recent 30 entries.
                    if len(self._sun_cache) > 30:
                        oldest = sorted(self._sun_cache.keys(),
                                        key=lambda k: k[1])[:-30]
                        for k in oldest:
                            self._sun_cache.pop(k, None)
                except Exception as e:
                    log.warning("sunrise-sunset.org failed for %s: %s",
                                loc["slug"], e)
                    sun = {
                        "sunrise_utc": "", "sunset_utc": "",
                        "civil_twilight_end_utc": "",
                        "astronomical_twilight_end_utc": "",
                    }
            cloud = (weather_payload or {}).get("cloud_cover_pct")
            vis = (weather_payload or {}).get("visibility_m")
            kp = self._last_kp.get("kp")
            aurora = noaa_swpc.aurora_signal_for_location(
                kp, loc["lat"]) if kp is not None else ""
            self.weather_db.add_sky_observation(
                location_id=loc["id"],
                observed_at=_utc_now_iso(),
                local_observed_at=_local_iso(
                    loc.get("timezone") or "UTC"),
                raw_json=json.dumps({
                    "moon": moon,
                    "sun": sun,
                    "kp": self._last_kp,
                }),
                moon_phase=moon["phase"],
                moon_phase_name=moon["phase_name"],
                moon_illumination_pct=moon["illumination_pct"],
                sunrise_utc=sun["sunrise_utc"],
                sunset_utc=sun["sunset_utc"],
                civil_twilight_end_utc=sun["civil_twilight_end_utc"],
                astronomical_twilight_end_utc=sun[
                    "astronomical_twilight_end_utc"],
                kp_index=kp,
                kp_category=self._last_kp.get("kp_category", ""),
                aurora_forecast=aurora,
                cloud_cover_pct=cloud,
                visibility_m=vis,
            )
            result["sky"] = {
                "ok": True,
                "moon_phase_name": moon["phase_name"],
                "illumination_pct": moon["illumination_pct"],
                "kp": kp,
                "kp_category": self._last_kp.get("kp_category", ""),
                "aurora": aurora,
            }
        except Exception as e:
            log.warning("sky obs for %s failed: %s",
                        loc["slug"], e)
            result["sky"] = {"ok": False, "error": str(e)}

        self.weather_db.touch_location_polled(loc["id"])
        return result


# Required by the runtime: top-level callable that returns the plugin.
def register(api):
    return WeatherPlugin(api)
