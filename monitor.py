#!/usr/bin/env python3
"""
PVR Koramangala IMAX Ticket Availability Monitor
Monitors BookMyShow for "Hail Mary" IMAX shows at PVR Koramangala
and sends Telegram alerts when tickets open.

Usage:
    python3 monitor.py          # reads credentials from .env automatically
    # or export manually:
    export TELEGRAM_BOT_TOKEN="<your-bot-token>"
    export TELEGRAM_CHAT_ID="<your-chat-id>"
    python3 monitor.py

.env / env vars:
    TELEGRAM_BOT_TOKEN  - Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID    - chat/channel/group ID to send alerts to
    CHECK_INTERVAL      - seconds between checks (default: 120)
    TARGET_DATE         - YYYYMMDD date to check (default: 20260403)
    BMS_CITY_CODE       - BookMyShow city code (default: BANG)

NOTE: Requires 'cloudscraper' and 'beautifulsoup4' packages:
    pip install cloudscraper beautifulsoup4
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import cloudscraper
import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Load .env file if present (no python-dotenv dependency needed)
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
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL", "120"))   # seconds
TARGET_DATE        = os.environ.get("TARGET_DATE", "20260403")       # default: Apr 3 2026
CITY_CODE          = os.environ.get("BMS_CITY_CODE", "BANG")

MOVIE_NAME_KEYWORDS   = ["hail mary", "hailmary"]
TARGET_VENUE_KEYWORDS = ["pvr koramangala", "koramangala"]
TARGET_FORMAT_KEYWORDS = ["imax"]

BMS_BASE  = "https://in.bookmyshow.com"
BMS_CITY_SLUG = "bengaluru"

# Human-readable date label for messages
_DATE_LABELS = {
    "20260402": "Wednesday, April 2, 2026",
    "20260403": "Friday, April 3, 2026",
}
DATE_LABEL = _DATE_LABELS.get(TARGET_DATE, TARGET_DATE)


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
# CloudScraper session (handles Cloudflare JS challenges)
# ---------------------------------------------------------------------------

def _make_scraper() -> cloudscraper.CloudScraper:
    sc = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    sc.headers.update({
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{BMS_BASE}/{BMS_CITY_SLUG}/movies",
        "X-Region-Code": CITY_CODE,
        "X-Region-Slug": BMS_CITY_SLUG,
    })
    return sc

SCRAPER = _make_scraper()


def _bms_get(path: str, params: dict | None = None, timeout: int = 25) -> dict | list | None:
    """GET a BMS path; return parsed JSON or None on failure."""
    url = f"{BMS_BASE}{path}"
    try:
        resp = SCRAPER.get(url, params=params, timeout=timeout)
        if resp.status_code == 403:
            log.debug("BMS 403 on %s — Cloudflare block (expected on server IPs)", path)
            return None
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "json" not in ct:
            log.debug("Non-JSON response from %s: %s", path, ct)
            return None
        return resp.json()
    except cloudscraper.exceptions.CloudflareChallengeError:
        log.warning("Cloudflare challenge not solved for %s (server IP blocked)", path)
    except requests.HTTPError as exc:
        log.warning("HTTP error %s: %s", path, exc)
    except requests.RequestException as exc:
        log.warning("Request error %s: %s", path, exc)
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("JSON parse error %s: %s", path, exc)
    return None


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> bool:
    """Send a Telegram message. Returns True on success."""
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
# Strategy 1: BMS QUICKBOOK API
# ---------------------------------------------------------------------------

def _warm_session() -> None:
    """Hit the BMS homepage once to get cookies before API calls."""
    try:
        SCRAPER.get(f"{BMS_BASE}/{BMS_CITY_SLUG}/movies", timeout=20)
    except Exception:
        pass


def fetch_quickbook_shows() -> list[dict]:
    """
    Use the BMS QUICKBOOK endpoint to get all movies + venues for the city.
    Returns a flat list of venue+show dicts for the target movie on TARGET_DATE.
    """
    _warm_session()
    data = _bms_get(
        "/serv/getData",
        params={
            "cmd": "QUICKBOOK",
            "appCode": "MOBAND2",
            "appVersion": "14380",
            "language": "en",
            "regionCode": CITY_CODE,
            "date": TARGET_DATE,
        },
    )
    if not data:
        return []

    # Navigate the BMS nested response
    # Typical shape: {"BookMyShow": {"arrEvents": [{"EventTitle":..., "Venues":[...]}]}}
    root = data.get("BookMyShow") or data
    events = root.get("arrEvents") or root.get("Events") or []
    if not isinstance(events, list):
        return []

    results = []
    for event in events:
        title = (event.get("EventTitle") or event.get("EventName") or "").lower()
        if not any(kw in title for kw in MOVIE_NAME_KEYWORDS):
            continue
        log.info("QUICKBOOK: found movie '%s'", event.get("EventTitle") or event.get("EventName"))
        venues = event.get("Venues") or event.get("arrVenues") or []
        for venue in venues:
            vname = (venue.get("VenueName") or "").lower()
            if not any(kw in vname for kw in TARGET_VENUE_KEYWORDS):
                continue
            results.extend(_extract_imax_shows(venue))
    return results


# ---------------------------------------------------------------------------
# Strategy 2: BMS movie-list + showtimes-by-event API
# ---------------------------------------------------------------------------

def _fetch_movie_event_code() -> str | None:
    """
    Fetch the BMS base event code for 'Project Hail Mary' by scraping the
    explore/movies-in-bengaluru page (the /api/explore/v1/movies endpoint is 404).
    """
    try:
        resp = SCRAPER.get(f"{BMS_BASE}/explore/movies-in-bengaluru", timeout=20)
        if resp.status_code != 200:
            log.debug("explore page returned %d", resp.status_code)
            return None
    except Exception as exc:
        log.warning("explore page fetch failed: %s", exc)
        return None

    # The page embeds URLs like: /bengaluru/movies/project-hail-mary/ET00451760
    for kw in MOVIE_NAME_KEYWORDS:
        slug = kw.replace(" ", "-")
        match = re.search(
            rf'/bengaluru/movies/[^"]*{re.escape(slug)}[^"/]*/?(ET\d+)',
            resp.text,
            re.IGNORECASE,
        )
        if match:
            code = match.group(1)
            log.info("explore page: found movie code=%s", code)
            return code
    return None


def _fetch_imax_event_code(base_event_code: str) -> str | None:
    """
    Query showtimes-by-event for the base event and extract the IMAX child event code
    from the ChildEvents list in the response.
    """
    data = _bms_get(
        "/api/movies-data/showtimes-by-event",
        params={
            "appCode": "MOBAND2",
            "appVersion": "14380",
            "language": "en",
            "eventCode": base_event_code,
            "regionCode": CITY_CODE,
            "subRegion": CITY_CODE,
            "date": TARGET_DATE,
        },
    )
    if not data:
        return None
    show_details = data.get("ShowDetails") or []
    if not show_details:
        return None
    child_events = (show_details[0].get("Event") or {}).get("ChildEvents") or []
    for ce in child_events:
        title = (ce.get("EventTitle") or "").lower()
        code  = ce.get("EventCode") or ""
        if any(kw in title for kw in TARGET_FORMAT_KEYWORDS) and code:
            log.info("Found IMAX child event: '%s' code=%s", ce.get("EventTitle"), code)
            return str(code)
    return None


def fetch_shows_by_event(event_code: str) -> list[dict]:
    """
    Fetch venues + shows for a specific BMS event code on TARGET_DATE.
    Actual response shape: {"ShowDetails": [{"Venues": [...], "Event": {...}}]}
    """
    data = _bms_get(
        "/api/movies-data/showtimes-by-event",
        params={
            "appCode": "MOBAND2",
            "appVersion": "14380",
            "language": "en",
            "eventCode": event_code,
            "regionCode": CITY_CODE,
            "subRegion": CITY_CODE,
            "date": TARGET_DATE,
        },
    )
    if not data:
        return []

    show_details = data.get("ShowDetails") or []
    if not show_details:
        return []
    venues = show_details[0].get("Venues") or []

    results = []
    for venue in venues:
        vname = (venue.get("VenueName") or "").lower()
        if any(kw in vname for kw in TARGET_VENUE_KEYWORDS):
            results.extend(_extract_shows_from_venue(venue))
    return results


# ---------------------------------------------------------------------------
# Strategy 3: HTML scrape of BMS venue page
# ---------------------------------------------------------------------------

def fetch_shows_via_html() -> list[dict]:
    """
    Scrape the BMS city movies page HTML and look for embedded __NEXT_DATA__
    or JSON blobs that mention Hail Mary + IMAX + Koramangala.
    Returns list of show dicts on match.
    """
    _warm_session()
    try:
        resp = SCRAPER.get(
            f"{BMS_BASE}/explore/movies-in-bengaluru",
            timeout=25,
        )
        if resp.status_code not in (200, 404):
            log.debug("HTML scrape got status %d", resp.status_code)
            return []
    except Exception as exc:
        log.warning("HTML scrape request failed: %s", exc)
        return []

    text_lower = resp.text.lower()
    has_movie  = any(kw in text_lower for kw in MOVIE_NAME_KEYWORDS)
    has_imax   = any(kw in text_lower for kw in TARGET_FORMAT_KEYWORDS)
    has_venue  = any(kw in text_lower for kw in TARGET_VENUE_KEYWORDS)

    if has_movie and has_imax and has_venue:
        log.info("HTML scrape: found Hail Mary + IMAX + Koramangala on BMS page!")
        # Try to extract show times from embedded JSON (__NEXT_DATA__)
        soup = BeautifulSoup(resp.text, "lxml")
        next_data_tag = soup.find("script", id="__NEXT_DATA__")
        if next_data_tag:
            try:
                page_data = json.loads(next_data_tag.string or "")
                return _scan_json_for_shows(page_data)
            except (ValueError, AttributeError):
                pass
        # Fallback: return a synthetic match to trigger alert
        return [{"venue": "PVR Koramangala", "screen": "IMAX", "time": "?", "availability": "available",
                 "booking_url": f"{BMS_BASE}/explore/movies-in-bengaluru"}]
    return []


def _scan_json_for_shows(data: object, depth: int = 0) -> list[dict]:
    """Recursively scan a JSON blob for show objects that match our criteria."""
    if depth > 10:
        return []
    results = []
    if isinstance(data, dict):
        title = str(data.get("EventTitle") or data.get("name") or "").lower()
        vname = str(data.get("VenueName") or data.get("venue") or "").lower()
        fmt   = str(data.get("ScreenName") or data.get("format") or "").lower()
        if (any(kw in title for kw in MOVIE_NAME_KEYWORDS)
                and any(kw in vname for kw in TARGET_VENUE_KEYWORDS)
                and any(kw in fmt   for kw in TARGET_FORMAT_KEYWORDS)):
            results.append({
                "venue": data.get("VenueName") or data.get("venue"),
                "screen": data.get("ScreenName") or data.get("format"),
                "time": data.get("ShowTime") or data.get("showTime") or "?",
                "availability": "available",
                "booking_url": f"{BMS_BASE}/{BMS_CITY_SLUG}/movies",
            })
        for v in data.values():
            results.extend(_scan_json_for_shows(v, depth + 1))
    elif isinstance(data, list):
        for item in data:
            results.extend(_scan_json_for_shows(item, depth + 1))
    return results


# ---------------------------------------------------------------------------
# Shared: extract IMAX shows from a venue block
# ---------------------------------------------------------------------------

def _extract_imax_shows(venue: dict) -> list[dict]:
    """Walk a venue dict (QUICKBOOK format) and return all IMAX show slots."""
    results = []
    categories = (
        venue.get("ShowDetails")
        or venue.get("arrShowDetails")
        or venue.get("Categories")
        or []
    )
    if not isinstance(categories, list):
        categories = [categories]

    for cat in categories:
        fmt = (
            cat.get("ScreenName") or cat.get("ShowType") or cat.get("CategoryName") or ""
        ).lower()
        if not any(kw in fmt for kw in TARGET_FORMAT_KEYWORDS):
            continue

        shows = cat.get("ShowTimes") or cat.get("arrShowTimes") or []
        if not isinstance(shows, list):
            shows = [shows]

        for show in shows:
            avail = (show.get("ShowAvailability") or "A").upper()
            # A = available, S = sold out, N = not yet bookable
            if avail in ("S", "N"):
                continue
            show_id = show.get("ShowId") or show.get("showId") or ""
            results.append({
                "venue":        venue.get("VenueName") or venue.get("venueName"),
                "screen":       cat.get("ScreenName") or "IMAX",
                "time":         show.get("ShowTime") or show.get("showTime") or "?",
                "availability": avail,
                "booking_url":  f"{BMS_BASE}/buytickets/{show_id}" if show_id
                                else f"{BMS_BASE}/explore/movies-in-bengaluru",
            })
    return results


def _extract_shows_from_venue(venue: dict) -> list[dict]:
    """
    Extract shows from a venue dict as returned by /api/movies-data/showtimes-by-event.
    Response shape: {"VenueName": "...", "ShowTimes": [{"ShowDateTime": "YYYYMMDDHHSS"}]}
    ShowDateTime is 12 digits: YYYYMMDDHHMI
    """
    results = []
    venue_code = venue.get("VenueCode") or ""
    shows = venue.get("ShowTimes") or []
    for show in shows:
        raw_dt = show.get("ShowDateTime") or ""
        # Format: 202604031130 → "11:30"
        if len(raw_dt) == 12:
            show_time = f"{raw_dt[8:10]}:{raw_dt[10:12]}"
        else:
            show_time = raw_dt or "?"
        results.append({
            "venue":        venue.get("VenueName"),
            "screen":       "IMAX 2D",
            "time":         show_time,
            "availability": "available",
            "booking_url":  (
                f"{BMS_BASE}/bengaluru/movies/project-hail-mary-imax-2d/{venue_code}"
                if venue_code
                else f"{BMS_BASE}/explore/movies-in-bengaluru"
            ),
        })
    return results


# ---------------------------------------------------------------------------
# Main check orchestrator
# ---------------------------------------------------------------------------

def format_alert(shows: list[dict]) -> str:
    lines = [
        "🎬 <b>IMAX Hail Mary tickets are LIVE!</b>",
        f"📅 {DATE_LABEL}",
        "📍 PVR Koramangala, Bengaluru",
        "",
        "<b>Available shows:</b>",
    ]
    for show in shows:
        t    = show.get("time") or "?"
        scr  = show.get("screen") or "IMAX"
        url  = show.get("booking_url") or BMS_BASE
        lines.append(f"  • {scr}  {t}  — <a href='{url}'>Book now</a>")
    lines += [
        "",
        f"🔗 <a href='{BMS_BASE}/explore/movies-in-bengaluru'>BookMyShow Bengaluru</a>",
    ]
    return "\n".join(lines)


def run_check() -> bool:
    """
    Run one full check cycle using all three strategies in order.
    Returns True if available tickets were found (and alert was sent).
    """
    log.info("Checking BMS for Hail Mary IMAX @ PVR Koramangala on %s…", TARGET_DATE)

    # Strategy 1: QUICKBOOK
    shows = fetch_quickbook_shows()
    if shows:
        log.info("TICKETS FOUND via QUICKBOOK! %d IMAX slot(s).", len(shows))
        send_telegram(format_alert(shows))
        return True

    # Strategy 2: explore page scrape → discover IMAX child event code → showtimes-by-event
    base_code = _fetch_movie_event_code()
    if base_code:
        imax_code = _fetch_imax_event_code(base_code)
        check_code = imax_code or base_code
        shows = fetch_shows_by_event(check_code)
        if shows:
            log.info("TICKETS FOUND via showtimes API! %d IMAX slot(s).", len(shows))
            send_telegram(format_alert(shows))
            return True
        log.info("Movie found (base=%s imax=%s) but no IMAX shows at PVR Koramangala yet.",
                 base_code, imax_code)
        return False

    # Strategy 3: HTML scrape
    shows = fetch_shows_via_html()
    if shows:
        log.info("TICKETS FOUND via HTML scrape! Sending alert.")
        send_telegram(format_alert(shows))
        return True

    log.info("'Hail Mary' not yet listed / no IMAX slots available. Will retry.")
    return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set.")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_CHAT_ID is not set.")
        sys.exit(1)

    log.info("=" * 60)
    log.info("Hail Mary IMAX Monitor — PVR Koramangala | %s", DATE_LABEL)
    log.info("Check interval: %ds  |  Target date: %s", CHECK_INTERVAL, TARGET_DATE)
    log.info("=" * 60)

    send_telegram(
        "🤖 <b>Monitor started!</b>\n"
        f"Watching <b>Hail Mary IMAX</b> @ PVR Koramangala\n"
        f"📅 {DATE_LABEL} · checking every {CHECK_INTERVAL}s"
    )

    while True:
        try:
            found = run_check()
            if found:
                time.sleep(CHECK_INTERVAL * 5)   # back off after alert
            else:
                time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as exc:
            log.exception("Unexpected error: %s", exc)
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
