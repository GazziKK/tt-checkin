#!/usr/bin/env python3
"""Автоматична відмітка відвідування в TimeTable SNU.

Один файл, тільки стандартна бібліотека. Копіюється на телефон як є.
Дизайн і причини рішень: docs/superpowers/specs/2026-09-08-tt-checkin-design.md

    python3 tt_checkin.py login
    python3 tt_checkin.py tick
    python3 tt_checkin.py status
    python3 tt_checkin.py daemon
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from getpass import getpass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# ─── КОНСТАНТИ ──────────────────────────────────────────────────────────────

BASE_URL = "https://timetable.lond.lg.ua/"
AVTORIZATION = "php/Avtorization.php"
GET_QUERY = "php/getQuery.php"

DATE_FMT = "%d.%m.%Y"
TIME_FMT = "%H:%M:%S"

TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Linux; Android) tt-checkin"

STATE_ROOT = Path.home() / ".tt-checkin"

# Успішна відмітка нотифікації не дає: чотири повідомлення щодня назавжди
# перетворюються на шум, а мовчання й означає «усе гаразд». Постав `True` на
# перший тиждень, поки не звикнеш довіряти автоматиці.
NOTIFY_ON_SUCCESS = False

PENDING = "pending"
SENT = "sent"
DONE = "done"
# Термінальний стан: вікно закрилось, останній запит до журналу нас там не
# знайшов. Такої пари більше не торкаємось — інакше вона до півночі коштувала б
# по запиту на тік, уже нічого не змінюючи.
MISSED = "missed"

# Службові ключі в `state.json`. Ключ пари починається з дати (`DD.MM.YYYY#…`),
# тому префікс «#» гарантує, що маркери з парами не переплутаються.
MARK_DEAD_TOKEN = "#dead-token"
MARK_MISSED = "#missed"
MARK_PROBED = "#probed"
MARK_RELOGIN = "#relogin"

# ─── МОДЕЛЬ ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Lesson:
    """Одна пара з розкладу на день.

    `kind` і `week_numbers` названі за справжнім змістом, але у запиті відмітки
    надсилаються в полях `lesson` і `type` — сайт їх плутає, ми повторюємо.
    """

    id_lesson: str
    num_lesson: str
    kind: str
    week_numbers: str
    teacher: str
    id_fio: str
    group: str
    dateles: str
    timebeg: str
    timeend: str
    discipline: str

    @property
    def key(self) -> str:
        return f"{self.dateles}#{self.id_lesson}#{self.num_lesson}"

    def window(self) -> tuple[datetime, datetime]:
        day = datetime.strptime(self.dateles, DATE_FMT).date()
        beg = datetime.strptime(self.timebeg, TIME_FMT).time()
        end = datetime.strptime(self.timeend, TIME_FMT).time()
        return datetime.combine(day, beg), datetime.combine(day, end)


@dataclass(frozen=True)
class User:
    """Контекст користувача з відповіді checkAccess."""

    fio: str
    id_user: str
    id_fio: str
    group: str
    id_group: str


@dataclass(frozen=True)
class DaySchedule:
    weekday: str
    week_number: int
    lessons: tuple[Lesson, ...]
    # Скільки рядків з даними парсер відкинув. Часткова втрата пар інакше
    # невидима: `lessons` непорожній, і про зникле заняття ніхто не дізнається.
    skipped: int = 0

# ─── ПЕРІОД ─────────────────────────────────────────────────────────────────


def academic_period(day: date) -> tuple[str, int]:
    """Сервер цих значень не віддає — їх треба надсилати. Правило взяте з коду
    сайту: вересень–грудень → Y/Y+1 семестр 1; січень → Y-1/Y семестр 1;
    лютий–серпень → Y-1/Y семестр 2."""
    if day.month >= 9:
        return f"{day.year}/{day.year + 1}", 1
    if day.month == 1:
        return f"{day.year - 1}/{day.year}", 1
    return f"{day.year - 1}/{day.year}", 2


def candidates(day: date) -> list[tuple[str, int]]:
    """Основний період плюс суміжні — якщо межі семестру не збігаються з нашим
    правилом, checkAccess має що спробувати."""
    year, semestr = academic_period(day)
    first, second = (int(part) for part in year.split("/"))
    previous = f"{first - 1}/{second - 1}"
    other = 2 if semestr == 1 else 1
    result: list[tuple[str, int]] = []
    for item in [(year, semestr), (year, other), (previous, semestr)]:
        if item not in result:
            result.append(item)
    return result

# ─── ЛОГІКА ─────────────────────────────────────────────────────────────────


def window_is_open(now: datetime, lesson: Lesson) -> bool:
    """Початок включно, кінець виключно."""
    beg, end = lesson.window()
    return beg <= now < end


def plan_tick(
    now: datetime,
    lessons: tuple[Lesson, ...],
    state: dict[str, Any],
    min_age: timedelta = timedelta(0),
) -> tuple[tuple[Lesson, ...], tuple[Lesson, ...]]:
    """Що робити на цьому тіку: (перевірити журнал, відмітити).

    `done` і `missed` не торкаємось узагалі. `sent` завжди йде на перевірку
    журналом, а не на повторну відправку — щоб не накрутити лічильник
    «Завантаження». Обидва списки все одно починаються з читання журналу
    (див. `_settle`), і жодна пара не потрапляє в обидва: одна пара за тік —
    щонайбільше один клік.

    `min_age` — це запасний шлях сторожа: він працює лише з парами, чий
    початок минув більше ніж на цей проміжок, і так лишає демону першу спробу.
    """
    to_verify: list[Lesson] = []
    to_check_in: list[Lesson] = []

    for lesson in lessons:
        current = state.get(lesson.key, PENDING)
        if current in (DONE, MISSED):
            continue
        beg, end = lesson.window()
        if min_age and now - beg <= min_age:
            # Пара щойно почалась — це хвилини демона. Умова свідомо не діє при
            # `min_age=0`, інакше пара точно на `timebeg` випадала б з тіку.
            continue
        if now < beg:
            continue
        if now < end:
            # Відсутній ключ теж означає `pending` — це нормальний початок дня.
            # `sent` і нерозпізнане значення йдуть на перевірку журналом:
            # fail-safe коштує один запит, fail-open — видимий клік.
            (to_check_in if current == PENDING else to_verify).append(lesson)
            continue
        # Вікно закрилось. Один останній запит до журналу — і пара або `done`
        # (хтось відмітився вручну, або наша відправка все ж зареєструвалась),
        # або `missed` назавжди. `_settle` тут відправити нічого не може:
        # вікно закрите.
        to_verify.append(lesson)

    return tuple(to_verify), tuple(to_check_in)


# Порожній кеш робочого дня перечитується: розклад можуть опублікувати з
# запізненням, і кеш сховав би це до півночі.
PLAN_EMPTY_TTL = timedelta(hours=4)
PLAN_RECHECK_FROM = 7
PLAN_RECHECK_TO = 19


def empty_plan_is_stale(now: datetime, mtime: float | None) -> bool:
    """Чи варто перечитати порожній план дня. Не частіше ніж раз на 4 години і
    лише в межах 07:00–19:00 робочого дня: вихідні й ніч кешуються на весь
    день, бо порожня субота — це не підозра, а норма. Ціна — до трьох зайвих
    запитів на порожній робочий день."""
    if now.weekday() >= 5 or not PLAN_RECHECK_FROM <= now.hour < PLAN_RECHECK_TO:
        return False
    if mtime is None:
        return False
    # Час забору беремо з mtime самого файла плану — окремий маркер у стані був
    # би ще одним джерелом істини про те саме.
    return datetime.fromtimestamp(mtime) + PLAN_EMPTY_TTL <= now

# ─── ПАРСИНГ ────────────────────────────────────────────────────────────────

# Відповіді getQuery.php — не JSON: це рядок, розділений вертикальною рискою,
# у якому одна з частин містить HTML рядків <tr>.
#
#   розклад: [0] день тижня, [1] номер тижня (<=0 — даних немає), [2] рядки
#   журнал:  [0] рядки, решта не використовується
#
# Таблиця розкладу має 9 колонок:
#   0:№  1:Час  2:Вид  3:Дисципліна  4:moodle  5:№ тижня  6:Викладач
#   7:Група  8:Аудиторія


def _clean(text: str) -> str:
    """Текст так, як його бачить innerText: <br> уже перетворено в перевід
    рядка, тут з'їдаються зайві пробіли і порожні рядки."""
    lines = (" ".join(part.split()) for part in text.split("\n"))
    return "\n".join(line for line in lines if line)


