# tt-checkin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Один самодостатній файл `tt_checkin.py`, який автоматично відмічає
відвідування занять у `timetable.lond.lg.ua` у час відповідної пари і підтверджує
кожну відмітку через журнал. Користувач копіює цей файл на телефон.

**Architecture:** «План-і-тік»: розклад забирається раз на добу і кешується на диск;
`termux-job-scheduler` будить `tick` раз на 30 хвилин, тік читає кешований план
локально і йде в мережу лише коли пора відмічатися.

**Tech Stack:** Python 3.10+, **тільки стандартна бібліотека** (`urllib.request`,
`html.parser`, `json`, `argparse`, `getpass`), Termux API для нотифікацій.

**Spec:** `docs/superpowers/specs/2026-09-08-tt-checkin-design.md`

## Відхилення від спеки (узгоджені з користувачем)

- **Один файл замість п'яти модулів** (§5 спеки). Причина: скинути на телефон один
  файл простіше, ніж каталог із пакетом. Межі відповідальності зберігаються як
  секції файлу, розділені банерними комментарями.
- **Тільки стандартна бібліотека** замість `requests` + `beautifulsoup4`. Причина:
  на телефоні достатньо `pkg install python`, жодного `pip`; локально не впираємось
  в externally-managed environment.
- **Юніт-тестів немає, комітів немає.** Кожна задача завершується реальним прогоном,
  що друкує справжні значення. Замість тестів — `--dry-run` і `--at` у CLI.

## Global Constraints

- Python `>=3.10`. Жодних сторонніх залежностей — тільки stdlib.
- Файл: `tt_checkin.py` у корені проєкту. Виконуваний як `python3 tt_checkin.py <команда>`.
- Базовий URL: `https://timetable.lond.lg.ua/`. Скрипти: `php/Avtorization.php` (JSON),
  `php/getQuery.php` (`application/x-www-form-urlencoded`).
- Час — наївний локальний. Формат дат `DD.MM.YYYY`, часу `HH:MM:SS`.
- `year` — рядок `"2026/2027"`, `semestr` — число.
- Каталог стану `~/.tt-checkin` з правами `0700`, файли в ньому `0600`.
- **Пароль ніколи не пишеться на диск. Токен ніколи не потрапляє в лог або stdout.**
- Поля `lesson` і `type` у запиті відмітки несуть `Вид` і `№ тижня` — це фактична
  (помилкова) поведінка сайту, її треба повторювати точно.
- Секції файлу йдуть у порядку задач і позначаються банером:
  `# ─── НАЗВА СЕКЦІЇ ───────────────────────────────────────────────`

---

### Task 1: Каркас файлу, модель і чиста логіка

**Files:**
- Create: `tt_checkin.py` (секції `КОНСТАНТИ`, `МОДЕЛЬ`, `ПЕРІОД`, `ЛОГІКА`)

**Interfaces:**
- Consumes: нічого.
- Produces: `BASE_URL`, `DATE_FMT`, `TIME_FMT`, `MAX_ATTEMPTS`;
  `Lesson` (поля `id_lesson`, `num_lesson`, `kind`, `week_numbers`, `teacher`,
  `id_fio`, `group`, `dateles`, `timebeg`, `timeend`, `discipline`; властивість
  `key`; метод `window()`); `User(fio, id_user, id_fio, group, id_group)`;
  `DaySchedule(weekday, week_number, lessons)`;
  `academic_period(day) -> tuple[str, int]`; `candidates(day) -> list[tuple[str, int]]`;
  `PENDING`/`SENT`/`DONE` (рядкові константи станів);
  `window_is_open(now, lesson) -> bool`;
  `plan_tick(now, lessons, state) -> tuple[tuple[Lesson, ...], tuple[Lesson, ...]]`
  (перший елемент — на перевірку, другий — на відмітку).

- [ ] **Step 1: Створити файл із заголовком і константами**

