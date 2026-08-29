#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run_now.sh — Control panel for the Movie Monitor
# ─────────────────────────────────────────────────────────────────────────────
#
# Usage (pass a flag directly):
#   ./run_now.sh              # interactive menu
#   ./run_now.sh --run        # check showtimes right now
#   ./run_now.sh --status     # tail the last 20 lines of monitor.log
#   ./run_now.sh --cron-on    # install cron job (every 30 min)
#   ./run_now.sh --cron-off   # remove cron job
#   ./run_now.sh --cron-status # show whether cron job is installed
#
# Diagnostics:
#   ./run_now.sh --print-python      # print the interpreter that would be used
#   ./run_now.sh --print-cron-entry  # print the crontab line --cron-on installs
#   ./run_now.sh --cron-run          # what the cron entry itself invokes
# ─────────────────────────────────────────────────────────────────────────────

# Resolved with shell builtins only (no dirname): this script runs from cron,
# where PATH is minimal, and the interpreter-resolution error path below has to
# stay readable even when nothing external is on PATH.
case "$0" in
    */*) _script_dir="${0%/*}" ;;
    *)   _script_dir="." ;;
esac
SCRIPT_DIR="$(cd "$_script_dir" && pwd)"
unset _script_dir
MONITOR="$SCRIPT_DIR/monitor.py"
LOG="$SCRIPT_DIR/monitor.log"

# Cron entry that will be installed / removed.
#
# It invokes THIS script rather than an interpreter path. The interpreter is
# resolved every time the job fires, so the entry keeps working across Python
# upgrades, pyenv version removals, and machines other than the one it was
# installed on. Previously the entry embedded an absolute path
# (/Users/<name>/.pyenv/versions/3.13.1/bin/python3) that went stale silently:
# cron would run, the binary would be missing, and nothing would ever notify.
#
# stderr goes to the log (not /dev/null) so an interpreter-resolution failure
# is visible in `--status` instead of vanishing.
CRON_MARKER="# movie-monitor"     # identifies our entry among all cron jobs
LEGACY_CRON_MARKER="monitor.py"   # entries written by older versions of this script
CRON_ENTRY="*/30 * * * * \"$SCRIPT_DIR/run_now.sh\" --cron-run >> \"$LOG\" 2>&1 $CRON_MARKER"

# ── Interpreter resolution ────────────────────────────────────────────────────

# Resolve the Python interpreter at run time. Prints the path on stdout and
# returns 0, or explains the problem on stderr and returns 1.
#
# Order of preference:
#   1. $MOVIE_MONITOR_PYTHON     — explicit override, wins over everything
#   2. $VIRTUAL_ENV/bin/python   — the venv the caller has activated
#   3. ./venv or ./.venv         — a repo-local venv (README step 2 creates ./venv)
#   4. python3 from PATH
resolve_python() {
    local candidate

    if [ -n "${MOVIE_MONITOR_PYTHON:-}" ]; then
        if [ -x "$MOVIE_MONITOR_PYTHON" ]; then
            printf '%s\n' "$MOVIE_MONITOR_PYTHON"
            return 0
        fi
        echo "run_now.sh: MOVIE_MONITOR_PYTHON is set to '$MOVIE_MONITOR_PYTHON', which is not an executable file." >&2
        return 1
    fi

    if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
        printf '%s\n' "$VIRTUAL_ENV/bin/python"
        return 0
    fi

    for candidate in "$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/.venv/bin/python"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    candidate="$(command -v python3 2>/dev/null)"
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
    fi

    # printf, not a `cat` heredoc: this must still print when PATH is broken,
    # which is one of the ways we get here in the first place.
    printf '%s\n' \
        "run_now.sh: no usable Python interpreter found." \
        "" \
        "Looked, in order, at:" \
        "  1. \$MOVIE_MONITOR_PYTHON    (unset)" \
        "  2. \$VIRTUAL_ENV/bin/python  (VIRTUAL_ENV=${VIRTUAL_ENV:-unset})" \
        "  3. $SCRIPT_DIR/venv/bin/python" \
        "     $SCRIPT_DIR/.venv/bin/python" \
        "  4. python3 on \$PATH         (PATH=${PATH:-<empty>})" \
        "" \
        "Fix it with any one of:" \
        "  - create the repo-local venv:  python3 -m venv $SCRIPT_DIR/venv" \
        "  - activate an existing venv:   source /path/to/venv/bin/activate" \
        "  - point straight at one:       export MOVIE_MONITOR_PYTHON=/path/to/python3" \
        "" \
        "cron runs with a minimal PATH (usually /usr/bin:/bin). If this only fails" \
        "under cron, use the repo-local venv or MOVIE_MONITOR_PYTHON — both are" \
        "absolute and do not depend on PATH." >&2
    return 1
}

