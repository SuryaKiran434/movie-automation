#!/usr/bin/env python3
"""
Regal Medlock Crossing — Movie Showtime Monitor
================================================
Monitors regmovies.com for a configurable movie title at Regal Medlock
Crossing on the configured date.  MOVIE_TITLE and TARGET_DATE are set via
.env — no code changes needed to switch movies or dates.

Workflow
--------
1. Read MOVIE_TITLE, TARGET_DATE from .env.
2. Try a lightweight direct HTTPS call to the Regal API (fast path).
3. If Cloudflare blocks it, fall back to a headless Chromium browser.
4. Parse the getShowtimes JSON response for the target movie.
5. If found  → send HTML email + macOS desktop notification + remove cron.
6. If not found → send a daily heartbeat email (once per day) so you know
   the monitor is alive and can see what IS currently scheduled.

Logs → monitor.log (same directory as this script)
"""

import logging
import os
import re
import smtplib
import subprocess
import sys
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

from lock import ProcessLock

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent.resolve()
LOG_FILE       = SCRIPT_DIR / "monitor.log"
SENTINEL_FILE  = SCRIPT_DIR / ".notified"
HEARTBEAT_FILE = SCRIPT_DIR / ".last_heartbeat"

load_dotenv(SCRIPT_DIR / ".env")

# ── Constants ──────────────────────────────────────────────────────────────────
THEATER_CODE = "0354"
THEATER_NAME = "Regal Medlock Crossing"
THEATER_URL  = "https://www.regmovies.com/theatres/regal-medlock-crossing-rpx-0354"
API_BASE     = "https://www.regmovies.com/api/getShowtimes"

# Attributes shown in the email's format column
DISPLAY_ATTRS = {"2D", "3D", "IMAX", "RPX", "4DX", "ScreenX", "Dolby", "4K"}

# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    """Log to monitor.log and stdout at INFO level (audit trail only)."""
    logger = logging.getLogger("movie_monitor")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ── Config loading ─────────────────────────────────────────────────────────────

def load_config(logger: logging.Logger) -> tuple[str, str, str, str, date] | None:
    """
    Read and validate required settings from .env.
    Returns (sender, password, recipient, movie_keyword, target_date) or None.
    """
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        logger.error(".env file not found — create it with the required variables.")
        return None

    sender    = os.environ.get("SENDER_EMAIL", "").strip()
    password  = os.environ.get("SENDER_APP_PASSWORD", "").strip()
    recipient = os.environ.get("RECIPIENT_EMAIL", "").strip()
    movie     = os.environ.get("MOVIE_TITLE", "").strip()
    date_str  = os.environ.get("TARGET_DATE", "").strip()

    missing = [name for name, val in [
        ("SENDER_EMAIL",        sender),
        ("SENDER_APP_PASSWORD", password),
        ("RECIPIENT_EMAIL",     recipient),
        ("MOVIE_TITLE",         movie),
        ("TARGET_DATE",         date_str),
    ] if not val]

    if missing:
        logger.error(
            "Missing required .env variable(s): %s — open .env and fill in the values.",
            ", ".join(missing),
        )
        return None

    try:
        target_date = datetime.strptime(date_str, "%m-%d-%Y").date()
    except ValueError:
        logger.error(
            "TARGET_DATE %r is not in MM-DD-YYYY format (e.g. 03-27-2026).", date_str
        )
        return None

    return sender, password, recipient, movie, target_date


# ── API fetch: fast path (direct HTTPS) ───────────────────────────────────────

def _fetch_direct(target_date: date, logger: logging.Logger) -> dict | None:
    """
    Attempt a direct HTTPS call to the Regal getShowtimes API.
    Uses THEATER_CODE to construct the URL — no browser needed.

    Returns parsed JSON on success, or None if Cloudflare blocks it (403/503).
    Requires httpx; skips silently if not installed.
    """
    if not _HTTPX_AVAILABLE:
        return None

    url = (
        f"{API_BASE}?theatres={THEATER_CODE}"
        f"&date={target_date.strftime('%m-%d-%Y')}"
        f"&hoCode=&ignoreCache=false&moviesOnly=false"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         THEATER_URL,
        "sec-fetch-dest":  "empty",
        "sec-fetch-mode":  "cors",
        "sec-fetch-site":  "same-origin",
    }
    try:
        with httpx.Client(http2=True, timeout=15) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


# ── API fetch: slow path (headless browser) ───────────────────────────────────