class _RowParser(HTMLParser):
    """Збирає рядки таблиці: атрибути <tr> плюс текст кожної <td>.
    Парсер не покладається на добре сформований HTML: новий тег <tr>/<td>
    неявно закриває попередній, незакритий останній рядок теж потрапляє
    в результат."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[dict[str, str], list[str]]] = []
        self._attrs: dict[str, str] | None = None
        self._cells: list[str] | None = None
        self._buf: list[str] | None = None
        self._depth = 0  # глибина вкладених у клітинку таблиць

    def _flush_cell(self) -> None:
        if self._buf is not None and self._cells is not None:
            self._cells.append(_clean("".join(self._buf)))
        self._buf = None

    def _flush_row(self) -> None:
        self._flush_cell()
        if self._cells is not None:
            self.rows.append((self._attrs or {}, self._cells))
        self._attrs, self._cells = None, None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "br":
            if self._buf is not None:
                self._buf.append("\n")
            return
        if tag == "table":
            # Таблиця всередині клітинки: її <tr>/<td> — це вміст клітинки,
            # а не межа зовнішнього рядка. Без цієї перевірки внутрішній <tr>
            # закривав би зовнішню пару, і одна пара з двох зникала б.
            if self._buf is not None:
                self._depth += 1
            return
        if self._depth:
            return
        if tag == "tr":
            self._flush_row()  # HTML5: новий <tr> неявно закриває попередній
            self._attrs = {name: (value or "") for name, value in attrs}
            self._cells = []
        elif tag == "td" and self._cells is not None:
            self._flush_cell()  # новий <td> неявно закриває попередній
            self._buf = []

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "br" and self._buf is not None:
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._depth:
                self._depth -= 1
            return
        if self._depth:
            return
        if tag == "td":
            self._flush_cell()
        elif tag == "tr":
            self._flush_row()

    def handle_data(self, data: str) -> None:
        if self._buf is not None:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush_row()  # незакритий останній рядок теж має потрапити в результат


def _parse_rows(html: str) -> list[tuple[dict[str, str], list[str]]]:
    parser = _RowParser()
    parser.feed(html)
    parser.close()
    return parser.rows


SCHEDULE_CELLS = 9


def _times_are_valid(attrs: dict[str, str]) -> bool:
    """Дата і час рядка мусять парситись тут, а не в `Lesson.window()`:
    інакше `ValueError` вилітає вже посеред тіку, і побитий план ще й
    лишається в кеші на весь день."""
    try:
        datetime.strptime(attrs.get("data-dateles", ""), DATE_FMT)
        datetime.strptime(attrs.get("data-timebeg", ""), TIME_FMT)
        datetime.strptime(attrs.get("data-timeend", ""), TIME_FMT)
    except ValueError:
        return False
    return True


def parse_day(text: str) -> DaySchedule:
    parts = text.split("|", 2)
    if len(parts) < 3:
        return DaySchedule("", 0, ())

    weekday, raw_week, rows_html = parts
    try:
        week_number = int(raw_week.strip())
    except ValueError:
        week_number = 0
    if week_number <= 0:
        return DaySchedule(weekday.strip(), 0, ())

    lessons: list[Lesson] = []
    skipped = 0
    for attrs, cells in _parse_rows(rows_html):
        # Рівно 9 клітинок: видалення одного <td> зсуває колонки так, що
        # `week_numbers` стає рядком викладача, а на нього спирається поле
        # `type` у запиті журналу. Такий рядок краще втратити, ніж відправити.
        if (
            "data-id_lesson" not in attrs
            or len(cells) != SCHEDULE_CELLS
            or not _times_are_valid(attrs)
        ):
            if cells:
                skipped += 1  # рядок з даними, який не став парою — це втрата
            continue
        lessons.append(
            Lesson(
                id_lesson=attrs["data-id_lesson"],
                num_lesson=cells[0],
                kind=cells[2],
                week_numbers=cells[5],
                teacher=cells[6],
                id_fio=attrs.get("data-id_fio", ""),
                group=attrs.get("data-group", ""),
                dateles=attrs["data-dateles"],
                timebeg=attrs["data-timebeg"],
                timeend=attrs["data-timeend"],
                discipline=attrs.get("data-lesson", ""),
            )
        )
    return DaySchedule(weekday.strip(), week_number, tuple(lessons), skipped)


JOURNAL_COLUMNS = ("num", "fio", "status", "group", "time", "loads")


def parse_journal(text: str) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    for _attrs, cells in _parse_rows(text.split("|")[0]):
        if len(cells) < len(JOURNAL_COLUMNS):
            continue
        rows.append(dict(zip(JOURNAL_COLUMNS, cells)))
    return tuple(rows)


def registration_row(
    rows: tuple[dict[str, str], ...], fio: str
) -> dict[str, str] | None:
    """Наш рядок журналу, або None, якщо нас там немає. Повертається весь
    рядок, а не лише час: потрібна ще колонка `loads` («Завантаження»).
    Порівняння точне (не підрядок), але терпиме до регістру і зайвих пробілів."""
    wanted = " ".join(fio.split()).casefold()
    for row in rows:
        if " ".join(row["fio"].split()).casefold() == wanted:
            return row
    return None


# ─── HTTP ───────────────────────────────────────────────────────────────────

# Два скрипти з різним кодуванням тіла:
#   php/Avtorization.php — JSON
#   php/getQuery.php     — application/x-www-form-urlencoded


class NetworkError(Exception):
    """Мережа недоступна або сервер не відповів. Тік має це переживати,
    а не падати: наступний тік у межах вікна повторить."""


class Client:
    def __init__(self, token: str | None = None, base_url: str = BASE_URL) -> None:
        self.token = token
        self._base = base_url.rstrip("/") + "/"

    def _post(self, path: str, body: bytes, content_type: str) -> str:
        request = urllib.request.Request(self._base + path, data=body, method="POST")
        request.add_header("Content-Type", content_type)
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("Referer", self._base)
        if self.token:
            request.add_header("Cookie", f"tt_tokin={self.token}")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                self._capture_token(response)
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            raise NetworkError(f"HTTP {error.code}") from error
        except (urllib.error.URLError, OSError) as error:
            raise NetworkError(str(error)) from error

    def _capture_token(self, response) -> None:
        """Логін віддає tt_tokin у Set-Cookie. Ім'я cookie — завжди перший
        фрагмент, решта — атрибути; `Max-Age<=0` означає logout, і живий
        токен таким cookie перезаписувати не можна (інакше він стане
        рядком `deleted`)."""
        for header in response.headers.get_all("Set-Cookie") or []:
            first, _, rest = header.partition(";")
            name, _, value = first.partition("=")
            if name.strip() != "tt_tokin" or not value.strip():
                continue
            attrs: dict[str, str] = {}
            for chunk in rest.split(";"):
                key, _, raw = chunk.strip().partition("=")
                attrs[key.strip().casefold()] = raw.strip()
            try:
                if int(attrs.get("max-age", "1")) <= 0:
                    continue
            except ValueError:
                pass
            self.token = value.strip()

    def _post_json(self, payload: dict) -> str:
        return self._post(
            AVTORIZATION,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json;charset=utf-8",
        )

    def _post_form(self, payload: dict) -> str:
        return self._post(
            GET_QUERY,
            urllib.parse.urlencode(payload, encoding="utf-8").encode("utf-8"),
            "application/x-www-form-urlencoded",
        )

    def login(self, login: str, password: str) -> None:
        """Надсилає пароль так, як це робить сама сторінка входу: відкритим
        текстом у JSON по HTTPS. Успіх перевіряється не тут, а check_access."""
        self._post_json({"method": "checkPassword", "login": login, "password": password})

    def check_access(self, year: str, semestr: int) -> User | None:
        """Контекст користувача. None означає «токен не працює або період не той»
        — розрізняти ці випадки нам не потрібно."""
        raw = self._post_json({"method": "checkAccess", "year": year, "semestr": semestr})
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("status") != "ok":
            return None
        try:
            user = json.loads(data.get("user", ""))
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(user, dict) or not user.get("group"):
            return None
        return User(
            fio=str(user.get("fio", "")),
            id_user=str(user.get("id_user", "")),
            id_fio=str(user.get("id_fio", "")),
            group=str(user["group"]),
            id_group=str(user.get("id_group", "")),
        )

    def day_schedule(self, user: User, year: str, semestr: int, day: date) -> str:
        return self._post_form(
            {
                "param": "get_listLessonTodayStudent",
                "group": user.group,
                "id_group": user.id_group,
                "date": day.strftime(DATE_FMT),
                "year": year,
                "semestr": semestr,
            }
        )

    def check_in(self, user: User, lesson: Lesson) -> str:
        """Поля `lesson` і `type` навмисно несуть Вид і № тижня — саме так
        робить сайт (див. §2 спеки)."""
        return self._post_form(
            {
                "param": "get_checkZoom",
                "state": "s",
                "group": user.group,
                "fioUser": user.fio,
                "idUser": user.id_user,
                "numLesson": lesson.num_lesson,
                "lesson": lesson.kind,
                "id_lesson": lesson.id_lesson,
                "type": lesson.week_numbers,
                "fioTeacher": lesson.teacher,
                "id_fio": lesson.id_fio,
                "dateles": lesson.dateles,
                "timebeg": lesson.timebeg,
                "timeend": lesson.timeend,
            }
        )

    def journal(self, user: User, lesson: Lesson) -> str:
        """Увага: тут поле називається `date`, а не `dateles`."""
        return self._post_form(
            {
                "param": "get_listStudent",
                "group": user.group,
                "numLesson": lesson.num_lesson,
                "lesson": lesson.kind,
                "id_lesson": lesson.id_lesson,
                "type": lesson.week_numbers,
                "akademGroup": "1",
                "date": lesson.dateles,
                "id_fio": lesson.id_fio,
            }
        )

# ─── СХОВИЩЕ ────────────────────────────────────────────────────────────────

# Каталог 0700, файли 0600. Пароль тут не зберігається взагалі,
# токен у лог не потрапляє ніколи.


class Store:
    def __init__(self, root: Path = STATE_ROOT) -> None:
        self.root = Path(root)

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def _write_private(self, name: str, content: str) -> None:
        """Запис через тимчасовий файл і `os.replace`: пряме `write_text`
        спершу обрізає файл, і процес, убитий посередині, лишав би побитий
        JSON. Такий JSON читається як `{}` — і весь день пішов би на
        перевідправку. Лог цього не потребує: там тільки append."""
        self._ensure_root()
        path = self.root / name
        tmp = self.root / f".{name}.{os.getpid()}.tmp"
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.chmod(0o600)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def _mtime(self, name: str) -> float | None:
        try:
            return (self.root / name).stat().st_mtime
        except OSError:
            return None

    def read_token(self) -> str | None:
        path = self.root / "token"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def write_token(self, value: str) -> None:
        self._write_private("token", value)

    def token_mtime(self) -> float | None:
        """Відступ при мертвому токені привʼязаний саме до mtime: щойно
        користувач зробить `login`, файл перепишеться, mtime змінюється —
        і маркер «токен мертвий» сам стає нечинним."""
        return self._mtime("token")

    def has_credentials(self) -> bool:
        """Чи є з чим пробувати перелогін. Нечитабельний файл — це «немає»:
        інакше зламаний JSON вимкнув би і перелогін, і нотифікацію про
        мертвий токен, тобто поламка стала б зовсім тихою."""
        return self.read_credentials() is not None

    def read_credentials(self) -> tuple[str, str] | None:
        """Необовʼязковий файл кредів (його може створити й сам користувач).
        Назовні йде або пара значень, або None: ні вміст файла, ні текст
        помилки розбору не мають шансу потрапити в лог чи в нотифікацію."""
        try:
            data = json.loads((self.root / "credentials").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        login = str(data.get("login") or "")
        password = str(data.get("password") or "")
        if not login or not password:
            return None
        return login, password

    def write_credentials(self, login: str, password: str) -> None:
        self._write_private(
            "credentials",
            json.dumps({"login": login, "password": password}, ensure_ascii=False),
        )

    def credentials_mtime(self) -> float | None:
        """Лічильник невдалих входів скидається, коли цей mtime змінився:
        користувач виправив пароль — старі невдачі більше не чинні."""
        return self._mtime("credentials")

    def read_state(self) -> dict[str, Any]:
        path = self.root / "state.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def write_state(self, state: dict[str, Any]) -> None:
        self._write_private("state.json", json.dumps(state, ensure_ascii=False, indent=1))

    def read_plan(self, day: date) -> tuple[Lesson, ...] | None:
        path = self.root / f"plan-{day.isoformat()}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, list):
            return None
        try:
            return tuple(Lesson(**item) for item in data)
        except TypeError:
            return None

    def write_plan(self, day: date, lessons: tuple[Lesson, ...]) -> None:
        self._write_private(
            f"plan-{day.isoformat()}.json",
            json.dumps([asdict(item) for item in lessons], ensure_ascii=False, indent=1),
        )

    def plan_mtime(self, day: date) -> float | None:
        return self._mtime(f"plan-{day.isoformat()}.json")

    def _open_lock(self, name: str):
        self._ensure_root()
        path = self.root / name
        handle = path.open("a", encoding="utf-8")
        path.chmod(0o600)
        return handle

    def open_lock(self):
        """Файл-замок від одночасних відміток. Тримається відкритим на весь час
        тіку; закриття дескриптора знімає блокування. Його бере і тік, і демон:
        інакше обидва прочитали б той самий стан і клікнули двічі."""
        return self._open_lock("lock")

    def open_daemon_lock(self):
        """Замок демона. Живий демон тримає його весь час життя процесу, і
        зайнятий замок — це і є ознака «демон живий» для сторожа. PID-файл не
        потрібен: flock звільняється смертю процесу, тому за вбитим демоном
        нічого прибирати не треба."""
        return self._open_lock("daemon.lock")

    @property
    def log_path(self) -> Path:
        return self.root / "log.txt"

    def log(self, message: str) -> None:
        self._ensure_root()
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
        self.log_path.chmod(0o600)


def try_lock(handle) -> bool:
    """Неблокуюче захоплення замку. Перевірено на цільовій системі: зайнятий
    замок дає `BlockingIOError` (підклас `OSError`), а сам замок звільняється
    смертю процесу — тому «чи живий процес» = чи вдалось узяти його замок."""
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True

# ─── ОРКЕСТРАЦІЯ ────────────────────────────────────────────────────────────

DEAD_TOKEN = "токен не працює — запусти `python3 tt_checkin.py login`"
NO_NETWORK = "немає мережі — наступний тік повторить"
RELOGIN_STOPPED = (
    "перелогін зі збережених кредів не вдався тричі підряд — перевір "
    "~/.tt-checkin/credentials і запусти `python3 tt_checkin.py login`"
)

# Мертвий токен коштував би 3 запити на тік, тобто сотні на добу в нікуди, а
# полагодити його може лише користувач. Тому маркер із mtime файла токена і
# спроба не частіше ніж раз на годину — страховка на випадок, що сервер
# віддавав не-`ok` тимчасово.
RETRY_DEAD_TOKEN = timedelta(hours=1)
# Зі сторожем раз на 15 хвилин хибний пароль дав би 96 спроб входу на добу —
# це шлях до блокування акаунта на боці університету.
RETRY_RELOGIN = timedelta(hours=1)
RELOGIN_MAX_FAILS = 3
# Запасний шлях сторожа працює тільки з парами, чий початок минув понад це:
# першу спробу лишаємо демону, щоб вони не приходили в ту саму хвилину.
LATE_FALLBACK = timedelta(minutes=5)

Notifier = Callable[[str, str], None]


def notify(title: str, message: str) -> None:
    """Нотифікація через Termux API. Не на телефоні — тихо нічого не робимо.
    `--id` один для всіх повідомлень: повтори заміщають одне одного, а не
    складаються в стос, який починають змахувати не читаючи."""
    try:
        subprocess.run(
            [
                "termux-notification",
                "--id",
                "tt-checkin",
                "--title",
                title,
                "--content",
                message,
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        # Не тільки FileNotFoundError: невиконуваний termux-notification на
        # PATH дає PermissionError, і через нього падав би цілий тік.
        pass


def _remember(
    store: Store, state: dict[str, Any], key: str, value: Any
) -> bool:
    """Пише стан на диск одразу після зміни (`value=None` — прибрати ключ).
    Повертає True, якщо стан справді змінився — за цим тік вирішує, чи є
    про що писати в лог.

    Один запис у кінці тіку означав, що тік, який упав після відправки,
    лишав стан порожнім, і наступний тік відправляв ще раз. Збій самого
    запису йде в лог і не далі: джерело істини про «вже відмічено» —
    журнал, тому тік має право дожити до кінця.
    """
    if value is None:
        if key not in state:
            return False
        del state[key]
    else:
        if state.get(key) == value:
            return False
        state[key] = value
    try:
        store.write_state(state)
    except OSError as error:
        store.log(f"УВАГА: стан не збережено ({key}): {error}")
    return True


def _resolve_access(
    client: Client, day: date
) -> tuple[User | None, str | None, int | None]:
    """Перший період, який пройшов checkAccess. Це ще не остаточний вибір:
    порожній розклад змушує `_load_lessons` перебрати решту кандидатів."""
    for year, semestr in candidates(day):
        user = client.check_access(year, semestr)
        if user is not None:
            return user, year, semestr
    return None, None, None


def _mark(state: dict[str, Any], key: str) -> dict[str, Any]:
    """Копія службового маркера-словника. Копія, а не сам словник: `_remember`
    вирішує, чи писати стан, порівнянням зі збереженим значенням."""
    value = state.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _moment(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _too_soon(now: datetime, raw: Any, interval: timedelta) -> bool:
    """Чи ще рано для наступної спроби. Час у майбутньому (системний час
    зсунули назад) відступ не продовжує: краще одна зайва спроба, ніж скрипт,
    який замовк до кінця семестру."""
    last = _moment(raw)
    if last is None:
        return False
    return timedelta(0) <= now - last < interval


def _fails(mark: dict[str, Any]) -> int:
    try:
        return int(mark.get("fails") or 0)
    except (TypeError, ValueError):
        return 0


def _dead_token_backoff(store: Store, state: dict[str, Any], now: datetime) -> bool:
    """True — токен уже визнано мертвим і пробувати ще рано: тік має вийти
    БЕЗ жодного запиту. Маркер привʼязаний до mtime обох файлів, з яких можна
    здобути доступ, тому і `login`, і виправлений вручну файл кредів знімають
    відступ одразу, а не через годину."""
    mark = _mark(state, MARK_DEAD_TOKEN)
    if not mark or mark.get("token_mtime") != store.token_mtime():
        return False
    if mark.get("creds_mtime") != store.credentials_mtime():
        return False
    return _too_soon(now, mark.get("tried_at"), RETRY_DEAD_TOKEN)


def _dead_token_message(store: Store, state: dict[str, Any]) -> str | None:
    """Що сказати користувачеві про мертвий токен. None — поки що нічого:
    креди збережені, спроби перелогіну ще не вичерпані, і скрипт має шанс
    полагодити себе сам протягом години. Одна поламка — одна нотифікація на
    добу, інакше канал перестають читати."""
    if not store.has_credentials():
        return DEAD_TOKEN
    if _fails(_mark(state, MARK_RELOGIN)) >= RELOGIN_MAX_FAILS:
        return RELOGIN_STOPPED
    return None


def _note_dead_token(
    store: Store, state: dict[str, Any], notifier: Notifier, now: datetime
) -> None:
    """Токен мертвий (і перелогін не допоміг). Рядок і нотифікація — один раз
    на добу, а не на кожен тік: однакові повідомлення користувач починає
    змахувати рефлекторно, а лог — не читати."""
    mark = _mark(state, MARK_DEAD_TOKEN)
    day = now.date().isoformat()
    message = _dead_token_message(store, state)
    fresh = message is not None and mark.get("notified_day") != day
    mark["token_mtime"] = store.token_mtime()
    mark["creds_mtime"] = store.credentials_mtime()
    mark["tried_at"] = now.isoformat(timespec="seconds")
    if fresh:
        mark["notified_day"] = day
    _remember(store, state, MARK_DEAD_TOKEN, mark)
    if fresh and message is not None:
        store.log(message)
        notifier("tt-checkin", message)


def _relogin(
    client: Client,
    store: Store,
    state: dict[str, Any],
    now: datetime,
    day: date,
) -> tuple[User | None, str | None, int | None]:
    """Токен мертвий, але креди збережені — входимо самі й продовжуємо тік з
    місця, де спинились. Нотифікація лишається лише на випадок, коли й це не
    вдалось (її робить `_note_dead_token`).

    Обмеження проти блокування акаунта — найважливіша частина цієї функції:
    спроба не частіше ніж раз на годину і не більше трьох невдач підряд.
    Пароль не потрапляє ні в лог, ні в текст помилки, ні в нотифікацію.
    """
    credentials = store.read_credentials()
    if credentials is None:
        return None, None, None

    mark = _mark(state, MARK_RELOGIN)
    creds_mtime = store.credentials_mtime()
    if mark.get("creds_mtime") != creds_mtime:
        # Користувач виправив пароль — старі невдачі більше не чинні.
        mark = {"creds_mtime": creds_mtime}
    fails = _fails(mark)
    if fails >= RELOGIN_MAX_FAILS:
        # Здались: далі поводимось так, як з мертвим токеном без кредів —
        # нуль запитів на вхід. Про це нотифікує `_note_dead_token`, раз на
        # добу, поки користувач не виправить файл кредів.
        return None, None, None
    if _too_soon(now, mark.get("at"), RETRY_RELOGIN):
        return None, None, None

    login, password = credentials
    # Спробу записуємо ДО запиту: якщо процес умре посередині, невідомо, чи
    # запит дійшов до сервера, і краще зачекати годину, ніж стукати в акаунт.
    mark["at"] = now.isoformat(timespec="seconds")
    mark["fails"] = fails
    _remember(store, state, MARK_RELOGIN, mark)
    try:
        client.login(login, password)
        user, year, semestr = _resolve_access(client, day)
    finally:
        del password

    if user is None:
        # Сервер відповів, але доступу немає — це схоже на хибний пароль.
        # NetworkError сюди не доходить: він летить вище, і лічильник невдач
        # не рухається, бо з мережевої помилки про пароль нічого не видно.
        fails += 1
        mark["fails"] = fails
        _remember(store, state, MARK_RELOGIN, mark)
        # Нотифікує не ця гілка, а `_note_dead_token`: поки спроби не
        # вичерпані, скрипт може полагодити себе сам, і повідомлення «запусти
        # login» було б передчасним. У лозі лишається кожна спроба.
        store.log(f"перелогін не вдався: спроба {fails} з {RELOGIN_MAX_FAILS}")
        return None, None, None

    # Успіх: лічильник невдач у нуль, новий токен одразу на диск — інакше
    # падіння посеред тіку змусило б входити ще раз. У лозі — тільки факт
    # і період, ніяких кредів.
    _remember(store, state, MARK_RELOGIN, {"creds_mtime": creds_mtime})
    if client.token:
        try:
            store.write_token(client.token)
        except OSError as error:
            store.log(f"УВАГА: новий токен не збережено: {error}")
    store.log(f"перелогін зі збережених кредів: період {year} семестр {semestr}")
    return user, year, semestr


def _obtain_access(
    client: Client,
    store: Store,
    state: dict[str, Any],
    notifier: Notifier,
    now: datetime,
    day: date,
) -> tuple[User | None, str | None, int | None]:
    """Контекст користувача: відступ при мертвому токені (нуль запитів) →
    checkAccess → перелогін зі збережених кредів. None означає «доступу немає»,
    і функція вже сама написала в лог і нотифікувала, якщо було про що."""
    if _dead_token_backoff(store, state, now):
        return None, None, None
    user, year, semestr = _resolve_access(client, day)
    if user is None:
        user, year, semestr = _relogin(client, store, state, now, day)
    if user is None:
        _note_dead_token(store, state, notifier, now)
        return None, None, None
    # Токен знову живий — наступна смерть має право нотифікувати.
    _remember(store, state, MARK_DEAD_TOKEN, None)
    return user, year, semestr


def _persist_token(store: Store, client: Client) -> None:
    """Сервер міг оновити cookie посеред тіку; якщо не зберегти, робочий токен
    викидається, і скрипт починає вимагати `login` без причини. Порівнюємо з
    тим, що на диску, а не з тим, що читали на початку: перелогін уже міг
    записати новий токен сам."""
    if not client.token:
        return
    try:
        if store.read_token() == client.token:
            return
        store.write_token(client.token)
    except OSError as error:
        store.log(f"УВАГА: новий токен не збережено: {error}")
        return
    store.log("токен оновлено сервером")


def _warn_skipped(store: Store, schedule: DaySchedule) -> None:
    if schedule.skipped:
        # Часткова втрата пар інакше невидима: `lessons` непорожній.
        store.log(
            f"УВАГА: рядків розкладу відкинуто: {schedule.skipped} — "
            "можливо, змінилася розмітка"
        )


def _markup_broke(store: Store, schedule: DaySchedule) -> bool:
    """`№ тижня > 0`, а пар нуль — це не «порожній день», а зламана розмітка:
    сервер знає номер тижня, отже дані на дату є. Такий день не кешуємо, бо
    наступний тік може застати сервер уже виправленим. Перевірка однакова для
    основного кандидата і для перебраних: інакше перебраний кандидат з битою
    розміткою виглядав би просто порожнім, і день кешувався б як «можливо,
    хибний семестр»."""
    if schedule.week_number <= 0 or schedule.lessons:
        return False
    store.log(
        "УВАГА: відповідь розкладу непорожня, але жодної пари не "
        "розібрано — можливо, змінився формат"
    )
    return True


def _load_lessons(
    client: Client,
    store: Store,
    user: User,
    year: str,
    semestr: int,
    day: date,
    state: dict[str, Any],
) -> tuple[Lesson, ...]:
    """Забирає розклад на день з сервера і кешує його. Кеш читає викликач.

    Порожній день кешується — інакше будильник кожні 15–30 хвилин ходив би
    в мережу за тим самим «нічого». Але «порожньо» бувають двох різних сортів, і
    розрізняє їх сам сервер (§2 спеки: `№ тижня <= 0` — даних на цю дату
    немає):

    - `week_number <= 0` — сервер каже, що на цю дату нічого немає. Кешуємо;
    - `week_number > 0`, а пар нуль — зламалась розмітка. Не кешуємо, бо
      наступний тік може застати сервер уже виправленим.

    Порожній **робочий** день — це ще й симптом хибного семестру (той
    проходить checkAccess, але віддає порожньо щодня), тому решту кандидатів
    з `candidates()` пробуємо один раз на добу. Вихідні кешуються одразу:
    порожня субота — це не підозра.
    """
    schedule = parse_day(client.day_schedule(user, year, semestr, day))
    _warn_skipped(store, schedule)

    if schedule.lessons:
        store.write_plan(day, schedule.lessons)
        return schedule.lessons

    if _markup_broke(store, schedule):
        return ()

    if day.weekday() < 5 and state.get(MARK_PROBED) != day.isoformat():
        for other_year, other_semestr in candidates(day):
            if (other_year, other_semestr) == (year, semestr):
                continue
            probe = parse_day(
                client.day_schedule(user, other_year, other_semestr, day)
            )
            _warn_skipped(store, probe)
            if _markup_broke(store, probe):
                return ()
            if probe.lessons:
                store.log(
                    f"розклад узято з періоду {other_year}, "
                    f"семестр {other_semestr}"
                )
                store.write_plan(day, probe.lessons)
                return probe.lessons
        # Маркер ставимо тільки тут: якщо проба знайшла розклад, її треба
        # мати право повторити (наприклад, коли план видалили руками).
        _remember(store, state, MARK_PROBED, day.isoformat())
        store.log("УВАГА: порожній робочий день — можливо, хибний семестр")

    store.write_plan(day, ())
    return ()


def _confirm(client: Client, user: User, lesson: Lesson) -> dict[str, str] | None:
    return registration_row(parse_journal(client.journal(user, lesson)), user.fio)


def _settle(
    client: Client,
    user: User,
    store: Store,
    now: datetime,
    lesson: Lesson,
) -> tuple[str | None, bool, str | None]:
    """Одна пара за один тік: журнал → щонайбільше одна відмітка → журнал.
    Повертає (підтвердження, чи була відправка, помилка мережі).

    Рівно одна відправка за тік, і жодної, якщо відмітка вже в журналі.
    Колонка «Завантаження» — лічильник кліків, який видно викладачу в журналі
    заняття, і накрутку неможливо відкатити; тому невдале підтвердження
    (інший рендер ПІП, лаг запису журналу, зсув колонок) не має права
    спричинити другий клік. Пара, яку журнал не підтвердив, лишається в
    `sent`, і наступний тік починає з того самого читання журналу:
    самозаліковування є, але його квант — один клік на 30 хвилин.
    """
    sent = False
    try:
        row = _confirm(client, user, lesson)
        if row is None and window_is_open(now, lesson):
            # `sent` ставимо ДО запиту: якщо POST упаде, невідомо, чи він
            # дійшов, і пара мусить піти в `sent` (§7 спеки). Інакше вона
            # лишалась би `pending`, а успішна відмітка, що все ж
            # зареєструвалась, так і лишилась би невідомою.
            sent = True
            client.check_in(user, lesson)
            row = _confirm(client, user, lesson)
    except NetworkError as error:
        return None, sent, str(error)

    if row is None:
        return None, sent, None

    loads = (row.get("loads") or "").strip()
    if loads.isdigit() and int(loads) > 1:
        store.log(f"УВАГА: пара {lesson.num_lesson}: loads={loads}")
    # Підтверджує сам факт рядка в журналі, а не час: порожня клітинка часу
    # не має виглядати як «не підтверджено» і провокувати повторний клік.
    return (row.get("time") or "—"), sent, None


def run_tick(
    now: datetime,
    client: Client,
    store: Store,
    notifier: Notifier = notify,
    dry_run: bool = False,
    min_age: timedelta = timedelta(0),
    refresh: bool = False,
) -> str:
    """Один прохід. Ніколи не падає трейсбеком: неочікувана помилка йде
    в лог і в нотифікацію, бо інакше про неї знав би лише `cron.out`.

    `min_age` — запасний шлях сторожа (тільки пари, чий початок минув понад
    цей проміжок). `refresh` — розклад береться з мережі, а не з кешу: так
    робить демон, прокинувшись на ціль, бо пару могли скасувати.
    """
    try:
        return _run_tick(now, client, store, notifier, dry_run, min_age, refresh)
    except Exception as error:  # свідомо широко: тік не має падати тихо
        message = f"неочікувана помилка: {type(error).__name__}: {error}"
        try:
            store.log(message)
        except OSError:
            pass  # лог недоступний — нотифікація і stdout лишаються
        notifier("tt-checkin", message)
        return message


def _idle(lessons: tuple[Lesson, ...], dry_run: bool) -> str:
    """Рядок для тіку, якому нічого робити. У лог не йде: будильник
    приходить кожні 15–30 хвилин, тобто до ~96 разів на добу, і рядок на тік
    зробив би `log.txt` — файл, який README радить читати — самим шумом."""
    return f"{'[dry-run] ' if dry_run else ''}пар: {len(lessons)}; робити нічого"


def _report_missed(
    store: Store,
    state: dict[str, Any],
    notifier: Notifier,
    now: datetime,
    day: date,
    lessons: tuple[Lesson, ...],
) -> None:
    """Пари, чиє вікно закрилось, а `done` так і не сталось. Телефон, що
    продрімав ранок, інакше ніде про це не повідомляє. Мережі не потребує:
    усе вже є в плані і в стані.

    Маркер тримає множину вже відзвітованих пар, а не «звітував сьогодні»:
    з булевим маркером друга пропущена пара за день не потрапляла ні в лог,
    ні в нотифікацію — саме той канал, для якого цей звіт існує, губив подію.
    Нотифікація лишається одна на добу (щоб не спамити), лог отримує кожну
    пару.
    """
    mark = _mark(state, MARK_MISSED)
    if mark.get("day") != day.isoformat():
        mark = {"day": day.isoformat()}
    reported = [key for key in mark.get("keys") or [] if isinstance(key, str)]
    fresh = [
        lesson
        for lesson in lessons
        if state.get(lesson.key) != DONE
        and now >= lesson.window()[1]
        and lesson.key not in reported
    ]
    if not fresh:
        return
    numbers = [lesson.num_lesson for lesson in fresh]
    mark["keys"] = reported + [lesson.key for lesson in fresh]
    notified = mark.get("notified") is True
    mark["notified"] = True
    _remember(store, state, MARK_MISSED, mark)
    store.log(f"вікно закрилось без відмітки: пари {numbers}")
    if not notified:
        notifier("tt-checkin", f"вікно закрилось без відмітки: пари {numbers}")


def _run_tick(
    now: datetime,
    client: Client,
    store: Store,
    notifier: Notifier,
    dry_run: bool,
    min_age: timedelta,
    refresh: bool,
) -> str:
    day = now.date()
    state = store.read_state()

    # Спершу локальний план і локальний стан — і лише потім мережа. Будильник
    # приходить до ~96 разів на добу, і майже кожного разу робити нічого: між
    # парами, у вихідний і після того, як усі пари `done`, тік має коштувати
    # нуль запитів.
    lessons = None if refresh else store.read_plan(day)
    if lessons is not None and not lessons:
        if empty_plan_is_stale(now, store.plan_mtime(day)):
            # Порожній кеш робочого дня застарів: розклад могли опублікувати
            # з запізненням. Перечитуємо — це один додатковий запит, не
            # більше трьох разів на день.
            lessons = None
    to_verify: tuple[Lesson, ...] = ()
    to_check_in: tuple[Lesson, ...] = ()
    if lessons is not None:
        to_verify, to_check_in = plan_tick(now, lessons, state, min_age)
        if not to_verify and not to_check_in:
            _report_missed(store, state, notifier, now, day, lessons)
            return _idle(lessons, dry_run)

    # Далі без мережі не обійтись: або план ще не забирали, або є що робити.
    try:
        user, year, semestr = _obtain_access(client, store, state, notifier, now, day)
    except NetworkError as error:
        store.log(f"{NO_NETWORK} ({error})")
        return NO_NETWORK

    if user is None:
        # Рядок і нотифікація вже зроблені в `_obtain_access` — по разу на
        # добу, а не на кожен тік. Сам рядок «токен не працює» усе одно йде
        # в stdout, тобто в cron.out.
        return DEAD_TOKEN

    if lessons is None:
        try:
            lessons = _load_lessons(client, store, user, year, semestr, day, state)
        except NetworkError as error:
            store.log(f"{NO_NETWORK} ({error})")
            return NO_NETWORK
        to_verify, to_check_in = plan_tick(now, lessons, state, min_age)
        if not to_verify and not to_check_in:
            _report_missed(store, state, notifier, now, day, lessons)
            return _idle(lessons, dry_run)

    if dry_run:
        summary = (
            f"[dry-run] пар: {len(lessons)}; відмітив би: "
            f"{[item.num_lesson for item in to_check_in] or '—'}; "
            f"перевірив би: {[item.num_lesson for item in to_verify] or '—'}"
        )
        store.log(summary)
        return summary

    done: list[str] = []
    fresh_done: list[str] = []
    stuck: list[str] = []
    stuck_open: list[str] = []
    gone: list[str] = []
    changed = False

    # Обидва списки обробляються однаково: `_settle` сам починає з журналу,
    # тому «перевірити» і «відмітити» — це той самий один клік максимум.
    for lesson in to_verify + to_check_in:
        confirmed, sent, error = _settle(client, user, store, now, lesson)
        changed = changed or sent
        if error is not None:
            if sent:
                # Відправка пішла (або могла піти), підтвердження — ні. `sent`
                # змусить наступний тік спершу зазирнути в журнал.
                _remember(store, state, lesson.key, SENT)
            store.log(f"пара {lesson.num_lesson}: мережа впала ({error})")
            continue
        if confirmed:
            if _remember(store, state, lesson.key, DONE):
                changed = True
                fresh_done.append(f"{lesson.num_lesson}:{confirmed}")
            done.append(f"{lesson.num_lesson}:{confirmed}")
            continue
        if window_is_open(now, lesson):
            changed = _remember(store, state, lesson.key, SENT) or changed
            stuck.append(lesson.num_lesson)
            stuck_open.append(lesson.num_lesson)
            continue
        # Вікно закрилось, і останній запит до журналу нас там не знайшов:
        # термінальний стан. Далі ця пара не коштує жодного запиту — змінити
        # вже нічого не можна, а видимою вона лишається в `status` і в лозі.
        changed = _remember(store, state, lesson.key, MISSED) or changed
        gone.append(lesson.num_lesson)

    _report_missed(store, state, notifier, now, day, lessons)

    summary = f"пар: {len(lessons)}; підтверджено: {done or '—'}"
    if stuck:
        summary += f"; без підтвердження: {stuck}"
    if gone:
        summary += f"; пропущено: {gone}"
    if changed:
        # Пишемо в лог лише те, що справді сталося: відправку, підтвердження
        # або перехід стану. Пара, яка зависла в `sent` після закритого
        # вікна, інакше давала б однаковий рядок до кінця дня.
        store.log(summary)

    # Нотифікуємо лише поки є що врятувати: пара з закритим вікном уже
    # нікуди не подінеться, а повтор на кожному тіку знищує сам канал.
    if stuck_open:
        notifier("tt-checkin", f"відмітка без підтвердження: пари {stuck_open}")
    if NOTIFY_ON_SUCCESS and fresh_done:
        notifier("tt-checkin", f"відмічено: пари {fresh_done}")
    return summary


def tick_once(
    store: Store,
    now: datetime,
    notifier: Notifier = notify,
    dry_run: bool = False,
    min_age: timedelta = timedelta(0),
    refresh: bool = False,
) -> str | None:
    """Тік із токеном з диска і збереженням токена, який сервер міг оновити.
    Спільне для команди `tick`, для сторожа і для демона. None означає
    «токена немає» — з цим може щось зробити лише користувач.

    Замок від одночасних відміток бере викликач: демон тримає його ще й на
    час перечитування розкладу.
    """
    token = store.read_token()
    if token is None:
        return None
    client = Client(token=token)
    summary = run_tick(now, client, store, notifier, dry_run, min_age, refresh)
    _persist_token(store, client)
    return summary


# ─── ДЕМОН ──────────────────────────────────────────────────────────────────

# Відмітка має падати на ПОЧАТОК пари: у живому журналі студенти стоять купкою
# у перші хвилини заняття, і відмітка на 20-й хвилині виглядає в цьому списку
# окремо. Періодичним тіком у конкретну хвилину влучити не можна в принципі —
# Android затискає періодичні задачі JobScheduler до >=15 хвилин. Звідси
# довгоживучий процес, який спить саме до початку кожної пари.

DAEMON_CHUNK = timedelta(minutes=10)  # порція сну
DAEMON_RETRY = timedelta(minutes=10)  # пауза, коли розклад узяти не вдалось
DAEMON_AFTER_MARK = timedelta(minutes=1)
NEW_DAY_AT = timedelta(minutes=30)  # 00:30 — сервер на цей час уже має новий день
JITTER_MIN, JITTER_MAX = 30, 120
LOCK_WAIT = timedelta(minutes=1)  # скільки демон чекає на замок тіку
LOCK_POLL = 5.0


def lesson_target(lesson: Lesson) -> datetime:
    """Ціль демона: початок пари плюс зсув 30–120 секунд. Рівно `09:00:00`
    щодня місяцями — теж підпис, тому зсув потрібен; але він мусить бути
    однаковим між перечитуваннями розкладу, інакше ціль стрибала б і демон
    прокидався б не туди. Тому не `random.random()`, а детермінований вивід
    з дати й ключа пари — без збереження на диск."""
    shift = random.Random(f"{lesson.dateles}#{lesson.key}").randint(
        JITTER_MIN, JITTER_MAX
    )
    return lesson.window()[0] + timedelta(seconds=shift)


def daemon_targets(
    now: datetime, lessons: tuple[Lesson, ...], state: dict[str, Any]
) -> list[tuple[datetime, Lesson]]:
    """Цілі, які ще в майбутньому, від найближчої. Ціль у минулому демон не
    переслідує: пари, чий початок уже минув, — це робота сторожа (він бере
    їх через 5 хвилин після початку). Так демон зі сторожем не приходять до
    однієї пари в одну хвилину."""
    targets = [
        (lesson_target(lesson), lesson)
        for lesson in lessons
        if state.get(lesson.key, PENDING) not in (DONE, MISSED)
    ]
    return sorted(
        (item for item in targets if item[0] > now), key=lambda item: item[0]
    )


def _next_day_start(now: datetime) -> datetime:
    """00:30 наступного дня. Не 00:00: сервер має встигнути віддати новий
    день, і перші хвилини доби лишаються тіку."""
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
    return midnight + NEW_DAY_AT


def _sleep_until(
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
    until: datetime,
) -> None:
    """Сон порціями по 10 хвилин, із перечитуванням годинника щоразу. Одне
    `sleep()` на всю дистанцію не пережило б ні стрибка системного часу, ні
    зміни дати: телефон, який прокинувся з іншим часом, спав би далі до
    вчорашньої цілі."""
    while True:
        left = (until - clock()).total_seconds()
        if left <= 0:
            return
        sleeper(min(left, DAEMON_CHUNK.total_seconds()))


def _wait_lock(
    handle,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
    deadline: datetime,
) -> bool:
    """Замок з обмеженим очікуванням. Блокуючий `flock` зупинив би демона на
    невизначений час, якби чужий тік завис у мережі, — а демон, який не
    прокинувся, це пропущений семестр."""
    while True:
        if try_lock(handle):
            return True
        if clock() >= deadline:
            return False
        sleeper(LOCK_POLL)


def _daemon_lock(
    store: Store,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
):
    """Замок від одночасних відміток на час роботи демона з мережею і станом.
    Демон і сторож інакше прочитали б той самий стан і клікнули двічі."""
    handle = store.open_lock()
    if _wait_lock(handle, clock, sleeper, clock() + LOCK_WAIT):
        return handle
    handle.close()
    return None


def _daemon_schedule(
    store: Store, notifier: Notifier, now: datetime, complain: bool
) -> tuple[Lesson, ...] | None:
    """Розклад на сьогодні з мережі, а не з кешу: розклад живий — бувають
    заміни, скасування і додані пари, а демон планує на весь день уперед.
    Кеш заодно оновлюється, тому свіжий план дістається й сторожеві.
    None означає «не вдалось» (немає токена, токен мертвий або немає мережі)."""
    token = store.read_token()
    if token is None:
        return None
    client = Client(token=token)
    state = store.read_state()
    day = now.date()
    try:
        user, year, semestr = _obtain_access(client, store, state, notifier, now, day)
        if user is None:
            return None
        return _load_lessons(client, store, user, year, semestr, day, state)
    except NetworkError as error:
        if complain:
            store.log(f"{NO_NETWORK} (демон: {error})")
        return None
    finally:
        _persist_token(store, client)


def _daemon_plan(
    store: Store,
    notifier: Notifier,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
    complain: bool,
) -> tuple[Lesson, ...] | None:
    handle = _daemon_lock(store, clock, sleeper)
    if handle is None:
        return None
    try:
        return _daemon_schedule(store, notifier, clock(), complain)
    finally:
        handle.close()  # закриття дескриптора знімає flock


def _daemon_mark(
    store: Store,
    notifier: Notifier,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
) -> None:
    """Ціль настала. Розклад перечитується ще раз (пару могли скасувати, і
    тоді її не існує в свіжому плані), а відмічається тією самою логікою, що
    й у тіку: журнал перед відправкою, одна відправка, підтвердження."""
    handle = _daemon_lock(store, clock, sleeper)
    if handle is None:
        store.log("демон: замок тіку зайнятий — відмітку робить сторож")
        return
    try:
        summary = tick_once(store, clock(), notifier, refresh=True)
    finally:
        handle.close()
    if summary is not None:
        print(summary, flush=True)


def _daemon_cycle(
    store: Store,
    notifier: Notifier,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
    complain: bool,
) -> bool:
    """Один прохід циклу; повертає, чи вдалось узяти розклад. Кожна гілка або
    спить, або відмічається і спить — прокрутитись без сну цикл не може."""
    lessons = _daemon_plan(store, notifier, clock, sleeper, complain)
    if lessons is None:
        # Причина вже в лозі (або це мертвий токен, про який звітують раз на
        # добу). Сторож тим часом лишається запасним шляхом.
        _sleep_until(clock, sleeper, clock() + DAEMON_RETRY)
        return False
    targets = daemon_targets(clock(), lessons, store.read_state())
    if not targets:
        _sleep_until(clock, sleeper, _next_day_start(clock()))
        return True
    _sleep_until(clock, sleeper, targets[0][0])
    _daemon_mark(store, notifier, clock, sleeper)
    # Хвилина після відмітки: якби системний час зсунули назад, та сама ціль
    # знову виявилась би в майбутньому, і цикл крутився б без паузи.
    _sleep_until(clock, sleeper, clock() + DAEMON_AFTER_MARK)
    return True


def _daemon_loop(
    store: Store,
    notifier: Notifier,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
) -> None:
    # `complain` гасить повтори: доба без мережі інакше дала б 144 однакові
    # рядки в тому файлі, який README радить читати. Перша невдача серії йде
    # в лог, решта — ні, поки розклад знову не вдасться взяти.
    complain = True
    while True:
        try:
            complain = _daemon_cycle(store, notifier, clock, sleeper, complain)
        except Exception as error:  # свідомо широко: демон не має вмирати
            try:
                store.log(
                    f"демон: неочікувана помилка: {type(error).__name__}: {error}"
                )
            except OSError:
                pass
            _sleep_until(clock, sleeper, clock() + DAEMON_RETRY)


def run_daemon(
    store: Store,
    notifier: Notifier = notify,
    clock: Callable[[], datetime] = datetime.now,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Демон точного часу. `clock` і `sleeper` — параметри, щоб цикл можна
    було прогнати без очікування реального часу.

    `termux-wake-lock` демон не бере сам: це робить завантажувальний скрипт,
    щоб демон лишався запускним і на звичайному Linux.
    """
    try:
        handle = store.open_daemon_lock()
    except OSError as error:
        print(f"не вдалося відкрити файл-замок демона: {error}", file=sys.stderr)
        return 1
    if not try_lock(handle):
        # Замок зайнятий — демон уже працює. Виходимо з нулем: для сторожа це
        # нормальний стан речей, а не поламка.
        handle.close()
        print("демон уже працює")
        return 0
    # Замок тримається до смерті процесу: `handle` живе до кінця функції, а
    # flock звільняється смертю процесу і без `close()`.
    store.log("демон запущено")
    if store.read_token() is None:
        store.log("демон: немає токена — спочатку `python3 tt_checkin.py login`")
    try:
        _daemon_loop(store, notifier, clock, sleeper)
    except KeyboardInterrupt:
        store.log("демон зупинено")
    return 0