# Resolve into $PYTHON or abort. Only the paths that actually need an
# interpreter call this, so --status / --cron-off keep working without one.
require_python() {
    PYTHON="$(resolve_python)" || exit 1
}

# ── Helpers ───────────────────────────────────────────────────────────────────

run_monitor() {
    require_python
    echo "[run_now] Using interpreter: $PYTHON"
    echo "[run_now] Starting monitor.py at $(date '+%Y-%m-%d %H:%M:%S')"
    "$PYTHON" "$MONITOR"
    EXIT_CODE=$?
    echo "[run_now] Finished with exit code $EXIT_CODE"
    echo ""
    echo "=== Last 10 lines of monitor.log ==="
    tail -10 "$LOG" 2>/dev/null || echo "(log file not found yet)"
}

# What the cron entry invokes: resolve, then hand the process straight to
# monitor.py. No banner, no log tail — cron appends this output to the log.
cron_run() {
    require_python
    exec "$PYTHON" "$MONITOR"
}

show_status() {
    echo "=== Last 20 lines of monitor.log ==="
    tail -20 "$LOG" 2>/dev/null || echo "(log file not found yet)"
    echo ""
    cron_status_inline
}

# Print our cron lines (new-style and legacy), if any.
current_cron_entries() {
    crontab -l 2>/dev/null | grep -F -e "$CRON_MARKER" -e "$LEGACY_CRON_MARKER"
}

cron_status_inline() {
    if [ -n "$(current_cron_entries)" ]; then
        echo "Cron job : ACTIVE  (runs every 30 minutes)"
    else
        echo "Cron job : NOT installed"
    fi
}

cron_on() {
    # Warn early rather than letting cron fail silently in 30 minutes' time.
    # Still installs: the entry resolves the interpreter when it fires, so a
    # venv created after this point will be picked up.
    if ! resolve_python >/dev/null; then
        echo ""
        echo "run_now.sh: installing the cron job anyway — it resolves the interpreter"
        echo "            at run time — but it will fail until the above is fixed." >&2
    fi

    # Remove any existing entry for this script first to avoid duplicates,
    # including entries written by older versions that named monitor.py directly.
    ( crontab -l 2>/dev/null | grep -vF -e "$CRON_MARKER" -e "$LEGACY_CRON_MARKER"; echo "$CRON_ENTRY" ) | crontab -
    echo "Cron job installed — monitor.py will run every 30 minutes."
    current_cron_entries
}

cron_off() {
    if [ -z "$(current_cron_entries)" ]; then
        echo "No cron job found for monitor.py — nothing to remove."
        return
    fi
    crontab -l 2>/dev/null | grep -vF -e "$CRON_MARKER" -e "$LEGACY_CRON_MARKER" | crontab -
    echo "Cron job removed."
}

cron_show_status() {
    if [ -n "$(current_cron_entries)" ]; then
        echo "Cron job is ACTIVE:"
        current_cron_entries
    else
        echo "Cron job is NOT installed."
    fi
}

show_menu() {
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║         Movie Monitor — Control Panel    ║"
    echo "╠══════════════════════════════════════════╣"
    echo "║  1) Run monitor now (on-demand check)    ║"
    echo "║  2) Show log (last 20 lines)             ║"
    echo "║  3) Install cron job (every 30 min)      ║"
    echo "║  4) Remove cron job                      ║"
    echo "║  5) Show cron job status                 ║"
    echo "║  q) Quit                                 ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""
    printf "Choose an option: "
    read -r choice
    case "$choice" in
        1) run_monitor ;;
        2) show_status ;;
        3) cron_on ;;
        4) cron_off ;;
        5) cron_show_status ;;
        q|Q) echo "Bye." ; exit 0 ;;
        *) echo "Invalid option: $choice" ; show_menu ;;
    esac
}

# ── Entry point ───────────────────────────────────────────────────────────────

case "$1" in
    --run)              run_monitor ;;
    --cron-run)         cron_run ;;
    --status)           show_status ;;
    --cron-on)          cron_on ;;
    --cron-off)         cron_off ;;
    --cron-status)      cron_show_status ;;
    --print-python)     resolve_python ;;
    --print-cron-entry) echo "$CRON_ENTRY" ;;
    "")                 show_menu ;;
    *)
        echo "Unknown option: $1"
        echo "Usage: $0 [--run | --status | --cron-on | --cron-off | --cron-status]"
        echo "       $0 [--print-python | --print-cron-entry | --cron-run]"
        exit 1
        ;;
esac