def _fetch_via_browser(target_date: date, logger: logging.Logger) -> dict | None:
    """
    Launch headless Chromium to bypass Cloudflare.

    Navigates directly to the theater URL with the target date as a query
    parameter (?date=MM-DD-YYYY). The Regal React app reads that parameter
    and calls the getShowtimes API client-side. We intercept that response
    via expect_response wrapped around the goto — no date-clicking needed.

    Returns parsed JSON or None on failure.
    """
    url = f"{THEATER_URL}?date={target_date.strftime('%m-%d-%Y')}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        ).new_page()

        try:
            with page.expect_response(
                lambda r: "getShowtimes" in r.url, timeout=45_000
            ) as resp_info:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            data = resp_info.value.json()
        except PlaywrightTimeoutError:
            logger.warning(
                "getShowtimes API did not fire — date may have no schedule published yet."
            )
            data = None
        except Exception as exc:
            logger.error("Browser fetch failed: %s", exc)
            data = None
        finally:
            browser.close()

    return data


def fetch_showtimes(target_date: date, logger: logging.Logger) -> dict | None:
    """
    Fetch showtime data using the fastest available method:
      1. Direct HTTPS call (fast, ~1s) — works if Cloudflare permits it.
      2. Headless browser fallback (~8s) — used when Cloudflare blocks #1.
    """
    data = _fetch_direct(target_date, logger)
    if data is not None:
        return data
    logger.debug("Direct API unavailable — using browser fallback.")
    return _fetch_via_browser(target_date, logger)


# ── Movie detection ────────────────────────────────────────────────────────────

def find_movie(showtime_data: dict, movie_keyword: str, logger: logging.Logger) -> dict | None:
    """
    Search the showtime payload for the target movie via case-insensitive
    substring match. Returns the film dict or None.
    """
    shows = showtime_data.get("shows", [])
    films = shows[0].get("Film", []) if shows else []

    keyword_lower = movie_keyword.lower()
    for film in films:
        if keyword_lower in film.get("Title", "").lower():
            logger.info("FOUND: %r", film["Title"])
            return film

    logger.info(
        "Not listed yet — %d film(s) on schedule, keyword %r not among them.",
        len(films),
        movie_keyword,
    )
    return None


def _film_titles(showtime_data: dict) -> list[str]:
    """Extract all film titles from the showtime payload (used by heartbeat)."""
    shows = showtime_data.get("shows", []) if showtime_data else []
    films = shows[0].get("Film", []) if shows else []
    return [f.get("Title", "") for f in films if f.get("Title")]


# ── Email helpers ──────────────────────────────────────────────────────────────

def _format_time(iso_str: str) -> str:
    """'2026-03-27T14:30:00' → '2:30 PM'"""
    return datetime.fromisoformat(iso_str).strftime("%-I:%M %p")


def _experience_label(attributes: list[str]) -> str:
    relevant = [a for a in attributes if a in DISPLAY_ATTRS]
    return " · ".join(relevant) if relevant else "Standard"


def _parse_language(title: str) -> str:
    """
    Extract a trailing parenthetical language tag from the film title.
    'Dhurandhar The Revenge (Hindi)' → 'Hindi'
    Returns an empty string if no trailing parenthetical is present.
    """
    m = re.search(r'\(([^)]+)\)\s*$', title)
    return m.group(1) if m else ""


# ── Alert email ────────────────────────────────────────────────────────────────