# ─── CLI ────────────────────────────────────────────────────────────────────

LOGIN_HINT = "логін — це корпоративна пошта, наприклад ipz-23d-123@snu.edu.ua"


CREDENTIALS_SAVED = (
    "креди збережено в ~/.tt-checkin/credentials (0600): скрипт зможе "
    "перелогінитись сам. Токен можна відкликати виходом з акаунта, пароль — ні"
)


def cmd_login(store: Store, remember: bool = False) -> int:
    print(LOGIN_HINT)
    try:
        login = input("логін: ").strip()
        password = getpass("пароль: ")
    except (EOFError, KeyboardInterrupt):
        # Ctrl-C у промпті — це відмова, а не аварія: без цього користувач
        # бачить трейсбек замість повідомлення.
        print("\nскасовано", file=sys.stderr)
        return 1

    client = Client()
    try:
        try:
            client.login(login, password)
        except NetworkError as error:
            print(f"мережа: {error}", file=sys.stderr)
            return 1

        if not client.token:
            # Сервер не поставив cookie. Це може бути невірний пароль, а може —
            # зміна контракту входу; окреме повідомлення потрібне, щоб
            # користувач не шукав проблему в паролі, коли справа в іншому.
            print(
                "сервер не віддав cookie tt_tokin — або логін і пароль невірні, "
                "або змінився контракт входу",
                file=sys.stderr,
            )
            return 1

        for year, semestr in candidates(date.today()):
            try:
                user = client.check_access(year, semestr)
            except NetworkError as error:
                print(f"мережа: {error}", file=sys.stderr)
                return 1
            if user is None:
                continue
            store.write_token(client.token)
            if remember:
                # Креди пишемо тільки після підтвердженого входу: збережений
                # хибний пароль автоматика потім била б об акаунт.
                try:
                    store.write_credentials(login, password)
                except OSError as error:
                    print(
                        f"токен збережено, але креди не записались: {error}",
                        file=sys.stderr,
                    )
                else:
                    print(CREDENTIALS_SAVED)
            store.log(f"успішний вхід: {user.group}, період {year} семестр {semestr}")
            print(
                f"готово: {user.fio}, група {user.group}, "
                f"період {year} семестр {semestr}"
            )
            return 0
    finally:
        del password

    print(
        "токен отримано, але жоден навчальний період не підійшов",
        file=sys.stderr,
    )
    return 1


