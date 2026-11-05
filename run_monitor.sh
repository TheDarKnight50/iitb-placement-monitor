#!/bin/bash
# Runner loop script that checks every 15 minutes (900 seconds)

BASE_DIR="/home/aryan/Desktop/Imp/Placements/scripts"
cd "$BASE_DIR" || exit 1

echo "================================================================="
echo "IIT Bombay Placement & Blog Monitor Daemon"
echo "Running every 15 minutes in the background..."
echo "Log file: $BASE_DIR/monitor.log"
echo "================================================================="

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting check..." >> "$BASE_DIR/monitor.log"
    /usr/bin/python3 "$BASE_DIR/scraper.py" >> "$BASE_DIR/monitor.log" 2>&1
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Finished check. Sleeping for 15 minutes..." >> "$BASE_DIR/monitor.log"
    sleep 900
done