def build_email(film: dict, target_date: date) -> tuple[str, str, str]:
    """
    Build the showtime alert email.
    Returns (subject, plain_text, html_body).
    Language is extracted from the title's trailing parenthetical — not hardcoded.
    """
    title    = film["Title"]
    language = _parse_language(title)
    performances: list[dict] = film.get("Performances", [])

    by_screen: dict[int, list[dict]] = {}
    for perf in performances:
        by_screen.setdefault(perf["Auditorium"], []).append(perf)
    for perfs in by_screen.values():
        perfs.sort(key=lambda p: p["CalendarShowTime"])

    date_label  = target_date.strftime("%b %d")
    date_long   = target_date.strftime("%A, %B %d, %Y")
    booking_url = f"{THEATER_URL}?date={target_date.strftime('%m-%d-%Y')}"
    subject     = f"Showtimes Live – {title} | {date_label}"

    # ── Plain text ─────────────────────────────────────────────────────────────
    plain_lines = [
        f"'{title}' is now showing at {THEATER_NAME} on {date_long}.",
        "",
        f"Theater : {THEATER_NAME}",
        f"Address : 9700 Medlock Bridge Rd, Suite 170, Duluth, GA 30097",
        f"Date    : {date_long}",
        "",
        "Showtimes",
        "---------",
    ]
    for screen_num, perfs in sorted(by_screen.items()):
        experience = _experience_label(perfs[0].get("PerformanceAttributes", []))
        times = ", ".join(_format_time(p["CalendarShowTime"]) for p in perfs)
        plain_lines.append(f"  Screen {screen_num} ({experience}): {times}")
    plain_lines += ["", f"Book tickets: {booking_url}", "", "— Movie Monitor (automated)"]
    plain_text = "\n".join(plain_lines)

    # ── HTML ───────────────────────────────────────────────────────────────────
    table_rows = []
    for screen_num, perfs in sorted(by_screen.items()):
        experience = _experience_label(perfs[0].get("PerformanceAttributes", []))
        time_badges = "".join(
            f'<span style="display:inline-block;background:#1a1a2e;color:#fff;'
            f'padding:5px 12px;border-radius:4px;margin:3px 4px 3px 0;'
            f'font-size:13px;font-weight:600;">{_format_time(p["CalendarShowTime"])}</span>'
            for p in perfs
        )
        table_rows.append(
            f"<tr>"
            f'<td style="padding:10px 8px;font-weight:700;color:#1a1a2e;white-space:nowrap;">'
            f"Screen {screen_num}</td>"
            f'<td style="padding:10px 8px;color:#555;font-size:13px;">{experience}</td>'
            f'<td style="padding:10px 8px;">{time_badges}</td>'
            f"</tr>"
        )

    lang_line = (
        f'<p style="margin:0 0 20px;color:#666;font-size:13px;">{language}</p>'
        if language else ""
    )

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:30px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:8px;overflow:hidden;
                    box-shadow:0 2px 8px rgba(0,0,0,.12);max-width:600px;">

        <!-- Header -->
        <tr>
          <td style="background:#1a1a2e;padding:24px 28px;">
            <p style="margin:0;color:#e50000;font-size:12px;font-weight:700;
                      letter-spacing:1px;text-transform:uppercase;">Movie Alert</p>
            <h1 style="margin:6px 0 0;color:#fff;font-size:22px;line-height:1.3;">
              Showtimes Now Available!
            </h1>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:28px;">
            <h2 style="margin:0 0 4px;color:#1a1a2e;font-size:20px;">{title}</h2>
            {lang_line}

            <table width="100%" cellpadding="0" cellspacing="0"
                   style="border:1px solid #eee;border-radius:6px;overflow:hidden;
                          margin-bottom:24px;">
              <tr style="background:#f9f9f9;">
                <td style="padding:10px 8px;font-size:12px;font-weight:700;
                           text-transform:uppercase;color:#999;">Theater</td>
                <td style="padding:10px 8px;font-size:14px;color:#333;" colspan="2">
                  {THEATER_NAME}<br>
                  <span style="font-size:12px;color:#888;">
                    9700 Medlock Bridge Rd, Suite 170, Duluth, GA 30097
                  </span>
                </td>
              </tr>
              <tr>
                <td style="padding:10px 8px;font-size:12px;font-weight:700;
                           text-transform:uppercase;color:#999;">Date</td>
                <td style="padding:10px 8px;font-size:14px;color:#333;"
                    colspan="2">{date_long}</td>
              </tr>
            </table>

            <h3 style="margin:0 0 12px;color:#1a1a2e;font-size:15px;">Showtimes</h3>
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <thead>
                <tr style="border-bottom:2px solid #eee;">
                  <th style="text-align:left;padding:8px;font-size:12px;
                             color:#999;font-weight:700;text-transform:uppercase;">Screen</th>
                  <th style="text-align:left;padding:8px;font-size:12px;
                             color:#999;font-weight:700;text-transform:uppercase;">Format</th>
                  <th style="text-align:left;padding:8px;font-size:12px;
                             color:#999;font-weight:700;text-transform:uppercase;">Times</th>
                </tr>
              </thead>
              <tbody>{''.join(table_rows)}</tbody>
            </table>

            <div style="text-align:center;margin-top:28px;">
              <a href="{booking_url}"
                 style="display:inline-block;background:#e50000;color:#fff;
                        padding:13px 30px;border-radius:5px;text-decoration:none;
                        font-weight:700;font-size:15px;letter-spacing:.3px;">
                Book Tickets Now
              </a>
            </div>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f9f9f9;padding:14px 28px;border-top:1px solid #eee;">
            <p style="margin:0;font-size:11px;color:#aaa;text-align:center;">
              Sent by Movie Monitor &mdash; automated showtime alert &bull;
              <a href="{booking_url}" style="color:#aaa;">View on Regal</a>
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    return subject, plain_text, html_body


