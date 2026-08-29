# Regal Showtime Monitor

Monitors the Regal Cinemas website for a specific movie on a specific date and fires an email + desktop notification the moment showtimes go live. Once notified, it self-destructs the cron job — no manual cleanup needed.

Built for the use case where Regal publishes showtimes for future dates unpredictably, and you want to be first to know and book before seats fill up.

---

## How It Works

```
Every 30 minutes (cron)
        │
        ▼
  ┌─────────────────────────────────┐
  │  1. Try direct HTTPS API call   │  ← fast path (~1s)
  │     getShowtimes API            │    blocked by Cloudflare most of the time
  └──────────────┬──────────────────┘
                 │ 403 / blocked
                 ▼
  ┌─────────────────────────────────┐
  │  2. Launch headless Chromium    │  ← passes Cloudflare bot detection
  │     on a PERSISTENT profile     │    .browser-profile/ keeps cf_clearance
  │     (.browser-profile/)         │    so repeat fallbacks skip the challenge
  │     Navigate to theater page    │    triggers getShowtimes API client-side
  │     with ?date=MM-DD-YYYY       │    by including the date in the URL
  └──────────────┬──────────────────┘
                 │
                 ▼
  ┌─────────────────────────────────┐
  │  3. Intercept getShowtimes JSON │  ← event-driven, no fixed sleep
  │     Search for MOVIE_TITLE      │
  └──────────────┬──────────────────┘
                 │
        ┌────────┴────────┐
        │                 │
   Not found           Found
        │                 │
        ▼                 ▼
  Daily heartbeat   Email alert sent
  email (1/day)     Desktop notification
  "still watching"  Cron job removed
                    Sentinel written (.notified)
```

