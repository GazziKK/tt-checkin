#!/data/data/com.termux/files/usr/bin/sh
# Викликається termux-job-scheduler. Тік тихий: усе цікаве пише в ~/.tt-checkin/log.txt
# У cron.out падає те, що в лог не потрапило: stdout тіку і трейсбеки інтерпретатора.
cd "$(dirname "$0")" || exit 1

# Каталог має існувати ДО перенаправлення: розписання, поставлене до першого
# `login`, інакше валить sh, і тік не лишає слідів у жодному файлі з README.
DIR="$HOME/.tt-checkin"
mkdir -p "$DIR" || exit 1
chmod 700 "$DIR"

OUT="$DIR/cron.out"
# Рости без межі файл не має: 64 КБ — це з запасом на останні трейсбеки.
if [ -f "$OUT" ] && [ "$(wc -c < "$OUT")" -gt 65536 ]; then
    : > "$OUT"
fi
: >> "$OUT"
chmod 600 "$OUT"

exec python3 tt_checkin.py tick >> "$OUT" 2>&1