```python
#!/usr/bin/env python3
"""Автоматична відмітка відвідування в TimeTable SNU.

Один файл, тільки стандартна бібліотека. Копіюється на телефон як є.
Дизайн і причини рішень: docs/superpowers/specs/2026-09-08-tt-checkin-design.md

    python3 tt_checkin.py login
    python3 tt_checkin.py tick
    python3 tt_checkin.py status
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime
from getpass import getpass
from html.parser import HTMLParser
from pathlib import Path

# ─── КОНСТАНТИ ──────────────────────────────────────────────────────────────

BASE_URL = "https://timetable.lond.lg.ua/"
AVTORIZATION = "php/Avtorization.php"
GET_QUERY = "php/getQuery.php"

DATE_FMT = "%d.%m.%Y"
TIME_FMT = "%H:%M:%S"

TIMEOUT = 30
MAX_ATTEMPTS = 3
USER_AGENT = "Mozilla/5.0 (Linux; Android) tt-checkin"

STATE_ROOT = Path.home() / ".tt-checkin"

PENDING = "pending"
SENT = "sent"
DONE = "done"
```

- [ ] **Step 2: Додати секцію МОДЕЛЬ**

```python
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
```

- [ ] **Step 3: Додати секцію ПЕРІОД**

```python
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
```

- [ ] **Step 4: Додати секцію ЛОГІКА**

```python
# ─── ЛОГІКА ─────────────────────────────────────────────────────────────────


def window_is_open(now: datetime, lesson: Lesson) -> bool:
    """Початок включно, кінець виключно."""
    beg, end = lesson.window()
    return beg <= now < end


def plan_tick(
    now: datetime,
    lessons: tuple[Lesson, ...],
    state: dict[str, str],
) -> tuple[tuple[Lesson, ...], tuple[Lesson, ...]]:
    """Що робити на цьому тіку: (перевірити журнал, відмітити).

    `done` не торкаємось узагалі. `sent` завжди йде на перевірку журналом, а не
    на повторну відправку — щоб не накрутити лічильник «Завантаження».
    """
    to_verify: list[Lesson] = []
    to_check_in: list[Lesson] = []

    for lesson in lessons:
        current = state.get(lesson.key, PENDING)
        if current == DONE:
            continue
        if current == SENT:
            to_verify.append(lesson)
            continue
        if window_is_open(now, lesson):
            to_check_in.append(lesson)

    return tuple(to_verify), tuple(to_check_in)
```

- [ ] **Step 5: Прогнати логіку на справжніх значеннях**

```bash
python3 - <<'PY'
import importlib.util, sys
from datetime import date, datetime
spec = importlib.util.spec_from_file_location("tt", "tt_checkin.py")
tt = importlib.util.module_from_spec(spec); spec.loader.exec_module(tt)

print("період 08.09.2026:", tt.academic_period(date(2026, 9, 8)))
print("період 15.03.2027:", tt.academic_period(date(2027, 3, 15)))
print("кандидати:", tt.candidates(date(2026, 9, 8)))

lesson = tt.Lesson("3570", "1", "ЛК", "2, 4", "доцент\nТест", "647",
                   "БЦІ-26д-2", "08.09.2026", "09:00:00", "10:20:00", "Вища математика")
print("ключ:", lesson.key)
print("вікно:", lesson.window())
for hhmm in ("08:59", "09:00", "10:19", "10:20"):
    now = datetime.strptime("08.09.2026 " + hhmm, "%d.%m.%Y %H:%M")
    print(f"  {hhmm} відкрите:", tt.window_is_open(now, lesson))

now = datetime(2026, 9, 8, 9, 5)
print("pending:", tt.plan_tick(now, (lesson,), {}))
print("done:   ", tt.plan_tick(now, (lesson,), {lesson.key: tt.DONE}))
print("sent:   ", tt.plan_tick(now, (lesson,), {lesson.key: tt.SENT}))
PY
```

Очікується: період `('2026/2027', 1)` і `('2026/2027', 2)`; ключ
`08.09.2026#3570#1`; вікно відкрите тілько для `09:00` і `10:19`; `pending`
дає пару в другому кортежі, `done` — два порожні кортежі, `sent` — у першому.

---

### Task 2: Парсинг відповідей

**Files:**
- Modify: `tt_checkin.py` (додати секцію `ПАРСИНГ`)
- Create: `samples/schedule_day.txt`
- Create: `samples/journal.txt`

**Interfaces:**
- Consumes: `Lesson`, `DaySchedule` з Task 1.
- Produces: `parse_day(text) -> DaySchedule`;
  `parse_journal(text) -> tuple[dict[str, str], ...]` (ключі `num`, `fio`,
  `status`, `group`, `time`, `loads`);
  `registration_time(rows, fio) -> str | None`.

- [ ] **Step 1: Зберегти зразки справжніх відповідей**

`samples/schedule_day.txt` — формат `день|тиждень|HTML рядків`, структура знята
з живої відповіді, ПІП викладача замінено на тестове (один рядок у файлі):

