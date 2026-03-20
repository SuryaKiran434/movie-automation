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
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/Users/suryakiran/.pyenv/versions/3.13.1/bin/python3"
MONITOR="$SCRIPT_DIR/monitor.py"
LOG="$SCRIPT_DIR/monitor.log"

# Cron entry that will be installed / removed
CRON_ENTRY="*/30 * * * * $PYTHON $MONITOR >> $LOG 2>&1"
CRON_MARKER="monitor.py"   # used to find our entry among all cron jobs

# ── Helpers ───────────────────────────────────────────────────────────────────

run_monitor() {
    echo "[run_now] Starting monitor.py at $(date '+%Y-%m-%d %H:%M:%S')"
    "$PYTHON" "$MONITOR"
    EXIT_CODE=$?
    echo "[run_now] Finished with exit code $EXIT_CODE"
    echo ""
    echo "=== Last 10 lines of monitor.log ==="
    tail -10 "$LOG" 2>/dev/null || echo "(log file not found yet)"
}

show_status() {
    echo "=== Last 20 lines of monitor.log ==="
    tail -20 "$LOG" 2>/dev/null || echo "(log file not found yet)"
    echo ""
    cron_status_inline
}

cron_status_inline() {
    if crontab -l 2>/dev/null | grep -qF "$CRON_MARKER"; then
        echo "Cron job : ACTIVE  (runs every 30 minutes)"
    else
        echo "Cron job : NOT installed"
    fi
}

cron_on() {
    # Remove any existing entry for this script first to avoid duplicates
    ( crontab -l 2>/dev/null | grep -vF "$CRON_MARKER"; echo "$CRON_ENTRY" ) | crontab -
    echo "Cron job installed — monitor.py will run every 30 minutes."
    crontab -l | grep "$CRON_MARKER"
}

cron_off() {
    if ! crontab -l 2>/dev/null | grep -qF "$CRON_MARKER"; then
        echo "No cron job found for monitor.py — nothing to remove."
        return
    fi
    crontab -l 2>/dev/null | grep -vF "$CRON_MARKER" | crontab -
    echo "Cron job removed."
}

cron_show_status() {
    if crontab -l 2>/dev/null | grep -qF "$CRON_MARKER"; then
        echo "Cron job is ACTIVE:"
        crontab -l | grep "$CRON_MARKER"
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
    --run)          run_monitor ;;
    --status)       show_status ;;
    --cron-on)      cron_on ;;
    --cron-off)     cron_off ;;
    --cron-status)  cron_show_status ;;
    "")             show_menu ;;
    *)
        echo "Unknown option: $1"
        echo "Usage: $0 [--run | --status | --cron-on | --cron-off | --cron-status]"
        exit 1
        ;;
esac
