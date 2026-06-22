"""Notification classifier (2026-06-13).

Tiers each captured device_notification deterministically by app, into
one of three tiers:

  - signal  : keep as corpus context (comms, social, interests)
  - ambient : parse into a structured time-series, not kept as prose
              (currently the phone weather widget -> ambient_observations)
  - drop    : device/media chatter, tiered out (Phone Link, Audible, etc.)

Why app-level + deterministic (no LLM): the app a notification comes
from decides ~all of it. Audible/Phone Link/weather/music are noise no
matter what they say; Outlook/SMS/Reddit are signal. Only a few apps
(handled below) need title inspection. This keeps the whole step free.

Tory's rulings folded in (see memory/phone_data_extraction_decisions):
  - Google Messages SMS = SIGNAL, include ALL of it.
  - Weather widget = AMBIENT, "good for ongoing data" — parse temp+time.
  - Phone Link = DROP (verified pure connection-state, carries no SMS).
"""
from __future__ import annotations

import re

# (tier, category) per app package id.
APP_TIERS = {
    # comms (signal) — interpersonal: messages, mail, calls, meetings
    "com.google.android.apps.messaging": ("signal", "comms"),    # SMS
    "com.microsoft.office.outlook":      ("signal", "comms"),     # email
    "com.google.android.gm":             ("signal", "comms"),     # gmail
    "com.google.android.dialer":         ("signal", "comms"),     # calls
    "com.android.server.telecom":        ("signal", "comms"),     # call events
    "com.microsoft.teams":               ("signal", "comms"),
    "org.telegram.messenger":            ("signal", "comms"),
    "com.whatsapp":                      ("signal", "comms"),
    "com.facebook.orca":                 ("signal", "comms"),     # Messenger
    "us.zoom.videomeetings":             ("signal", "comms"),
    "com.google.android.calendar":       ("signal", "comms"),     # invites/events
    "com.google.android.apps.tycho":     ("signal", "comms"),     # Google Fi
    # social (signal)
    "com.reddit.frontpage":              ("signal", "social"),
    "com.twitter.android":               ("signal", "social"),
    "com.discord":                       ("signal", "social"),
    "com.google.android.youtube":        ("signal", "social"),
    "com.zhiliaoapp.musically":          ("signal", "social"),    # tiktok
    # ai (signal) — assistant apps Tory uses
    "com.anthropic.claude":              ("signal", "ai"),
    "ai.x.grok":                         ("signal", "ai"),
    # health (signal) — wearables + digital wellbeing
    "com.fitbit.FitbitMobile":           ("signal", "health"),
    "com.google.android.apps.wellbeing": ("signal", "health"),
    # commerce (signal) — orders + money
    "com.amazon.mShop.android.shopping": ("signal", "commerce"),
    "com.squareup.cash":                 ("signal", "commerce"),
    # travel / media (signal)
    "com.google.android.apps.maps":      ("signal", "travel"),
    "com.google.android.apps.photos":    ("signal", "media"),
    "com.google.android.apps.recorder":  ("signal", "media"),
    # ambient (parsed time-series)
    "com.google.android.apps.weather":   ("ambient", "weather"),  # Google Weather app
    # drop (device / media chatter)
    "com.microsoft.appmanager":          ("drop", "device"),      # Phone Link
    "com.audible.application":           ("drop", "media"),
    "com.google.android.apps.youtube.music": ("drop", "media"),
    "com.google.android.deskclock":      ("drop", "device"),
    "com.google.android.projection.gearhead": ("drop", "device"),  # Android Auto
    "com.google.android.apps.wear.companion": ("drop", "device"),
    "com.android.settings":              ("drop", "device"),
    "com.google.android.odad":           ("drop", "device"),      # Google system app
    "com.oculus.twilight":               ("drop", "device"),      # Quest companion
}

# Unknown apps default to SIGNAL/unknown — keep + flag for review rather
# than silently drop a new signal source. Tory can re-tier later.
DEFAULT = ("signal", "unknown")

# Weather widget title like "72° in Lincoln" / "73°". Require the degree
# sign so a stray "72 in stock" never matches.
_TEMP_RE = re.compile(r"^\s*(\d{1,3})\s*[°°]")
_LOC_RE = re.compile(r"\bin\s+(.+?)\s*$", re.IGNORECASE)


def parse_weather(title: str):
    """Return {'temp_f': int, 'location': str} if the title is a phone
    weather-widget temperature reading, else None."""
    if not title:
        return None
    m = _TEMP_RE.match(title)
    if not m:
        return None
    loc = ""
    lm = _LOC_RE.search(title)
    if lm:
        loc = lm.group(1).strip()
    return {"temp_f": int(m.group(1)), "location": loc}


def classify(app: str, title: str = "", body: str = ""):
    """Return (tier, category) for a notification. Pure + deterministic."""
    app = (app or "").strip()
    title = title or ""
    body = body or ""

    # Google search app = weather widget + Discover/Assistant. Only the
    # parseable temp readings are ambient; the rest is the Discover news
    # feed (sports, headlines, "Today in <city>") -> news.
    if app == "com.google.android.googlequicksearchbox":
        if parse_weather(title):
            return ("ambient", "weather")
        return ("signal", "news")

    # Google Messages foreground-service blip is not a real text.
    if app == "com.google.android.apps.messaging":
        if "doing work in the background" in (title + " " + body).lower():
            return ("drop", "device")
        return ("signal", "comms")

    return APP_TIERS.get(app, DEFAULT)