def cmd_tick(
    store: Store, now: datetime, dry_run: bool, min_age: timedelta = timedelta(0)
) -> int:
    if store.read_token() is None:
        print("немає токена — спочатку `python3 tt_checkin.py login`", file=sys.stderr)
        return 1

    try:
        handle = store.open_lock()
    except OSError as error:
        print(f"не вдалося відкрити файл-замок: {error}", file=sys.stderr)
        return 1

    try:
        if not try_lock(handle):
            # Два тіки, що перетнулись (а README сам радить запускати `tick`
            # руками), обидва прочитали б той самий стан і обидва відправили
            # б — це «Завантаження» 2. Той самий замок бере й демон.
            store.log("тік уже виконується — вихід")
            return 0

        summary = tick_once(store, now, dry_run=dry_run, min_age=min_age)
        if summary is None:
            print(
                "немає токена — спочатку `python3 tt_checkin.py login`",
                file=sys.stderr,
            )
            return 1
        print(summary)
        return 0
    finally:
        handle.close()  # закриття дескриптора знімає flock


def cmd_daemon(store: Store, check: bool) -> int:
    if not check:
        return run_daemon(store)
    # «Чи живий демон» = спроба взяти його замок неблокуюче. Вдалося — демон
    # мертвий, і замок треба одразу відпустити (закриттям дескриптора), щоб
    # справжній демон міг захопити його сам. PID-файли не потрібні: flock
    # звільняється смертю процесу.
    try:
        handle = store.open_daemon_lock()
    except OSError as error:
        print(f"не вдалося відкрити файл-замок демона: {error}", file=sys.stderr)
        return 1  # вважаємо мертвим: краще підняти зайвий раз
    try:
        if try_lock(handle):
            print("демон не працює")
            return 1
        print("демон живий")
        return 0
    finally:
        handle.close()