```
ПОНЕДІЛОК|2|<tr class="tr1" data-id_lesson="3570" data-group="БЦІ-26д-2" data-numlesson="1" data-lesson="Вища математика. Частина 1." data-id_fio="647" data-dateles="08.09.2026" data-timebeg="09:00:00" data-timeend="10:20:00" data-items=""><td>1</td><td>09:00-<br>10:20</td><td><span class="lk">ЛК</span></td><td><button onclick="Modul_TT.loadZoom('MEET_URL')"><img src="img/Teams.png"></button>Вища математика. Частина 1.</td><td>-</td><td><span class="week">2</span>, 4, 6, 8, 10, 12</td><td><i>доцент</i><br>Тестовий Викладач Іванович</td><td>БЦІ-26д-2</td><td>401(ІП)</td></tr><tr class="tr2" data-id_lesson="3571" data-group="БЦІ-26д-2" data-numlesson="2" data-lesson="Хімія" data-id_fio="134" data-dateles="08.09.2026" data-timebeg="10:35:00" data-timeend="11:55:00" data-items=""><td>2</td><td>10:35-<br>11:55</td><td><span class="lk">ПЗ</span></td><td><button onclick="Modul_TT.loadZoom('MEET_URL')"><img src="img/Teams.png"></button>Хімія</td><td>-</td><td><span class="week">2</span>, 4, 6</td><td><i>доцент</i><br>Інший Викладач Петрович</td><td>БЦІ-26д-2</td><td></td></tr>
```

`samples/journal.txt` — рядки журналу лежать у нульовій частині відповіді:

```
<tr class="tr1"><td class="tabPS">1</td><td class="tabPS">Тестовий Викладач Іванович</td><td class="tabPS">викладач</td><td class="tabPS">—</td><td class="tabPS">08:55:01</td><td class="tabPS">1</td></tr><tr class="tr2"><td class="tabPS">2</td><td class="tabPS">Студент Перший Петрович</td><td class="tabPS">студент</td><td class="tabPS">БЦІ-26д-2</td><td class="tabPS">09:03:12</td><td class="tabPS">1</td></tr><tr class="tr1"><td class="tabPS">3</td><td class="tabPS">Тестовий   Студент Іванович</td><td class="tabPS">студент</td><td class="tabPS">БЦІ-26д-2</td><td class="tabPS">09:04:47</td><td class="tabPS">2</td></tr>|
```

- [ ] **Step 2: Додати секцію ПАРСИНГ**

```python
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
    """Збирає рядки таблиці: атрибути <tr> плюс текст кожної <td>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[dict[str, str], list[str]]] = []
        self._attrs: dict[str, str] | None = None
        self._cells: list[str] | None = None
        self._buf: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._attrs = {name: (value or "") for name, value in attrs}
            self._cells = []
        elif tag == "td" and self._cells is not None:
            self._buf = []
        elif tag == "br" and self._buf is not None:
            self._buf.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "br" and self._buf is not None:
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._buf is not None and self._cells is not None:
            self._cells.append(_clean("".join(self._buf)))
            self._buf = None
        elif tag == "tr" and self._cells is not None:
            self.rows.append((self._attrs or {}, self._cells))
            self._attrs, self._cells, self._buf = None, None, None

    def handle_data(self, data: str) -> None:
        if self._buf is not None:
            self._buf.append(data)


def _parse_rows(html: str) -> list[tuple[dict[str, str], list[str]]]:
    parser = _RowParser()
    parser.feed(html)
    parser.close()
    return parser.rows


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
    for attrs, cells in _parse_rows(rows_html):
        if "data-id_lesson" not in attrs or len(cells) < 7:
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
                dateles=attrs.get("data-dateles", ""),
                timebeg=attrs.get("data-timebeg", ""),
                timeend=attrs.get("data-timeend", ""),
                discipline=attrs.get("data-lesson", ""),
            )
        )
    return DaySchedule(weekday.strip(), week_number, tuple(lessons))


JOURNAL_COLUMNS = ("num", "fio", "status", "group", "time", "loads")


def parse_journal(text: str) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    for _attrs, cells in _parse_rows(text.split("|")[0]):
        if len(cells) < len(JOURNAL_COLUMNS):
            continue
        rows.append(dict(zip(JOURNAL_COLUMNS, cells)))
    return tuple(rows)


def registration_time(rows: tuple[dict[str, str], ...], fio: str) -> str | None:
    """Час нашої відмітки в журналі, або None, якщо нас там немає.
    Порівняння точне (не підрядок), але терпиме до регістру і зайвих пробілів."""
    wanted = " ".join(fio.split()).casefold()
    for row in rows:
        if " ".join(row["fio"].split()).casefold() == wanted:
            return row["time"]
    return None
```

