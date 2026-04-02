#!/usr/bin/env python3
"""
PVR Koramangala IMAX Ticket Availability Monitor
Monitors BookMyShow for "Hail Mary" IMAX shows at PVR Koramangala
on Friday April 3, 2026 and sends Telegram alerts when tickets open.

Usage:
    python3 monitor.py          # reads credentials from .env automatically
    # or set env vars manually:
    export TELEGRAM_BOT_TOKEN="<your-bot-token>"
    export TELEGRAM_CHAT_ID="<your-chat-id>"
    python3 monitor.py

Optional env vars / .env keys:
    CHECK_INTERVAL   - seconds between checks (default: 120)
    BMS_CITY_CODE    - BookMyShow city code (default: BANG)
"""

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests


# ---------------------------------------------------------------------------
# Load .env file if present (no external dependency needed)
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env") -> None:
    env_file = Path(__file__).parent / path
    if not env_file.exists():
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

_load_dotenv()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "120"))  # seconds

# Target show details
MOVIE_NAME_KEYWORDS = ["hail mary", "hailmary"]
TARGET_VENUE_KEYWORDS = ["pvr koramangala", "koramangala"]
TARGET_FORMAT_KEYWORDS = ["imax"]
TARGET_DATE = "20260403"         # YYYYMMDD — Friday April 3, 2026
CITY_CODE = os.environ.get("BMS_CITY_CODE", "BANG")
CITY_SLUG = "bengaluru"