def cmd_status(store: Store, day: date) -> int:
    lessons = store.read_plan(day)
    if lessons is None:
        print(f"розклад на {day.isoformat()} ще не забирався")
        return 0
    if not lessons:
        # Порожній план кешується, тому «забрали, пар нуль» відрізняється від
        # «не забирали» самим фактом наявності файла.
        print(f"розклад на {day.isoformat()} забрано: пар цього дня немає")
        return 0

    state = store.read_state()
    print(f"розклад на {day.isoformat()}:")
    for lesson in lessons:
        current = state.get(lesson.key, PENDING)
        print(f"  {lesson.num_lesson}. {lesson.timebeg}–{lesson.timeend} "
              f"{lesson.discipline} [{lesson.kind}] — {current}")
    missed = [
        lesson.num_lesson for lesson in lessons if state.get(lesson.key) == MISSED
    ]
    if missed:
        # Окремий рядок, а не лише стан у списку: це саме той рядок, який
        # користувач мусить побачити — пара, яку автоматика не змогла
        # відмітити, і вікно вже закрите.
        print(f"автоматика не змогла відмітити: пари {missed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tt_checkin.py", description="автоматична відмітка відвідування"
    )
    parser.add_argument("command", choices=["login", "tick", "status", "daemon"])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показати, що зробив би тік, і нічого не відправляти",
    )
    parser.add_argument(
        "--remember",
        action="store_true",
        help="з `login`: зберегти логін і пароль у ~/.tt-checkin/credentials, "
        "щоб скрипт міг перелогінитись сам, коли токен помре",
    )
    parser.add_argument(
        "--late-only",
        action="store_true",
        help="з `tick`: тільки пари, чий початок минув понад 5 хвилин "
        "(запасний шлях сторожа — першу спробу лишаємо демону)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="з `daemon`: чи живий демон (код 0 — живий, 1 — ні), і нічого "
        "не запускати",
    )
    parser.add_argument(
        "--at",
        metavar="YYYY-MM-DD HH:MM",
        help="підставити інший час замість поточного; з `tick` — тільки разом "
        "із --dry-run (для перевірки логіки вікон)",
    )
    args = parser.parse_args(argv)

    now = datetime.now()
    if args.at:
        try:
            now = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
        except ValueError:
            print("--at очікує формат 'YYYY-MM-DD HH:MM'", file=sys.stderr)
            return 2
        if args.command == "tick" and not args.dry_run:
            # Сервер не перевіряє вікно заняття, тому `tick --at` без
            # --dry-run відмітився б задніми числами за іншу дату — з
            # реальним часом у колонці, яку бачить викладач.
            print(
                "--at призначений лише для репетиції: додай --dry-run",
                file=sys.stderr,
            )
            return 2

    store = Store()
    if args.command == "login":
        return cmd_login(store, args.remember)
    if args.command == "daemon":
        return cmd_daemon(store, args.check)
    if args.command == "tick":
        min_age = LATE_FALLBACK if args.late_only else timedelta(0)
        return cmd_tick(store, now, args.dry_run, min_age)
    return cmd_status(store, now.date())


if __name__ == "__main__":
    raise SystemExit(main())
