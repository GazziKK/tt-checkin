#!/data/data/com.termux/files/usr/bin/sh
# Копіюється в ~/.termux/boot/ — Termux:Boot виконує все з цього каталогу після
# перезавантаження телефона. Піднімає демона точного часу; якщо демон уже
# працює, він сам тихо вийде з кодом 0.
#
# Wake-lock бере саме цей скрипт, а не демон: демон має лишатись запускним і на
# звичайному Linux, де termux-wake-lock не існує.
termux-wake-lock

DIR="$HOME/.tt-checkin"
mkdir -p "$DIR" || exit 1
chmod 700 "$DIR"

OUT="$DIR/daemon.out"
# Рости без межі файл не має: 64 КБ — це з запасом на останні трейсбеки.
if [ -f "$OUT" ] && [ "$(wc -c < "$OUT")" -gt 65536 ]; then
    : > "$OUT"
fi
: >> "$OUT"
chmod 600 "$OUT"

cd "$HOME/tt-checkin" || exit 1
exec python3 tt_checkin.py daemon >> "$OUT" 2>&1