BMS_BASE = "https://in.bookmyshow.com"
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://in.bookmyshow.com/",
        "X-Region-Code": CITY_CODE,
        "X-Region-Slug": CITY_SLUG,
    }
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def send_telegram(message: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set — skipping notification.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200 and resp.json().get("ok"):
            log.info("Telegram alert sent successfully.")
            return True
        log.error("Telegram API error: %s", resp.text)
    except requests.RequestException as exc:
        log.error("Failed to send Telegram message: %s", exc)
    return False


# ---------------------------------------------------------------------------
# BookMyShow API helpers
# ---------------------------------------------------------------------------


def _get(url: str, params: dict | None = None, timeout: int = 20) -> dict | list | None:
    """GET a BMS endpoint; return parsed JSON or None on failure."""
    try:
        resp = SESSION.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        log.warning("HTTP error fetching %s: %s", url, exc)
    except requests.RequestException as exc:
        log.warning("Request error fetching %s: %s", url, exc)
    except ValueError as exc:
        log.warning("JSON parse error for %s: %s", url, exc)
    return None


def fetch_now_showing_movies() -> list[dict]:
    """
    Fetch currently showing / upcoming movies for the configured city.
    BMS exposes a movie list endpoint used by the website's home/explore pages.
    """
    url = f"{BMS_BASE}/api/explore/v1/movies"
    params = {
        "appCode": "MOBAND2",
        "appVersion": "14380",
        "language": "en",
        "status": "nowShowing,comingSoon",
        "regionCode": CITY_CODE,
        "city": "Bengaluru",
    }
    data = _get(url, params=params)
    if isinstance(data, dict):
        # Response is usually {"BookMyShow": {"arrEvents": [...]}}
        movies = (
            data.get("BookMyShow", {}).get("arrEvents")
            or data.get("arrEvents")
            or []
        )
        return movies if isinstance(movies, list) else []
    return []


def find_movie_event_code(movies: list[dict]) -> str | None:
    """Return the BMS event code for the target movie, or None if not found."""
    for movie in movies:
        # BMS uses 'EventTitle' or 'EventName' depending on endpoint version
        title = (
            movie.get("EventTitle") or movie.get("EventName") or ""
        ).lower()
        code = movie.get("EventCode") or movie.get("EventId") or ""
        if any(kw in title for kw in MOVIE_NAME_KEYWORDS) and code:
            log.info("Found movie: %r  code=%r", movie.get("EventTitle") or movie.get("EventName"), code)
            return str(code)
    return None


def fetch_venue_shows(event_code: str) -> list[dict]:
    """
    Fetch all venues showing the movie in the city on the target date.
    Returns a list of venue show objects.
    """
    url = f"{BMS_BASE}/api/movies-data/showtimes-by-event"
    params = {
        "appCode": "MOBAND2",
        "appVersion": "14380",
        "language": "en",
        "eventCode": event_code,
        "regionCode": CITY_CODE,
        "subRegion": CITY_CODE,
        "bmsId": "1.21345445.1703675240",
        "token": "67x1xa33b4x4ba0x4x5c247b0x7",
        "date": TARGET_DATE,
    }
    data = _get(url, params=params)
    if isinstance(data, dict):
        venues = (
            data.get("ShowDetails")
            or data.get("arrVenueDetails")
            or data.get("Venues")
            or []
        )
        return venues if isinstance(venues, list) else []
    return []


def check_imax_availability(venues: list[dict]) -> list[dict]:
    """
    Filter down to PVR Koramangala IMAX shows that have seats available.
    Returns a list of matching show-session dicts (empty = not available yet).
    """
    matches = []
    for venue in venues:
        venue_name = (venue.get("VenueName") or venue.get("venueName") or "").lower()

        # Check if this is PVR Koramangala
        if not any(kw in venue_name for kw in TARGET_VENUE_KEYWORDS):
            continue

        # Walk show categories / sessions
        categories = (
            venue.get("ShowDetails")
            or venue.get("arrShowDetails")
            or venue.get("Categories")
            or []
        )
        if not isinstance(categories, list):
            categories = [categories]

        for category in categories:
            format_name = (
                category.get("ScreenName")
                or category.get("ShowType")
                or category.get("CategoryName")
                or ""
            ).lower()

            if not any(kw in format_name for kw in TARGET_FORMAT_KEYWORDS):
                continue

            shows = (
                category.get("ShowTimes")
                or category.get("arrShowTimes")
                or []
            )
            if not isinstance(shows, list):
                shows = [shows]

            for show in shows:
                availability = (show.get("ShowAvailability") or "").lower()
                # BMS uses: "A" = available, "S" = sold out, "N" = not yet open
                if availability not in ("s", "n", "sold out", "housefull"):
                    matches.append(
                        {
                            "venue": venue.get("VenueName") or venue.get("venueName"),
                            "screen": category.get("ScreenName") or category.get("ShowType"),
                            "time": show.get("ShowTime") or show.get("showTime"),
                            "availability": availability,
                            "booking_url": build_booking_url(venue, show),
                        }
                    )
    return matches


def build_booking_url(venue: dict, show: dict) -> str:
    """Construct a direct BMS booking URL for the show."""
    venue_code = venue.get("VenueCode") or venue.get("venueCode") or ""
    show_id = show.get("ShowId") or show.get("showId") or ""
    if show_id:
        return f"{BMS_BASE}/buytickets/{show_id}"
    if venue_code:
        return f"{BMS_BASE}/bengaluru/movies"
    return f"{BMS_BASE}/bengaluru/movies"


# ---------------------------------------------------------------------------
# Fallback: direct venue page scrape
# ---------------------------------------------------------------------------


def check_via_venue_page() -> bool:
    """
    Fallback check by fetching the BMS venue page for PVR Koramangala and
    looking for IMAX + "Hail Mary" keywords.
    Returns True if IMAX Hail Mary tickets appear to be listed.
    """
    # PVR INOX Koramangala venue code on BMS is typically BNGPVRK or similar.
    # We do a broad search on the city movie page filtered by movie name.
    search_url = f"{BMS_BASE}/api/movies-data/search"
    params = {
        "appCode": "MOBAND2",
        "appVersion": "14380",
        "language": "en",
        "regionCode": CITY_CODE,
        "query": "Hail Mary",
    }
    data = _get(search_url, params=params)
    if data:
        raw = str(data).lower()
        if any(kw in raw for kw in MOVIE_NAME_KEYWORDS):
            log.info("Fallback search: 'Hail Mary' found in BMS search results.")
            return True
    return False


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------


def format_alert(shows: list[dict]) -> str:
    """Build a Telegram alert message for available shows."""
    lines = [
        "🎬 <b>IMAX Hail Mary tickets are LIVE!</b>",
        f"📅 Friday, April 3, 2026",
        f"📍 PVR Koramangala, Bengaluru",
        "",
        "<b>Available shows:</b>",
    ]
    for show in shows:
        time_str = show.get("time") or "?"
        screen = show.get("screen") or "IMAX"
        url = show.get("booking_url") or BMS_BASE
        lines.append(f"  • {screen}  {time_str}  — <a href='{url}'>Book now</a>")
    lines += [
        "",
        f"🔗 <a href='https://in.bookmyshow.com/bengaluru/movies'>BookMyShow Bengaluru</a>",
    ]
    return "\n".join(lines)


def run_check() -> bool:
    """
    Run a single availability check cycle.
    Returns True if tickets were found (caller should alert and possibly stop).
    """
    log.info("Checking BMS for Hail Mary IMAX shows at PVR Koramangala on 2026-04-03…")

    movies = fetch_now_showing_movies()
    if not movies:
        log.warning("Could not fetch movie list from BMS (movie might not be listed yet).")
        # Try fallback
        found_via_fallback = check_via_venue_page()
        if found_via_fallback:
            msg = (
                "🎬 <b>Hail Mary</b> appears on BookMyShow!\n"
                "📍 PVR Koramangala · IMAX · April 3, 2026\n\n"
                "Please check manually:\n"
                "🔗 <a href='https://in.bookmyshow.com/bengaluru/movies'>BookMyShow Bengaluru</a>"
            )
            send_telegram(msg)
            return True
        return False

    event_code = find_movie_event_code(movies)
    if not event_code:
        log.info("'Hail Mary' not yet listed on BMS for Bengaluru.")
        return False

    venues = fetch_venue_shows(event_code)
    if not venues:
        log.info("No venue show data returned for event %s on %s.", event_code, TARGET_DATE)
        return False

    available = check_imax_availability(venues)
    if available:
        log.info("TICKETS FOUND! %d IMAX slot(s) available.", len(available))
        send_telegram(format_alert(available))
        return True

    log.info("No IMAX availability at PVR Koramangala yet.")
    return False


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set. Please export it before running.")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_CHAT_ID is not set. Please export it before running.")
        sys.exit(1)

    log.info("=" * 60)
    log.info("Hail Mary IMAX Monitor — PVR Koramangala, April 3 2026")
    log.info("Checking every %d seconds. Ctrl-C to stop.", CHECK_INTERVAL)
    log.info("=" * 60)

    # Send a startup confirmation
    send_telegram(
        "🤖 <b>Ticket monitor started!</b>\n"
        "Watching for <b>Hail Mary IMAX</b> tickets at PVR Koramangala\n"
        f"📅 April 3, 2026 · checking every {CHECK_INTERVAL}s"
    )

    alert_sent = False
    while True:
        try:
            found = run_check()
            if found and not alert_sent:
                alert_sent = True
                log.info("Alert sent. Continuing to monitor in case you missed it.")
                # Keep running so repeat alerts are sent on each check until show is found
                # Reset after one confirmed alert to avoid spam
                time.sleep(CHECK_INTERVAL * 5)  # longer wait after first alert
                alert_sent = False  # reset to re-alert next cycle if still available
            else:
                time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as exc:
            log.exception("Unexpected error during check: %s", exc)
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
