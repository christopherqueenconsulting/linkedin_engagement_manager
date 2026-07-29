"""City/state → lat/long/timezone geocoding for the Login Location feature.

Users pick a city/state (and admins align a user to their purchased proxy's city); the
browser's emulated geolocation/timezone/locale must match the proxy IP's location to avoid
LinkedIn "new location" suspicion. Uses free OpenStreetMap Nominatim for city→lat/long and
the offline `timezonefinder` for lat/long→IANA timezone (no API key, deterministic).

Nominatim usage policy: a descriptive User-Agent is required and requests are limited to
~1/sec. Both are handled here (module-level cache + min-interval throttle); volume is tiny
(occasional user/admin clicks). Complementary to the IP-based ipapi.co autocapture — do not
remove that; this is the reverse (place → coordinates).
"""

import os
import threading
import time
from typing import Optional

import requests

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = os.getenv(
    "NOMINATIM_USER_AGENT",
    "LinkedInEngagementManager/1.0 (+https://lem.christopherqueenconsulting.com)")
_MIN_INTERVAL_S = 1.1  # Nominatim policy: <= 1 req/sec

_cache: dict = {}
_lock = threading.Lock()
_last_call = 0.0

# Country ISO-2 → default locale (browser locale override). US default matches selenium_util.
_LOCALE_BY_COUNTRY = {"US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU"}

DEFAULT_CONTENT_LANGUAGE = "en-US"

# Language subtag → the name a generative model actually understands in a prompt. Veo has no
# language parameter, so the language reaches it only as prompt text (issue #548) — "Spanish"
# steers it, "es-ES" is far less reliable.
_LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
    "it": "Italian", "nl": "Dutch", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "pl": "Polish", "ru": "Russian", "tr": "Turkish", "ar": "Arabic",
    "he": "Hebrew", "hi": "Hindi", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "uk": "Ukrainian", "cs": "Czech",
    "el": "Greek", "ro": "Romanian", "hu": "Hungarian",
}


class GeocodeError(Exception):
    """Raised when a place can't be resolved to coordinates."""


def _locale_for(country_code: Optional[str]) -> str:
    if not country_code:
        return DEFAULT_CONTENT_LANGUAGE
    return _LOCALE_BY_COUNTRY.get(country_code.upper(), f"en-{country_code.upper()}")


def language_name(locale: Optional[str]) -> str:
    """Human-readable language name for a BCP-47 tag, for use inside model prompts.

    Regional variants stay visible ("Portuguese (pt-BR)") because they change the audio a
    model produces; an unknown tag is returned as-is rather than guessed at.
    """
    tag = (locale or DEFAULT_CONTENT_LANGUAGE).strip()
    if not tag:
        tag = DEFAULT_CONTENT_LANGUAGE
    subtag = tag.replace("_", "-").split("-")[0].lower()
    name = _LANGUAGE_NAMES.get(subtag)
    if not name:
        return tag
    return f"{name} ({tag})" if "-" in tag.replace("_", "-") else name


def _timezone_for(lat: float, lng: float) -> Optional[str]:
    try:
        from timezonefinder import TimezoneFinder
        return TimezoneFinder().timezone_at(lat=lat, lng=lng)
    except Exception:
        return None


def geocode_city(city: str, state: Optional[str] = None, country: Optional[str] = None) -> dict:
    """Resolve a city/state/country to a geo dict ready for `update_user_location`.

    Returns {latitude, longitude, city, country (ISO-2), timezone (IANA), locale}.
    Raises GeocodeError if the place can't be found or the geocoder is unreachable.
    """
    if not city or not city.strip():
        raise GeocodeError("City is required")

    key = "|".join((city.strip().lower(), (state or "").strip().lower(), (country or "").strip().lower()))
    with _lock:
        if key in _cache:
            return dict(_cache[key])

    params = {"format": "jsonv2", "addressdetails": 1, "limit": 1, "city": city.strip()}
    if state:
        params["state"] = state.strip()
    if country:
        params["country"] = country.strip()

    global _last_call
    with _lock:
        wait = _MIN_INTERVAL_S - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()  # lgtm[py/unused-global-variable]

    try:
        resp = requests.get(_NOMINATIM_URL, params=params,
                            headers={"User-Agent": _USER_AGENT}, timeout=8)
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        raise GeocodeError(f"Geocoding request failed: {e}")

    if not results:
        raise GeocodeError(f"No match for city={city!r} state={state!r} country={country!r}")

    top = results[0]
    try:
        lat, lng = float(top["lat"]), float(top["lon"])
    except (KeyError, TypeError, ValueError):
        raise GeocodeError("Geocoder returned no coordinates")

    cc = ((top.get("address") or {}).get("country_code") or country or "").upper() or None
    geo = {
        "latitude": lat,
        "longitude": lng,
        "city": city.strip(),
        "country": cc,
        "timezone": _timezone_for(lat, lng),
        "locale": _locale_for(cc),
    }
    with _lock:
        _cache[key] = dict(geo)
    return geo