- [ ] **Step 3: Прогнати парсери на зразках**

```bash
python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("tt", "tt_checkin.py")
tt = importlib.util.module_from_spec(spec); spec.loader.exec_module(tt)

day = tt.parse_day(open("samples/schedule_day.txt", encoding="utf-8").read())
print("день:", day.weekday, "тиждень:", day.week_number, "пар:", len(day.lessons))
for lesson in day.lessons:
    print(f"  {lesson.num_lesson} {lesson.timebeg}-{lesson.timeend} "
          f"kind={lesson.kind!r} weeks={lesson.week_numbers!r} "
          f"teacher={lesson.teacher!r} discipline={lesson.discipline!r}")

print("порожній день:", tt.parse_day("СУБОТА|0|"))
print("сміття:", tt.parse_day("<br /><b>Warning</b>: халепа").lessons)
pipe = open("samples/schedule_day.txt", encoding="utf-8").read().replace("401(ІП)", "401|ІП")
print("| всередині HTML, пар:", len(tt.parse_day(pipe).lessons))

rows = tt.parse_journal(open("samples/journal.txt", encoding="utf-8").read())
print("рядків журналу:", len(rows), "| перший:", rows[0])
print("свій час:", tt.registration_time(rows, "Студент Перший Петрович"))
print("зайві пробіли:", tt.registration_time(rows, "Тестовий Студент Іванович"))
print("інший регістр:", tt.registration_time(rows, "студент перший петрович"))
print("підрядок не матчиться:", tt.registration_time(rows, "Студент"))
print("відсутній:", tt.registration_time(rows, "Ніхто Ніякий Ніякович"))
PY
```

Очікується: 2 пари; `kind='ЛК'` і `'ПЗ'`; `weeks='2, 4, 6, 8, 10, 12'`;
`teacher='доцент\nТестовий Викладач Іванович'`; порожній день — нуль пар;
сміття — `()`; `|` всередині HTML не ламає розбір (усе одно 2 пари);
3 рядки журналу; свій час `09:03:12`; зайві пробіли й регістр знаходяться;
`'Студент'` і відсутній — `None`.

---

### Task 3: HTTP-клієнт

**Files:**
- Modify: `tt_checkin.py` (додати секцію `HTTP`)

**Interfaces:**
- Consumes: `Lesson`, `User`, константи з Task 1.
- Produces: `NetworkError`; `Client(token=None, base_url=BASE_URL)` з атрибутом
  `token` і методами `login(login, password) -> None`,
  `check_access(year, semestr) -> User | None`,
  `day_schedule(user, year, semestr, day) -> str`,
  `check_in(user, lesson) -> str`, `journal(user, lesson) -> str`.

- [ ] **Step 1: Додати секцію HTTP**

```python
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
        """Логін віддає tt_tokin у Set-Cookie."""
        for header in response.headers.get_all("Set-Cookie") or []:
            for chunk in header.split(";"):
                name, _, value = chunk.strip().partition("=")
                if name == "tt_tokin" and value:
                    self.token = value

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
```

- [ ] **Step 2: Перевірити кодування payload без відправки**

```bash
python3 - <<'PY'
import importlib.util, urllib.parse
spec = importlib.util.spec_from_file_location("tt", "tt_checkin.py")
tt = importlib.util.module_from_spec(spec); spec.loader.exec_module(tt)

user = tt.User("Тестовий Студент Іванович", "623", "0", "БЦІ-26д-2", "42")
lesson = tt.Lesson("3570", "1", "ЛК", "2, 4, 6", "доцент\nТест Викладач", "647",
                   "БЦІ-26д-2", "08.09.2026", "09:00:00", "10:20:00", "Вища математика")

captured = {}
client = tt.Client(token="ТОКЕН")
client._post = lambda path, body, ct: captured.update(path=path, body=body, ct=ct) or "{}"

client.check_in(user, lesson)
print("шлях:", captured["path"], "| тип:", captured["ct"])
print("payload:", urllib.parse.parse_qs(captured["body"].decode()))

client.journal(user, lesson)
fields = urllib.parse.parse_qs(captured["body"].decode())
print("журнал має date:", "date" in fields, "| не має dateles:", "dateles" not in fields)
PY
```

