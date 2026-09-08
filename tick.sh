#!/data/data/com.termux/files/usr/bin/sh
# Викликається termux-job-scheduler. Тік тихий: усе цікаве пише в ~/.tt-checkin/log.txt
cd "$(dirname "$0")" || exit 1
exec python3 tt_checkin.py tick >> "$HOME/.tt-checkin/cron.out" 2>&1