**Key behaviours:**
- Only one instance runs at a time — a file lock prevents concurrent cron + manual runs
- A sentinel file (`.notified`) prevents duplicate emails even if cron is accidentally re-added
- A daily heartbeat email confirms the monitor is alive and shows what IS currently scheduled
- After a successful notification the cron job removes itself automatically
- The browser fallback reuses a persistent Chromium profile, so a Cloudflare clearance earned on one run is not thrown away on the next. **This speeds up the fallback only — it does not change how often the fast path succeeds.** See [Architecture Notes](#architecture-notes).
- Fetching is retried up to 3 times per run with a 5-second delay between attempts

---

## File Structure

```
regal-showtime-monitor/
├── monitor.py        Main monitoring script (run via run_now.sh from cron)
├── lock.py           Process lock — prevents concurrent runs
├── run_now.sh        Control panel: resolves Python, runs on demand, manages cron
├── tests/            pytest suite (fully mocked — no network, no browser)
├── requirements.txt  Python dependencies
├── .env.example      Template for .env — placeholders only, safe to commit
├── .env              Your credentials and config (gitignored, never committed)
├── .github/          CI workflow and Dependabot config
└── .gitignore
```

Runtime files created automatically (gitignored):
```
monitor.log           Audit log
.notified             Sentinel — written after email is sent
.last_heartbeat       Tracks when last daily heartbeat was sent
.monitor.lock         Process lock file
.browser-profile/     Chromium user-data dir for the fallback (cookies/cache)
```

`.browser-profile/` holds only regmovies.com browsing state. No login happens
anywhere in this flow, so no credentials land there — but it is gitignored
regardless, and a test asserts that it stays that way.

---

## Prerequisites

- macOS (uses `osascript` for desktop notifications and `crontab` for scheduling)
- **Python 3.12+** — via [pyenv](https://github.com/pyenv/pyenv) or a system install. CI runs 3.12.
- A Gmail account with 2-Step Verification enabled

`run_now.sh` resolves the Python interpreter **at run time** — nothing is
hard-coded, so it works on a fresh clone and survives a Python upgrade. It picks
the first of:

1. `$MOVIE_MONITOR_PYTHON`, if you set it — an explicit override that wins over
   everything else
2. `$VIRTUAL_ENV/bin/python` — the venv you have activated
3. `./venv/bin/python` or `./.venv/bin/python` next to the script — the venv the
   setup below creates
4. `python3` from `$PATH`

If none of those turns up a usable interpreter it exits non-zero and prints
what it looked at and how to fix it, rather than failing silently. Check what
it would use:

```bash
./run_now.sh --print-python
```

> **Under cron**, `$PATH` is minimal (usually `/usr/bin:/bin`) and your shell
> profile is not sourced, so a pyenv shim will not be found. Options 3 and 4 are
> what cron relies on — the repo-local `./venv` (absolute, PATH-independent) is
> the most reliable, or set `MOVIE_MONITOR_PYTHON` in the crontab itself.

---

## One-Time Setup

### 1. Clone the repo

```bash
git clone https://github.com/SuryaKiran434/regal-showtime-monitor.git
cd regal-showtime-monitor
```

### 2. Install Python dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Chromium binary (~400MB) for the browser fallback.
# Needed for real runs. NOT needed to run the tests — see "Running the tests".
playwright install chromium
```

### 3. Create your `.env` file

Copy the template and fill in your own values:

```bash
cp .env.example .env
```

[`.env.example`](.env.example) carries placeholders only and is safe to commit.
`.env` is gitignored and must never be:

```
SENDER_EMAIL=you@gmail.com
SENDER_APP_PASSWORD=xxxx xxxx xxxx xxxx
RECIPIENT_EMAIL=you@gmail.com
MOVIE_TITLE=dhurandhar
TARGET_DATE=03-27-2026
TEST_RECIPIENT_EMAIL=
```

All five of the first variables are required; `monitor.py` refuses to start and
names the missing ones if any is blank.

| Variable | Description |
|---|---|
| `SENDER_EMAIL` | Gmail address the alert is sent **from** |
| `SENDER_APP_PASSWORD` | 16-character Gmail App Password (see below) |
| `RECIPIENT_EMAIL` | Address the alert is delivered **to** (can be the same as sender) |
| `MOVIE_TITLE` | Keyword to match — case-insensitive substring of the film title |
| `TARGET_DATE` | Date you want to see the movie — format `MM-DD-YYYY` |
| `TEST_RECIPIENT_EMAIL` | Optional override recipient for manual test runs |

### 4. Generate a Gmail App Password

1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** if not already on (required)
3. Search for **App passwords** and open it
4. Create a new app password — name it anything (e.g. "Movie Monitor")
5. Copy the 16-character code (spaces included) and paste it as `SENDER_APP_PASSWORD`

> Your normal Gmail login password will **not** work here. Only App Passwords work with SMTP.

### 5. About `MOVIE_TITLE` matching

The keyword is matched as a **case-insensitive substring** against the exact title Regal returns from their API.

```
API title: "Dhurandhar The Revenge (Hindi)"

MOVIE_TITLE=dhurandhar          ✅  matches
MOVIE_TITLE=The Revenge         ✅  matches
MOVIE_TITLE=HINDI               ✅  matches (it's inside the title)
MOVIE_TITLE=dhurandhar hindi    ❌  not a contiguous substring
MOVIE_TITLE=reveng              ❌  incomplete word, no match
```

**Recommendation:** Use the most distinctive single word from the title.

---

## Usage

All operations go through `run_now.sh`. Run it with no arguments for an interactive menu, or pass a flag:

```bash
./run_now.sh               # Interactive menu
./run_now.sh --run         # Trigger a check right now
./run_now.sh --status      # Show last 20 lines of monitor.log + cron status
./run_now.sh --cron-on     # Install cron job (runs every 30 minutes)
./run_now.sh --cron-off    # Remove cron job
./run_now.sh --cron-status # Check whether cron is active
```

Diagnostics:

```bash
./run_now.sh --print-python      # Show which interpreter would be used
./run_now.sh --print-cron-entry  # Show the crontab line --cron-on would install
./run_now.sh --cron-run          # What the cron entry itself invokes
```

`--cron-on` rewrites your user crontab: it strips any existing line for this
job (so re-running never duplicates it), then appends

```
*/30 * * * * "/path/to/run_now.sh" --cron-run >> "/path/to/monitor.log" 2>&1 # movie-monitor
```

The entry calls **`run_now.sh`, not an interpreter path** — so the interpreter is
resolved every time the job fires and the entry does not go stale when Python
moves. stderr goes to the log rather than `/dev/null`, so a resolution failure
shows up in `--status` instead of vanishing.

`--cron-off` removes it again, matching either the `# movie-monitor` marker or
the older `monitor.py` form, so an entry installed by a previous version of this
script is cleaned up (and `--cron-on` migrates it in place). Unrelated crontab
lines are left alone. No `sudo`, no LaunchAgent — a plain user crontab entry you
can inspect with `crontab -l`.

### Typical workflow

```bash
# 1. Fill in .env with your credentials and target movie/date

# 2. Run once manually to verify everything works end-to-end
./run_now.sh --run

# 3. Start the automated monitor
./run_now.sh --cron-on

# 4. Check status at any time
./run_now.sh --status
```

When the movie is found:
1. Email alert delivered to `RECIPIENT_EMAIL`
2. macOS desktop notification pops up immediately
3. Cron job removed automatically — no manual cleanup needed

---

## Running the tests

```bash
pip install pytest
python -m pytest
```

**22 tests**, all of them fully mocked. The parsing tests run against an in-file
`getShowtimes` fixture — the exact payload shape Regal returns — so nothing
touches the network, and the browser path is stubbed rather than driven. The
`run_now.sh` tests shell out to the script with controlled environments and
stub interpreters; they never read or write the real crontab.

That means **`playwright install chromium` is not required to run the tests.**
CI installs the `playwright` package (it is imported at module scope in
`monitor.py`) but deliberately skips the ~400MB browser download.

Coverage:

| Area | Tests |
|---|---|
| `find_movie` | case-insensitive substring match, absent title, empty payload |
| Parsing | `_film_titles`, `_parse_language`, `_format_time`, `_experience_label` |
| Email | `build_email` renders grouped, sorted showtimes |
| Fetch strategy | direct path preferred; browser fallback used when direct returns `None` |
| Browser profile | profile dir location and name, persistent context is used, graceful fallback when the profile dir is unusable, and that the dir is gitignored |
| Interpreter resolution | all four branches in priority order (`$MOVIE_MONITOR_PYTHON`, activated venv, repo-local `venv`/`.venv`, `python3` on PATH), loud non-zero failure for an unusable override and for no interpreter at all, and that the cron entry embeds no interpreter path |

CI runs on every push to `main` and every pull request. The **`Tests (Python)`**
check is a required status check on `main`.

---

## Monitoring Lifecycle

```
You run:  ./run_now.sh --cron-on
              └── cron fires every 30 min
              └── each run logs one line: "Not listed yet — N films on schedule"

Regal publishes showtimes for your target date
              └── next cron run detects it
              └── email + desktop notification sent
              └── cron removes itself
              └── .notified file written (stores movie + date, blocks duplicates)
              └── done — no further action needed

If TARGET_DATE passes without the movie being found:
              └── next cron run after the date detects expiry
              └── "not found" summary email sent
              └── cron removes itself
              └── .notified written (prevents resend)
```

### Daily heartbeat email

Once per day while the movie hasn't been found yet, the monitor sends a brief plain-text email:

```
Daily check-in — Mar 20, 2026
────────────────────────────────────────
Watching  : dhurandhar
Theater   : Regal Medlock Crossing
Target    : Friday, March 27, 2026

Status    : API reachable — 12 film(s) scheduled, target not listed yet.

Films currently on schedule for that date:
  • Some Movie Title
  • Another Movie
  ...
```

This is your safety net. If heartbeats stop arriving, something broke and you should check the log.

---

## Changing the Movie or Date

Edit `.env` and update `MOVIE_TITLE` and/or `TARGET_DATE`. Then reset state and restart:

```bash
rm -f .notified .last_heartbeat
./run_now.sh --cron-on
```

No code changes needed.

---

## Log Format

Each run adds one line to `monitor.log`. Example over several days:

```
2026-03-20 00:30  [INFO]     Not listed yet — 12 film(s) on schedule, keyword 'dhurandhar' not among them.
2026-03-20 01:00  [INFO]     Not listed yet — 12 film(s) on schedule, keyword 'dhurandhar' not among them.
...
2026-03-20 12:00  [INFO]     Daily heartbeat sent.
...
2026-03-26 14:00  [INFO]     FOUND: 'Dhurandhar The Revenge (Hindi)'
2026-03-26 14:00  [INFO]     Email delivered to you@gmail.com
2026-03-26 14:00  [INFO]     Desktop notification sent.
2026-03-26 14:00  [INFO]     Cron job removed — no further scheduled checks.
2026-03-26 14:00  [INFO]     Done — email sent, desktop notified, cron removed.
```

---

## Troubleshooting

**".env file not found"**
The `.env` file must exist in the project root. Create it with the five required variables listed in the setup section.

**"Missing required .env variable(s)"**
All five variables in `.env` must have values. Check for typos or blank lines.

**"Gmail authentication failed"**
- You must use an App Password, not your regular Gmail password
- 2-Step Verification must be enabled first
- Generate a new App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)

**"getShowtimes API did not fire"**
Regal hasn't published a schedule for your target date yet. Normal — the monitor will keep checking every 30 minutes.

**"Fetch failed after 3 attempts"**
A transient network or Cloudflare issue. The cron will retry automatically in 30 minutes. If it persists, run `./run_now.sh --run` to see the live error.

**Heartbeat emails stopped arriving**
```bash
./run_now.sh --cron-status   # is cron still installed?
./run_now.sh --run           # run manually and watch output
./run_now.sh --status        # check the log
```

**Email not received after `.notified` exists**
The notification already fired. To resend manually:
```bash
rm .notified && ./run_now.sh --run
```

**"TARGET_DATE has already passed and the monitor has never run"**
You set a past date in `.env` before the monitor ever ran. Update `TARGET_DATE` to a future date.

**"not found" summary email received**
The target date passed without the movie being listed. To watch for a new movie or date:
```bash
# Update MOVIE_TITLE and TARGET_DATE in .env, then:
rm -f .notified .last_heartbeat
./run_now.sh --cron-on
```

**"Persistent browser profile ... unusable — using a throwaway profile"**
The monitor still ran; only the Cloudflare-clearance reuse was lost for that
run. Usually a stale Chromium lock left behind by a killed process. Delete the
profile and let it rebuild:
```bash
rm -rf .browser-profile && ./run_now.sh --run
```

**".notified exists for different movie/date" warning in log**
`.env` was changed after a previous notification. Delete `.notified` to start fresh:
```bash
rm .notified && ./run_now.sh --cron-on
```

---

## Architecture Notes

**Two-path fetch strategy:** `fetch_showtimes()` tries `_fetch_direct()` first — a plain `httpx` HTTP/2 GET straight at `getShowtimes` with browser-ish headers, ~1s when it works. If it returns anything other than a 200, or raises, the function falls through to `_fetch_via_browser()`. There is no shared state between the two; the fast path is simply attempted first every run.

**Cloudflare bypass:** Direct HTTPS calls to the Regal API frequently return 403. Playwright launches a real Chromium browser which passes Cloudflare's bot detection. The browser navigates to `THEATER_URL?date=MM-DD-YYYY`, the Regal React app reads the date parameter and calls the `getShowtimes` API client-side, and Playwright intercepts that response via `page.expect_response()`.

**Persistent browser profile:** `_open_browser_context()` uses `launch_persistent_context()` against a gitignored `.browser-profile/` directory rather than a fresh throwaway context. Chromium keeps its cookies there, including Cloudflare's `cf_clearance`, so a clearance earned on one fallback is still valid on the next one and the challenge is skipped instead of re-solved. Only one monitor runs at a time (see the process lock), so the profile is never contended. If the directory turns out to be unusable — a stale Chromium lock, wrong permissions — the code logs a warning and falls back to a throwaway `launch()` + `new_context()` for that run, so an unusable profile degrades performance rather than breaking the monitor.

> **What the profile does *not* do:** it does not improve the fast path's hit rate. `_fetch_direct()` builds its own `httpx.Client`, which has a **separate cookie jar** and never sees the browser profile. A `cf_clearance` cookie sitting in `.browser-profile/` is invisible to it. The persistent profile makes the **browser fallback** cheaper; the direct HTTPS call succeeds or fails exactly as often as it did before.

**Event-driven response capture:** `page.expect_response()` wraps the `page.goto()` call — the script receives the data the instant the API responds, rather than sleeping for a fixed duration.

**Process safety:** `fcntl.flock(LOCK_EX | LOCK_NB)` on a shared lock file prevents concurrent runs from cron and manual triggers interfering with each other.

**Duplicate prevention:** A `.notified` sentinel file is written after successful delivery and checked at startup. If present, the script exits in under a second — no browser launched. The sentinel stores `movie=`, `date=`, and `notified_at=` so the script can detect when `.env` has been changed to a new movie/date and warn you rather than silently exiting.

**Past-date handling:** If `TARGET_DATE` has already passed when the script runs, the outcome depends on history. If neither `.notified` nor `.last_heartbeat` exists (monitor never ran), it exits with an error so you know to fix the config. If either file exists (monitor ran but movie was never found), it sends a one-time "not found" summary email and removes the cron job.