Очікується: шлях `php/getQuery.php`, тип `application/x-www-form-urlencoded`,
у payload відмітки `lesson=['ЛК']` і `type=['2, 4, 6']` (саме переплутані
значення), кирилиця розкодовується правильно; запит журналу має `date` і не
має `dateles`.

- [ ] **Step 3: Перевірити живий запит без токена**

Це читання без побічних ефектів і без будь-яких кредів: сервер має відповісти
не-`ok`, а клієнт — повернути `None`, не впавши.

```bash
python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("tt", "tt_checkin.py")
tt = importlib.util.module_from_spec(spec); spec.loader.exec_module(tt)
print("без токена check_access →", tt.Client().check_access("2026/2027", 1))
PY
```

Очікується: `None` (а не трейсбек і не `User`). Якщо натомість
`NetworkError` — сервер недосяжний, потрібен VPN або мережа.

---

### Task 4: Сховище стану і оркестрація тіку

**Files:**
- Modify: `tt_checkin.py` (додати секції `СХОВИЩЕ` і `ОРКЕСТРАЦІЯ`)

**Interfaces:**
- Consumes: усе з Task 1–3.
- Produces: `Store(root=STATE_ROOT)` з методами `read_token`, `write_token`,
  `read_state`, `write_state`, `read_plan(day)`, `write_plan(day, lessons)`,
  `log(message)` і властивістю `log_path`;
  `notify(title, message) -> None`;
  `run_tick(now, client, store, notifier=notify, dry_run=False) -> str`.

- [ ] **Step 1: Додати секцію СХОВИЩЕ**

```python
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
        self._ensure_root()
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

    def read_token(self) -> str | None:
        path = self.root / "token"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def write_token(self, value: str) -> None:
        self._write_private("token", value)

    def read_state(self) -> dict[str, str]:
        path = self.root / "state.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def write_state(self, state: dict[str, str]) -> None:
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

    @property
    def log_path(self) -> Path:
        return self.root / "log.txt"

    def log(self, message: str) -> None:
        self._ensure_root()
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
        self.log_path.chmod(0o600)
```

- [ ] **Step 2: Додати секцію ОРКЕСТРАЦІЯ**

