#!/data/data/com.termux/files/usr/bin/bash
# Встановлення і оновлення tt-checkin однією командою в Termux:
#
#   curl -fsSL https://raw.githubusercontent.com/GazziKK/tt-checkin/main/install.sh | bash
#
# Повторний запуск — це і є оновлення: файли перезаписуються, демон
# перезапускається, токен/креди/стан у ~/.tt-checkin не чіпаються.
set -euo pipefail

BASE="${TT_BASE:-https://raw.githubusercontent.com/GazziKK/tt-checkin/main}"
APP="$HOME/tt-checkin"
FILES="tt_checkin.py tick.sh boot/tt-checkin.sh"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mПОМИЛКА: %s\033[0m\n' "$*" >&2; exit 1; }

[ -n "${PREFIX:-}" ] && [ -d "$PREFIX" ] || die "запускай це в Termux на телефоні"

if ! command -v python3 >/dev/null || ! command -v termux-job-scheduler >/dev/null; then
    say "встановлюю python і termux-api (одноразово, кілька хвилин)"
    pkg install -y python termux-api
fi

# Спершу все качаємо в тимчасовий каталог і перевіряємо: обірване
# завантаження не має права зіпсувати робочу копію.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
say "завантажую свіжу версію"
for f in $FILES; do
    mkdir -p "$TMP/$(dirname "$f")"
    curl -fsSL "$BASE/$f" -o "$TMP/$f" || die "не вдалося завантажити $f"
done
python3 -m py_compile "$TMP/tt_checkin.py" || die "завантажений tt_checkin.py битий"

# Старий демон тримає стару версію в пам'яті — без зупинки оновлення не діє.
say "зупиняю старого демона (якщо був)"
pkill -f 'tt_checkin.py' || true

mkdir -p "$APP/boot" "$HOME/.termux/boot"
for f in $FILES; do
    cp "$TMP/$f" "$APP/$f"
done
chmod +x "$APP/tick.sh" "$APP/boot/tt-checkin.sh"
cp "$APP/boot/tt-checkin.sh" "$HOME/.termux/boot/"

cd "$APP"

# Код виходу: 0 — токен живий, 1 — мертвий, 2 — мережа недоступна.
token_alive() {
    python3 - <<'PY'
import sys
from datetime import date
import tt_checkin as tt
try:
    user, _, _ = tt._resolve_access(tt.Client(tt.Store().read_token()), date.today())
except tt.NetworkError:
    sys.exit(2)
sys.exit(0 if user else 1)
PY
}

# Креди з `--remember` перелогінюють самі, тому перевіряти є сенс лише токен
# без кредів: такий міг померти ще на старій версії.
need_login=""
if [ ! -s "$HOME/.tt-checkin/credentials" ]; then
    if [ ! -s "$HOME/.tt-checkin/token" ]; then
        need_login="перший запуск"
    else
        say "перевіряю збережений токен"
        rc=0; token_alive || rc=$?
        case $rc in
            0) echo "токен живий" ;;
            1) need_login="токен більше не працює" ;;
            *) echo "мережа недоступна — токен не перевірено, запусти команду ще раз пізніше" ;;
        esac
    fi
fi
if [ -n "$need_login" ]; then
    say "$need_login: вхід (корпоративна пошта і пароль від розкладу)"
    # stdin тут — труба від curl, тому логін читає з терміналу напряму.
    python3 tt_checkin.py login --remember < /dev/tty
fi

say "планую сторожа (раз на 15 хвилин) і запускаю демона"
termux-job-scheduler --job-id 0 --script "$APP/tick.sh" \
    --period-ms 900000 --persisted true >/dev/null
"$APP/tick.sh" || true

say "готово"
python3 tt_checkin.py status || true
tail -3 "$HOME/.tt-checkin/log.txt" 2>/dev/null || true
cat <<'EOF'

Якщо це перше встановлення, перевір ще раз руками:
  • встановлені Termux:API і Termux:Boot з F-Droid, Termux:Boot відкритий один раз;
  • Налаштування → Застосунки → Termux → Батарея → «Без обмежень»;
  • Termux дозволено показувати сповіщення.
Оновлення — та сама команда ще раз.
EOF
