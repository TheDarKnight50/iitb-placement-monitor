#!/bin/bash
# Runner loop: runs scraper.py every CHECK_INTERVAL_SECONDS (default 900 = 15 minutes).
# Works from wherever the repo is cloned - no hardcoded paths.

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR" || exit 1

# Read CHECK_INTERVAL_SECONDS from config.env if present (fallback: 900)
INTERVAL=900
if [ -f "$BASE_DIR/config.env" ]; then
    VAL=$(grep -E '^CHECK_INTERVAL_SECONDS=' "$BASE_DIR/config.env" | tail -1 | cut -d= -f2 | tr -d '[:space:]"')
    [[ "$VAL" =~ ^[0-9]+$ ]] && INTERVAL="$VAL"
fi
PYTHON="${PYTHON:-/usr/bin/python3}"

echo "================================================================="
echo "IIT Bombay Placement & Blog Monitor Daemon"
echo "Running every $INTERVAL seconds in the background..."
echo "Log file: $BASE_DIR/monitor.log"
echo "================================================================="

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting check..." >> "$BASE_DIR/monitor.log"
    "$PYTHON" "$BASE_DIR/scraper.py" >> "$BASE_DIR/monitor.log" 2>&1
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Finished check. Sleeping for $INTERVAL seconds..." >> "$BASE_DIR/monitor.log"
    sleep "$INTERVAL"
done