```python
# ─── ОРКЕСТРАЦІЯ ────────────────────────────────────────────────────────────

DEAD_TOKEN = "токен не працює — запусти `python3 tt_checkin.py login`"
NO_NETWORK = "немає мережі — наступний тік повторить"


def notify(title: str, message: str) -> None:
    """Нотифікація через Termux API. Не на телефоні — тихо нічого не робимо."""
    try:
        subprocess.run(
            ["termux-notification", "--title", title, "--content", message],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass


def _resolve_access(client: Client, day: date):
    for year, semestr in candidates(day):
        user = client.check_access(year, semestr)
        if user is not None:
            return user, year, semestr
    return None, None, None


def _load_lessons(client, store, user, year, semestr, day) -> tuple[Lesson, ...]:
    cached = store.read_plan(day)
    if cached is not None:
        return cached
    lessons = parse_day(client.day_schedule(user, year, semestr, day)).lessons
    store.write_plan(day, lessons)
    return lessons


def _confirm(client: Client, user: User, lesson: Lesson) -> str | None:
    return registration_time(parse_journal(client.journal(user, lesson)), user.fio)


def _check_in(client: Client, user: User, lesson: Lesson) -> str | None:
    """Відмітка з підтвердженням. Не більше MAX_ATTEMPTS спроб за тік, щоб
    зламаний сервер не накрутив «Завантаження»."""
    for _ in range(MAX_ATTEMPTS):
        client.check_in(user, lesson)
        confirmed = _confirm(client, user, lesson)
        if confirmed:
            return confirmed
    return None


def run_tick(now, client, store, notifier=notify, dry_run: bool = False) -> str:
    day = now.date()

    try:
        user, year, semestr = _resolve_access(client, day)
    except NetworkError as error:
        store.log(f"{NO_NETWORK} ({error})")
        return NO_NETWORK

    if user is None:
        store.log(DEAD_TOKEN)
        notifier("tt-checkin", DEAD_TOKEN)
        return DEAD_TOKEN

    try:
        lessons = _load_lessons(client, store, user, year, semestr, day)
    except NetworkError as error:
        store.log(f"{NO_NETWORK} ({error})")
        return NO_NETWORK

    state = store.read_state()
    to_verify, to_check_in = plan_tick(now, lessons, state)

    if dry_run:
        summary = (
            f"[dry-run] пар: {len(lessons)}; відмітив би: "
            f"{[item.num_lesson for item in to_check_in] or '—'}; "
            f"перевірив би: {[item.num_lesson for item in to_verify] or '—'}"
        )
        store.log(summary)
        return summary

    done: list[str] = []
    stuck: list[str] = []

    for lesson in to_verify:
        try:
            confirmed = _confirm(client, user, lesson)
            if not confirmed and window_is_open(now, lesson):
                # У журналі нас немає, а вікно ще відкрите — значить попередня
                # відправка не дійшла. Надсилаємо ще раз.
                confirmed = _check_in(client, user, lesson)
        except NetworkError as error:
            store.log(f"пара {lesson.num_lesson}: мережа впала ({error})")
            continue
        if confirmed:
            state[lesson.key] = DONE
            done.append(f"{lesson.num_lesson}:{confirmed}")
        else:
            stuck.append(lesson.num_lesson)

    for lesson in to_check_in:
        try:
            confirmed = _check_in(client, user, lesson)
        except NetworkError as error:
            # Невідомо, чи запит дійшов, тому `sent`: наступний тік спершу
            # подивиться в журнал, а не надішле вдруге.
            state[lesson.key] = SENT
            store.log(f"пара {lesson.num_lesson}: мережа впала ({error})")
            continue
        if confirmed:
            state[lesson.key] = DONE
            done.append(f"{lesson.num_lesson}:{confirmed}")
        else:
            state[lesson.key] = SENT
            stuck.append(lesson.num_lesson)

    store.write_state(state)

    summary = f"пар: {len(lessons)}; підтверджено: {done or '—'}"
    if stuck:
        summary += f"; без підтвердження: {stuck}"
        notifier("tt-checkin", f"відмітка без підтвердження: пари {stuck}")
    store.log(summary)
    return summary
```

- [ ] **Step 3: Прогнати оркестрацію на підставному клієнті**

Мережі тут немає — клієнт підставний, сховище у тимчасовому каталозі.

```bash
python3 - <<'PY'
import importlib.util, tempfile
from datetime import date, datetime
from pathlib import Path
spec = importlib.util.spec_from_file_location("tt", "tt_checkin.py")
tt = importlib.util.module_from_spec(spec); spec.loader.exec_module(tt)

USER = tt.User("Тестовий Студент Іванович", "623", "0", "БЦІ-26д-2", "42")
SCHEDULE = open("samples/schedule_day.txt", encoding="utf-8").read()

def journal_for(fio):
    return (f'<tr><td>1</td><td>{fio}</td><td>студент</td>'
            f'<td>БЦІ-26д-2</td><td>09:06:11</td><td>1</td></tr>|')

class Fake:
    def __init__(self, who=USER.fio, confirm_after=1):
        self.who, self.confirm_after = who, confirm_after
        self.check_ins, self.journals, self.schedules = [], 0, 0
    def check_access(self, year, semestr): return USER
    def day_schedule(self, *a): self.schedules += 1; return SCHEDULE
    def check_in(self, user, lesson): self.check_ins.append(lesson.num_lesson); return "ok"
    def journal(self, user, lesson):
        self.journals += 1
        return journal_for(self.who if self.journals >= self.confirm_after else "Хтось Інший")

def fresh():
    return tt.Store(Path(tempfile.mkdtemp()) / ".tt-checkin")

now = datetime(2026, 9, 8, 9, 5)   # всередині першої пари
notes = []
note = lambda t, m: notes.append(m)

store, client = fresh(), Fake()
print("1 щасливий шлях:", tt.run_tick(now, client, store, note))
print("   відмічено:", client.check_ins, "| стан:", list(store.read_state().values()))

store, client = fresh(), Fake(who="Хтось Інший")
print("2 без підтвердження:", tt.run_tick(now, client, store, note))
print("   спроб:", len(client.check_ins), "| стан:", list(store.read_state().values()),
      "| нотифікацій:", len(notes))

store, client = fresh(), Fake(confirm_after=2)
print("3 ретрай до успіху:", tt.run_tick(now, client, store, note))
print("   спроб:", len(client.check_ins))

store, client = fresh(), Fake()
tt.run_tick(now, client, store, note); tt.run_tick(now, client, store, note)
print("4 розклад тягнеться раз:", client.schedules, "| відміток усього:", len(client.check_ins))

store, client = fresh(), Fake()
print("5 поза вікном:", tt.run_tick(datetime(2026, 9, 8, 8, 30), client, store, note))
print("   відмічено:", client.check_ins)

store, client = fresh(), Fake()
print("6 dry-run:", tt.run_tick(now, client, store, note, dry_run=True))
print("   відмічено:", client.check_ins, "| стан:", store.read_state())

class Offline(Fake):
    def check_access(self, year, semestr): raise tt.NetworkError("немає мережі")
store, client = fresh(), Offline()
print("7 мережа впала:", tt.run_tick(now, client, store, note))

store, client = fresh(), Fake()
store.write_state({"08.09.2026#3570#1": tt.SENT})
store.write_plan(date(2026, 9, 8), tt.parse_day(SCHEDULE).lessons)
print("8 sent підтверджується:", tt.run_tick(now, client, store, note))
print("   повторних відміток:", client.check_ins)
PY
```