def send_email(
    subject: str,
    plain_text: str,
    html_body: str,
    sender: str,
    password: str,
    recipient: str,
    logger: logging.Logger,
) -> bool:
    """Send via Gmail SMTP over SSL. Returns True on success."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, recipient, msg.as_string())
        logger.info("Email delivered to %s", recipient)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "Gmail authentication failed — check SENDER_APP_PASSWORD in .env."
        )
    except smtplib.SMTPException as exc:
        logger.error("SMTP error: %s", exc)
    except OSError as exc:
        logger.error("Network error while sending email: %s", exc)
    return False


# ── Heartbeat ──────────────────────────────────────────────────────────────────

def heartbeat_due() -> bool:
    """True if no heartbeat has been sent today."""
    if not HEARTBEAT_FILE.exists():
        return True
    try:
        last = date.fromisoformat(HEARTBEAT_FILE.read_text().strip())
        return last < date.today()
    except (ValueError, OSError):
        return True


def send_heartbeat(
    movie_keyword: str,
    target_date: date,
    titles_on_schedule: list[str],
    fetch_ok: bool,
    sender: str,
    password: str,
    recipient: str,
    logger: logging.Logger,
) -> None:
    """
    Send a plain-text daily check-in email.

    Tells you:
      - The monitor is still running (counters silent failures)
      - Whether the API was reachable
      - What films ARE currently showing on target_date (confirms page structure
        is intact even if your movie isn't listed yet)
    """
    today_str = date.today().strftime("%b %d, %Y")

    if not fetch_ok:
        status = "⚠ WARNING: Could not reach the Regal API — check monitor.log"
        schedule_section = ""
    elif titles_on_schedule:
        status = f"API reachable — {len(titles_on_schedule)} film(s) scheduled, target not listed yet."
        schedule_section = (
            "\nFilms currently on schedule for that date:\n"
            + "\n".join(f"  • {t}" for t in titles_on_schedule)
            + "\n"
        )
    else:
        status = "API reachable — no films scheduled for that date yet."
        schedule_section = ""

    subject = f"Monitor alive – {movie_keyword} | not found yet | {today_str}"
    body = "\n".join([
        f"Daily check-in — {today_str}",
        "─" * 40,
        f"Watching  : {movie_keyword}",
        f"Theater   : {THEATER_NAME}",
        f"Target    : {target_date.strftime('%A, %B %d, %Y')}",
        "",
        f"Status    : {status}",
        schedule_section,
        "— Movie Monitor (automated)",
    ])

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, recipient, msg.as_string())
        HEARTBEAT_FILE.write_text(date.today().isoformat())
        logger.info("Daily heartbeat sent.")
    except Exception as exc:
        logger.warning("Heartbeat email failed (non-critical): %s", exc)


# ── Sentinel ───────────────────────────────────────────────────────────────────

def notification_already_sent() -> bool:
    return SENTINEL_FILE.exists()


def mark_as_notified(movie_keyword: str, target_date: date) -> None:
    SENTINEL_FILE.write_text(
        f"movie={movie_keyword}\n"
        f"date={target_date.isoformat()}\n"
        f"notified_at={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )


def read_sentinel_config() -> tuple[str, date] | None:
    """Return (movie_keyword, target_date) stored in sentinel, or None."""
    if not SENTINEL_FILE.exists():
        return None
    try:
        data = dict(
            line.split("=", 1)
            for line in SENTINEL_FILE.read_text().splitlines()
            if "=" in line
        )
        return data["movie"], date.fromisoformat(data["date"])
    except (KeyError, ValueError, OSError):
        return None


# ── Post-notification actions ──────────────────────────────────────────────────

def notify_desktop(title: str, logger: logging.Logger) -> None:
    """Fire a native macOS notification banner."""
    script = (
        f'display notification "Showtimes are live for {title} — check your email!" '
        f'with title "Movie Monitor" sound name "Glass"'
    )
    try:
        subprocess.run(["osascript", "-e", script], timeout=5, check=True)
        logger.info("Desktop notification sent.")
    except Exception as exc:
        logger.warning("Desktop notification failed (non-critical): %s", exc)


def remove_cron_job(logger: logging.Logger) -> None:
    """Remove the monitor.py cron entry — called after notification is sent."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        filtered = "\n".join(
            line for line in result.stdout.splitlines()
            if "monitor.py" not in line and "showtime monitor" not in line
        ).strip()
        subprocess.run(
            ["crontab", "-"], input=filtered + "\n", text=True, check=True
        )
        logger.info("Cron job removed — no further scheduled checks.")
    except Exception as exc:
        logger.warning(
            "Could not auto-remove cron job (remove manually with --cron-off): %s", exc
        )


# ── Not-found summary email ────────────────────────────────────────────────────