Очікується: (1) одна відмітка, стан `done`; (2) три спроби, стан `sent`, одна
нотифікація; (3) дві спроби і `done`; (4) розклад узятий один раз, відмітка
одна; (5) поза вікном нічого не відмічено; (6) dry-run нічого не відправив і
стан не записав; (7) відповідь про мережу без трейсбека; (8) пара в `sent`
підтверджена журналом **без** повторної відправки.

---

### Task 5: CLI, скрипт для job-scheduler, README

**Files:**
- Modify: `tt_checkin.py` (додати секцію `CLI`)
- Create: `tick.sh`
- Create: `README.md`

**Interfaces:**
- Consumes: усе з Task 1–4.
- Produces: команди `login`, `tick [--dry-run] [--at "YYYY-MM-DD HH:MM"]`,
  `status [--at ...]`; функція `main(argv=None) -> int`.

- [ ] **Step 1: Додати секцію CLI**

```python
# ─── CLI ────────────────────────────────────────────────────────────────────

LOGIN_HINT = "логін — це корпоративна пошта, наприклад ipz-23d-123@snu.edu.ua"


def cmd_login(store: Store) -> int:
    print(LOGIN_HINT)
    login = input("логін: ").strip()
    password = getpass("пароль: ")

    client = Client()
    try:
        client.login(login, password)
    except NetworkError as error:
        print(f"мережа: {error}", file=sys.stderr)
        return 1
    finally:
        del password

    for year, semestr in candidates(date.today()):
        try:
            user = client.check_access(year, semestr)
        except NetworkError as error:
            print(f"мережа: {error}", file=sys.stderr)
            return 1
        if user is None:
            continue
        if not client.token:
            print("вхід пройшов, але сервер не віддав cookie tt_tokin", file=sys.stderr)
            return 1
        store.write_token(client.token)
        store.log(f"успішний вхід: {user.group}, період {year} семестр {semestr}")
        print(f"готово: {user.fio}, група {user.group}, "
              f"період {year} семестр {semestr}")
        return 0

    print("вхід не вдався: перевір логін і пароль", file=sys.stderr)
    return 1


def cmd_tick(store: Store, now: datetime, dry_run: bool) -> int:
    token = store.read_token()
    if token is None:
        print("немає токена — спочатку `python3 tt_checkin.py login`", file=sys.stderr)
        return 1
    print(run_tick(now, Client(token=token), store, dry_run=dry_run))
    return 0


def cmd_status(store: Store, day: date) -> int:
    lessons = store.read_plan(day)
    if not lessons:
        print(f"плану на цей день немає: {day.isoformat()}")
        return 0

    state = store.read_state()
    print(f"розклад на {day.isoformat()}:")
    for lesson in lessons:
        current = state.get(lesson.key, PENDING)
        print(f"  {lesson.num_lesson}. {lesson.timebeg}–{lesson.timeend} "
              f"{lesson.discipline} [{lesson.kind}] — {current}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tt_checkin.py", description="автоматична відмітка відвідування"
    )
    parser.add_argument("command", choices=["login", "tick", "status"])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показати, що зробив би тік, і нічого не відправляти",
    )
    parser.add_argument(
        "--at",
        metavar="YYYY-MM-DD HH:MM",
        help="підставити інший час замість поточного (для перевірки логіки вікон)",
    )
    args = parser.parse_args(argv)

    now = datetime.now()
    if args.at:
        try:
            now = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
        except ValueError:
            print("--at очікує формат 'YYYY-MM-DD HH:MM'", file=sys.stderr)
            return 2

    store = Store()
    if args.command == "login":
        return cmd_login(store)
    if args.command == "tick":
        return cmd_tick(store, now, args.dry_run)
    return cmd_status(store, now.date())


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Створити `tick.sh`**

```sh
#!/data/data/com.termux/files/usr/bin/sh
# Викликається termux-job-scheduler. Тік тихий: усе цікаве пише в ~/.tt-checkin/log.txt
cd "$(dirname "$0")" || exit 1
exec python3 tt_checkin.py tick >> "$HOME/.tt-checkin/cron.out" 2>&1
```

Зробити виконуваним: `chmod +x tick.sh`

- [ ] **Step 3: Написати README**

`README.md`:

    # tt-checkin

    Автоматична відмітка відвідування в TimeTable SNU. Один файл, тільки
    стандартна бібліотека Python — на телефоні нічого встановлювати не треба,
    крім самого Python.

    Дизайн і причини рішень: `docs/superpowers/specs/2026-09-08-tt-checkin-design.md`

    ## Встановлення в Termux

        pkg install python termux-api
        # скопіювати tt_checkin.py і tick.sh у ~/tt-checkin/
        cd ~/tt-checkin && chmod +x tick.sh
        python3 tt_checkin.py login

    `login` запитає логін (корпоративна пошта) і пароль. Пароль на диск не
    зберігається — лишається тільки cookie `tt_tokin` у `~/.tt-checkin/token`.

    ## Розписання

        termux-job-scheduler --script ~/tt-checkin/tick.sh \
            --period-ms 1800000 --persisted true

    Далі вимкнути оптимізацію батареї для Termux у налаштуваннях Android,
    інакше система приспить job-scheduler і тіки перестануть приходити.

    ## Команди

        python3 tt_checkin.py status              # план на сьогодні і стан пар
        python3 tt_checkin.py tick                # один прохід
        python3 tt_checkin.py tick --dry-run      # показати, нічого не надсилаючи
        python3 tt_checkin.py tick --at "2026-09-09 09:05"   # підставити час
        tail -f ~/.tt-checkin/log.txt

    ## Файли стану

    `~/.tt-checkin/`: `token`, `plan-YYYY-MM-DD.json`, `state.json`, `log.txt`.
    Каталог 0700, файли 0600.

- [ ] **Step 4: Перевірити CLI без токена**

```bash
python3 tt_checkin.py --help
python3 tt_checkin.py status
python3 tt_checkin.py tick --dry-run
python3 tt_checkin.py tick --at "не дата"
```

Очікується: `--help` показує три команди і два прапорці; `status` без плану
друкує «плану на цей день немає»; `tick` без токена — «немає токена»
і код виходу 1; невалідний `--at` — підказка про формат і код 2.

---

## Локальний живий прогін (виконує користувач)

Автоматизувати неможливо: потрібен реальний вхід, а пароль вводить власник
акаунта. Порядок такий.

1. **Вхід** — виконує користувач у своєму терміналі (пароль ніде не з'являється):

       python3 tt_checkin.py login

   Очікується: `готово: <ПІП>, група <група>, період 2026/2027 семестр 1`.

2. **Розклад і план** — забрати розклад на сьогодні, нічого не відправляючи:

       python3 tt_checkin.py tick --dry-run
       python3 tt_checkin.py status

   Очікується: сьогоднішні пари з їхнім часом і станом `pending`.

3. **Логіка вікон на реальних даних** — підставити час всередині пари:

       python3 tt_checkin.py tick --dry-run --at "2026-09-08 09:05"

   Очікується: `відмітив би: ['1']`.

4. **Справжня відмітка.** Тут потрібне окреме рішення користувача: зараз усі
   сьогоднішні вікна вже закриті, тому реальна відмітка або стане з пізнім
   часом (видно викладачу в журналі), або треба дочекатися наступної пари.
   Обидва варіанти обговорюються перед запуском, за замовчуванням — чекаємо пари.

5. **Підтвердження в журналі** — після справжньої відмітки відкрити журнал
   заняття в браузері й переконатися, що рядок з'явився, а «Завантаження»
   дорівнює `1`, а не більше.