def _send_not_found_email(
    movie_keyword: str,
    target_date: date,
    sender: str,
    password: str,
    recipient: str,
    logger: logging.Logger,
) -> None:
    """
    Sent when TARGET_DATE has passed and the movie was never listed.
    Writes the sentinel so the email isn't resent on subsequent runs.
    """
    subject = f"Monitor complete — {movie_keyword!r} not found by {target_date}"
    body = "\n".join([
        f"The monitor has finished watching for {movie_keyword!r}.",
        "",
        f"Theater   : {THEATER_NAME}",
        f"Target    : {target_date.strftime('%A, %B %d, %Y')}",
        "",
        "The movie was never listed on the Regal schedule for that date.",
        "To watch for a new movie or date, update .env and run:",
        "  rm -f .notified .last_heartbeat",
        "  ./run_now.sh --cron-on",
        "",
        "— Movie Monitor (automated)",
    ])
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, recipient, msg.as_string())
        SENTINEL_FILE.write_text(
            f"movie={movie_keyword}\n"
            f"date={target_date.isoformat()}\n"
            f"not_found=true\n"
            f"notified_at={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        logger.info("'Not found' summary email sent to %s", recipient)
    except Exception as exc:
        logger.warning("Could not send 'not found' email: %s", exc)


# ── Main ───────────────────────────────────────────────────────────────────────

MAX_FETCH_ATTEMPTS = 3
RETRY_DELAY_SECS   = 5


def main() -> None:
    logger = setup_logging()

    with ProcessLock(logger) as lock:
        if not lock.acquired:
            logger.info("Skipped — another instance is already running.")
            sys.exit(0)

        config = load_config(logger)
        if config is None:
            sys.exit(1)
        sender, password, recipient, movie_keyword, target_date = config

        # Guard: date has passed
        today = date.today()
        if today > target_date:
            is_first_run = not SENTINEL_FILE.exists() and not HEARTBEAT_FILE.exists()
            if is_first_run:
                # Scenario A: user configured a past date before the monitor ever ran
                logger.error(
                    "TARGET_DATE %s has already passed and the monitor has never run "
                    "for this config. Update TARGET_DATE in .env to a future date.",
                    target_date,
                )
                sys.exit(1)
            else:
                # Scenario B: monitor ran for days; date expired without finding movie
                logger.info(
                    "Target date %s has passed — %r was never listed. "
                    "Sending 'not found' summary and stopping.",
                    target_date, movie_keyword,
                )
                if not notification_already_sent():
                    _send_not_found_email(
                        movie_keyword, target_date,
                        sender, password, recipient, logger,
                    )
                remove_cron_job(logger)
                sys.exit(0)

        # Guard: already notified
        if notification_already_sent():
            sentinel = read_sentinel_config()
            if sentinel and sentinel != (movie_keyword, target_date):
                logger.warning(
                    ".notified exists for %r / %s but .env now has %r / %s. "
                    "Delete .notified to start fresh.",
                    sentinel[0], sentinel[1], movie_keyword, target_date,
                )
            else:
                logger.info("Already notified — nothing to do.")
            sys.exit(0)

        # Fetch with retries
        showtime_data = None
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            if attempt > 1:
                logger.warning(
                    "Attempt %d/%d — retrying in %ds…",
                    attempt, MAX_FETCH_ATTEMPTS, RETRY_DELAY_SECS,
                )
                time.sleep(RETRY_DELAY_SECS)
            showtime_data = fetch_showtimes(target_date, logger)
            if showtime_data is not None:
                break

        if showtime_data is None:
            logger.error(
                "Fetch failed after %d attempts — will retry next run.", MAX_FETCH_ATTEMPTS
            )
            if heartbeat_due():
                send_heartbeat(
                    movie_keyword, target_date, [], False,
                    sender, password, recipient, logger,
                )
            sys.exit(1)

        # Search for the configured movie
        film = find_movie(showtime_data, movie_keyword, logger)
        if film is None:
            if heartbeat_due():
                send_heartbeat(
                    movie_keyword, target_date, _film_titles(showtime_data), True,
                    sender, password, recipient, logger,
                )
            sys.exit(0)

        # Build and send the alert email
        subject, plain_text, html_body = build_email(film, target_date)
        success = send_email(subject, plain_text, html_body, sender, password, recipient, logger)

        if success:
            mark_as_notified(movie_keyword, target_date)
            notify_desktop(film["Title"], logger)
            remove_cron_job(logger)
            logger.info("Done — email sent, desktop notified, cron removed.")
        else:
            logger.error("Email failed — will retry next run.")
            sys.exit(1)


if __name__ == "__main__":
    main()
