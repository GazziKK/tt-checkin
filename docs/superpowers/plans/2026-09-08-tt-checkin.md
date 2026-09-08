# tt-checkin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Скрипт для Termux, який автоматично відмічає відвідування занять у
`timetable.lond.lg.ua` у час відповідної пари і підтверджує кожну відмітку через журнал.

**Architecture:** «План-і-тік»: розклад забирається раз на добу і кешується на диск;
`termux-job-scheduler` будить `tick` раз на 30 хвилин, тік читає кешований план локально
і йде в мережу лише коли пора відмічатися. Уся логіка вікон і станів — у чистих функціях
без мережі; HTTP ізольований в одному модулі.

**Tech Stack:** Python 3.10+, `requests`, `beautifulsoup4`, `pytest`, Termux API
(`termux-job-scheduler`, `termux-notification`).

**Spec:** `docs/superpowers/specs/2026-09-08-tt-checkin-design.md`

## Global Constraints

- Python `>=3.10`. Залежності рантайму: тільки `requests>=2.31` і `beautifulsoup4>=4.12`.
- Тести не роблять жодного сетевого запиту. HTTP підмінюється фейковою сесією.
- Базовий URL: `https://timetable.lond.lg.ua/`. Скрипти: `php/Avtorization.php` (JSON),
  `php/getQuery.php` (`application/x-www-form-urlencoded`).
- Час — наївний локальний (годинник телефона). Жодних tz-конверсій.
- Формат дат у запитах і даних: `DD.MM.YYYY`. Формат часу: `HH:MM:SS`.
- `year` — рядок `"2026/2027"`, `semestr` — число.
- Каталог стану `~/.tt-checkin` створюється з правами `0700`, усі файли в ньому — `0600`.
- **Пароль ніколи не пишеться на диск. Токен ніколи не потрапляє в лог.**
- Поля `lesson` і `type` у запиті відмітки несуть переплутані значення (`Вид` і
  `№ тижня` відповідно) — це фактична поведінка сайту, її треба повторювати точно.

---

### Task 1: Каркас проєкту + навчальний період

**Files:**
- Create: `pyproject.toml`
- Create: `tt_checkin/__init__.py`
- Create: `tt_checkin/period.py`
- Test: `tests/test_period.py`

**Interfaces:**
- Consumes: нічого.
- Produces: `academic_period(today: date) -> tuple[str, int]`,
  `candidates(today: date) -> list[tuple[str, int]]`.

- [ ] **Step 1: Створити каркас проєкту**

`pyproject.toml`:

```toml
[project]
name = "tt-checkin"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["requests>=2.31", "beautifulsoup4>=4.12"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`tt_checkin/__init__.py` — порожній файл.

- [ ] **Step 2: Написати падаючий тест**

`tests/test_period.py`:

```python
from datetime import date

import pytest

from tt_checkin.period import academic_period, candidates


@pytest.mark.parametrize(
    "day,expected",
    [
        (date(2026, 9, 1), ("2026/2027", 1)),
        (date(2026, 9, 8), ("2026/2027", 1)),
        (date(2026, 12, 31), ("2026/2027", 1)),
        (date(2027, 1, 15), ("2026/2027", 1)),
        (date(2027, 2, 1), ("2026/2027", 2)),
        (date(2027, 8, 31), ("2026/2027", 2)),
    ],
)
def test_academic_period(day, expected):
    assert academic_period(day) == expected


def test_candidates_starts_with_primary():
    assert candidates(date(2026, 9, 8))[0] == ("2026/2027", 1)


def test_candidates_have_no_duplicates():
    result = candidates(date(2026, 9, 8))
    assert len(result) == len(set(result))


def test_candidates_include_other_semester():
    assert ("2026/2027", 2) in candidates(date(2026, 9, 8))
```

- [ ] **Step 3: Запустити тест і переконатися, що падає**

Run: `python -m pytest tests/test_period.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tt_checkin.period'`

- [ ] **Step 4: Реалізувати мінімум**

`tt_checkin/period.py`:

```python
"""Навчальний період: рік у форматі "2026/2027" і номер семестру.

Сервер ці значення не віддає — їх треба надсилати. У коді сайту вони
обчислюються з поточної дати, тут відтворено те саме правило.
"""

from __future__ import annotations

from datetime import date

Period = tuple[str, int]


def academic_period(today: date) -> Period:
    """Вересень–грудень → Y/Y+1 семестр 1; січень → Y-1/Y семестр 1;
    лютий–серпень → Y-1/Y семестр 2."""
    if today.month >= 9:
        return f"{today.year}/{today.year + 1}", 1
    if today.month == 1:
        return f"{today.year - 1}/{today.year}", 1
    return f"{today.year - 1}/{today.year}", 2


def candidates(today: date) -> list[Period]:
    """Основний період плюс суміжні — щоб `checkAccess` мав що спробувати,
    якщо межі семестру не збігаються з нашим правилом."""
    year, semestr = academic_period(today)
    first, second = (int(part) for part in year.split("/"))
    previous = f"{first - 1}/{second - 1}"
    other = 2 if semestr == 1 else 1
    ordered = [(year, semestr), (year, other), (previous, semestr)]
    seen: list[Period] = []
    for item in ordered:
        if item not in seen:
            seen.append(item)
    return seen
```

- [ ] **Step 5: Запустити тест і переконатися, що проходить**

Run: `python -m pytest tests/test_period.py -v`
Expected: PASS (9 тестів)

- [ ] **Step 6: Коміт**

```bash
git add pyproject.toml tt_checkin/__init__.py tt_checkin/period.py tests/test_period.py
git commit -m "feat: обчислення навчального року і семестру з дати"
```

---

### Task 2: Модель даних + парсинг розкладу

**Files:**
- Create: `tt_checkin/model.py`
- Create: `tt_checkin/schedule.py`
- Create: `tests/fixtures/schedule_day.txt`
- Create: `tests/fixtures/schedule_empty.txt`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Consumes: нічого.
- Produces: `Lesson` (поля `id_lesson`, `num_lesson`, `kind`, `week_numbers`,
  `teacher`, `id_fio`, `group`, `dateles`, `timebeg`, `timeend`, `discipline`;
  властивість `key: str`; метод `window() -> tuple[datetime, datetime]`),
  `User(fio, id_user, id_fio, group, id_group)`,
  `DaySchedule(weekday: str, week_number: int, lessons: tuple[Lesson, ...])`,
  `parse_day(response_text: str) -> DaySchedule`,
  `cell_text(td) -> str`.

- [ ] **Step 1: Створити фікстури реальної відповіді**

`tests/fixtures/schedule_day.txt` (один рядок, без переводу в кінці — формат
`день|тиждень|HTML рядків`; структура знята з живої відповіді, ПІП викладача
замінено на тестове):

```
ПОНЕДІЛОК|2|<tr class="tr1" data-id_lesson="3570" data-group="БЦІ-26д-2" data-numlesson="1" data-lesson="Вища математика. Частина 1." data-id_fio="647" data-dateles="08.09.2026" data-timebeg="09:00:00" data-timeend="10:20:00" data-items=""><td>1</td><td>09:00-<br>10:20</td><td><span class="lk">ЛК</span></td><td><button onclick="Modul_TT.loadZoom('MEET_URL')"><img src="img/Teams.png"></button>Вища математика. Частина 1.<button onclick="Modul_TT.ListStudent()"><img src="img/client1_list.png"></button></td><td>-</td><td><span class="week">2</span>, 4, 6, 8, 10, 12</td><td><i>доцент</i><br>Тестовий Викладач Іванович</td><td>БЦІ-26д-2</td><td>401(ІП)</td></tr><tr class="tr2" data-id_lesson="3571" data-group="БЦІ-26д-2" data-numlesson="2" data-lesson="Хімія" data-id_fio="134" data-dateles="08.09.2026" data-timebeg="10:35:00" data-timeend="11:55:00" data-items=""><td>2</td><td>10:35-<br>11:55</td><td><span class="lk">ПЗ</span></td><td><button onclick="Modul_TT.loadZoom('MEET_URL')"><img src="img/Teams.png"></button>Хімія</td><td>-</td><td><span class="week">2</span>, 4, 6</td><td><i>доцент</i><br>Інший Викладач Петрович</td><td>БЦІ-26д-2</td><td></td></tr>
```

`tests/fixtures/schedule_empty.txt` (день без занять — номер тижня `0`):

```
СУБОТА|0|
```

- [ ] **Step 2: Написати падаючі тести**

`tests/test_schedule.py`:

```python
from datetime import datetime
from pathlib import Path

import pytest

from tt_checkin.schedule import parse_day

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def day():
    return parse_day(load("schedule_day.txt"))


def test_parses_weekday_and_week_number(day):
    assert day.weekday == "ПОНЕДІЛОК"
    assert day.week_number == 2


def test_parses_both_lessons(day):
    assert len(day.lessons) == 2
    assert [lesson.id_lesson for lesson in day.lessons] == ["3570", "3571"]


def test_kind_comes_from_third_cell(day):
    # cells[2] — це «Вид», і саме воно летить у полі `lesson`
    assert day.lessons[0].kind == "ЛК"
    assert day.lessons[1].kind == "ПЗ"


def test_week_numbers_come_from_sixth_cell(day):
    # cells[5] — це «№ тижня», і саме воно летить у полі `type`
    assert day.lessons[0].week_numbers == "2, 4, 6, 8, 10, 12"


def test_teacher_keeps_line_break_from_br(day):
    assert day.lessons[0].teacher == "доцент\nТестовий Викладач Іванович"


def test_dataset_fields(day):
    lesson = day.lessons[0]
    assert lesson.num_lesson == "1"
    assert lesson.id_fio == "647"
    assert lesson.group == "БЦІ-26д-2"
    assert lesson.dateles == "08.09.2026"
    assert lesson.timebeg == "09:00:00"
    assert lesson.timeend == "10:20:00"
    assert lesson.discipline == "Вища математика. Частина 1."


def test_key_is_unique_per_lesson(day):
    assert day.lessons[0].key == "08.09.2026#3570#1"
    assert day.lessons[0].key != day.lessons[1].key


def test_window(day):
    beg, end = day.lessons[0].window()
    assert beg == datetime(2026, 9, 8, 9, 0, 0)
    assert end == datetime(2026, 9, 8, 10, 20, 0)


def test_empty_day_has_no_lessons():
    result = parse_day(load("schedule_empty.txt"))
    assert result.weekday == "СУБОТА"
    assert result.week_number == 0
    assert result.lessons == ()


def test_garbage_response_does_not_raise():
    result = parse_day("<br />\n<b>Warning</b>: щось зламалось")
    assert result.lessons == ()


def test_pipe_inside_html_does_not_truncate_rows():
    payload = load("schedule_day.txt").replace("401(ІП)", "401|ІП")
    assert len(parse_day(payload).lessons) == 2
```

- [ ] **Step 3: Запустити тести і переконатися, що падають**

Run: `python -m pytest tests/test_schedule.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tt_checkin.schedule'`

- [ ] **Step 4: Реалізувати модель**

`tt_checkin/model.py`:

```python
"""Структури даних, спільні для всіх модулів."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

DATE_FMT = "%d.%m.%Y"
TIME_FMT = "%H:%M:%S"


@dataclass(frozen=True)
class Lesson:
    """Одна пара з розкладу на день.

    `kind` і `week_numbers` названі за їхнім справжнім змістом, але у запиті
    відмітки надсилаються у полях `lesson` і `type` відповідно — сайт їх
    плутає, і ми повторюємо його поведінку.
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
    """Контекст користувача з відповіді `checkAccess`."""

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

- [ ] **Step 5: Реалізувати парсер**

`tt_checkin/schedule.py`:

```python
"""Розбір відповідей `getQuery.php`.

Відповіді — не JSON: це рядок, розділений вертикальною рискою, у якому одна
з частин містить HTML рядків `<tr>`.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from .model import DaySchedule, Lesson


def cell_text(td) -> str:
    """Текст клітинки так, як його бачить `innerText`: `<br>` стає переводом
    рядка, зайві пробіли з'їдаються, порожні рядки відкидаються."""
    for br in td.find_all("br"):
        br.replace_with("\n")
    lines = (" ".join(part.split()) for part in td.get_text().split("\n"))
    return "\n".join(line for line in lines if line)


def _rows(rows_html: str):
    soup = BeautifulSoup(f"<table><tbody>{rows_html}</tbody></table>", "html.parser")
    return soup.select("tr[data-id_lesson]")


def parse_day(response_text: str) -> DaySchedule:
    parts = response_text.split("|", 2)
    if len(parts) < 3:
        return DaySchedule(parts[0].strip() if parts else "", 0, ())

    weekday, raw_week, rows_html = parts
    try:
        week_number = int(raw_week.strip())
    except ValueError:
        week_number = 0
    if week_number <= 0:
        return DaySchedule(weekday.strip(), 0, ())

    lessons = []
    for tr in _rows(rows_html):
        cells = tr.find_all("td")
        if len(cells) < 7:
            continue
        attrs = tr.attrs
        lessons.append(
            Lesson(
                id_lesson=attrs["data-id_lesson"],
                num_lesson=cell_text(cells[0]),
                kind=cell_text(cells[2]),
                week_numbers=cell_text(cells[5]),
                teacher=cell_text(cells[6]),
                id_fio=attrs.get("data-id_fio", ""),
                group=attrs.get("data-group", ""),
                dateles=attrs.get("data-dateles", ""),
                timebeg=attrs.get("data-timebeg", ""),
                timeend=attrs.get("data-timeend", ""),
                discipline=attrs.get("data-lesson", ""),
            )
        )
    return DaySchedule(weekday.strip(), week_number, tuple(lessons))
```

- [ ] **Step 6: Запустити тести і переконатися, що проходять**

Run: `python -m pytest tests/test_schedule.py -v`
Expected: PASS (12 тестів)

- [ ] **Step 7: Коміт**

```bash
git add tt_checkin/model.py tt_checkin/schedule.py tests/test_schedule.py tests/fixtures
git commit -m "feat: модель даних і парсинг відповіді розкладу"
```

---

### Task 3: Парсинг журналу і перевірка власної відмітки

**Files:**
- Modify: `tt_checkin/schedule.py` (додати `JournalRow`, `parse_journal`, `is_registered`)
- Create: `tests/fixtures/journal.txt`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: `cell_text` з Task 2.
- Produces: `JournalRow(num, fio, status, group, time, loads)`,
  `parse_journal(response_text: str) -> tuple[JournalRow, ...]`,
  `is_registered(rows: tuple[JournalRow, ...], fio: str) -> bool`,
  `registration_time(rows, fio) -> str | None`.

- [ ] **Step 1: Створити фікстуру журналу**

`tests/fixtures/journal.txt` (колонки `№ | ПІП | Статус | Группа | Час | Завантаження`;
рядки лежать у нульовій частині відповіді):

```
<tr class="tr1"><td class="tabPS">1</td><td class="tabPS">Тестовий Викладач Іванович</td><td class="tabPS">викладач</td><td class="tabPS">—</td><td class="tabPS">08:55:01</td><td class="tabPS">1</td></tr><tr class="tr2"><td class="tabPS">2</td><td class="tabPS">Студент Перший Петрович</td><td class="tabPS">студент</td><td class="tabPS">БЦІ-26д-2</td><td class="tabPS">09:03:12</td><td class="tabPS">1</td></tr><tr class="tr1"><td class="tabPS">3</td><td class="tabPS">Тестовий   Студент Іванович</td><td class="tabPS">студент</td><td class="tabPS">БЦІ-26д-2</td><td class="tabPS">09:04:47</td><td class="tabPS">2</td></tr>|
```

- [ ] **Step 2: Написати падаючі тести**

`tests/test_journal.py`:

```python
from pathlib import Path

import pytest

from tt_checkin.schedule import is_registered, parse_journal, registration_time

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rows():
    return parse_journal((FIXTURES / "journal.txt").read_text(encoding="utf-8"))


def test_parses_all_rows(rows):
    assert len(rows) == 3


def test_parses_columns(rows):
    row = rows[1]
    assert row.num == "2"
    assert row.fio == "Студент Перший Петрович"
    assert row.status == "студент"
    assert row.group == "БЦІ-26д-2"
    assert row.time == "09:03:12"
    assert row.loads == "1"


def test_is_registered_finds_own_name(rows):
    assert is_registered(rows, "Студент Перший Петрович") is True


def test_is_registered_is_false_for_absent_name(rows):
    assert is_registered(rows, "Відсутній Студент Іванович") is False


def test_is_registered_ignores_extra_whitespace(rows):
    assert is_registered(rows, "Тестовий Студент Іванович") is True


def test_is_registered_ignores_case(rows):
    assert is_registered(rows, "студент перший петрович") is True


def test_is_registered_does_not_match_partial_name(rows):
    assert is_registered(rows, "Студент") is False


def test_registration_time_returns_own_time(rows):
    assert registration_time(rows, "Студент Перший Петрович") == "09:03:12"


def test_registration_time_returns_none_when_absent(rows):
    assert registration_time(rows, "Відсутній Студент Іванович") is None


def test_empty_response_gives_no_rows():
    assert parse_journal("") == ()
```

- [ ] **Step 3: Запустити тести і переконатися, що падають**

Run: `python -m pytest tests/test_journal.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_registered'`

- [ ] **Step 4: Дописати реалізацію в `tt_checkin/schedule.py`**

Додати `from dataclasses import dataclass` до імпортів і в кінець файлу:

```python
@dataclass(frozen=True)
class JournalRow:
    num: str
    fio: str
    status: str
    group: str
    time: str
    loads: str


def _normalize(fio: str) -> str:
    return " ".join(fio.split()).casefold()


def parse_journal(response_text: str) -> tuple[JournalRow, ...]:
    rows_html = response_text.split("|")[0]
    soup = BeautifulSoup(f"<table><tbody>{rows_html}</tbody></table>", "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 6:
            continue
        values = [cell_text(cell) for cell in cells[:6]]
        rows.append(JournalRow(*values))
    return tuple(rows)


def is_registered(rows: tuple[JournalRow, ...], fio: str) -> bool:
    return registration_time(rows, fio) is not None


def registration_time(rows: tuple[JournalRow, ...], fio: str) -> str | None:
    wanted = _normalize(fio)
    for row in rows:
        if _normalize(row.fio) == wanted:
            return row.time
    return None
```

- [ ] **Step 5: Запустити тести і переконатися, що проходять**

Run: `python -m pytest tests/test_journal.py tests/test_schedule.py -v`
Expected: PASS (22 тести)

- [ ] **Step 6: Коміт**

```bash
git add tt_checkin/schedule.py tests/test_journal.py tests/fixtures/journal.txt
git commit -m "feat: парсинг журналу і перевірка власної відмітки"
```

---

### Task 4: Рішення тіку (чиста логіка)

**Files:**
- Create: `tt_checkin/decide.py`
- Test: `tests/test_decide.py`

**Interfaces:**
- Consumes: `Lesson` з Task 2.
- Produces: `LessonState` (значення `"pending"`, `"sent"`, `"done"`),
  `TickPlan(to_verify: tuple[Lesson, ...], to_check_in: tuple[Lesson, ...])`,
  `window_is_open(now: datetime, lesson: Lesson) -> bool`,
  `plan_tick(now: datetime, lessons: Sequence[Lesson], state: Mapping[str, str]) -> TickPlan`.

- [ ] **Step 1: Написати падаючі тести**

`tests/test_decide.py`:

```python
from datetime import datetime

import pytest

from tt_checkin.decide import LessonState, plan_tick, window_is_open
from tt_checkin.model import Lesson


def make_lesson(num: str = "1", beg: str = "09:00:00", end: str = "10:20:00") -> Lesson:
    return Lesson(
        id_lesson="3570",
        num_lesson=num,
        kind="ЛК",
        week_numbers="2, 4",
        teacher="доцент\nТестовий Викладач Іванович",
        id_fio="647",
        group="БЦІ-26д-2",
        dateles="08.09.2026",
        timebeg=beg,
        timeend=end,
        discipline="Вища математика. Частина 1.",
    )


FIRST = make_lesson()
SECOND = make_lesson(num="2", beg="10:35:00", end="11:55:00")


@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 9, 8, 8, 59, 59), False),
        (datetime(2026, 9, 8, 9, 0, 0), True),
        (datetime(2026, 9, 8, 9, 30, 0), True),
        (datetime(2026, 9, 8, 10, 19, 59), True),
        (datetime(2026, 9, 8, 10, 20, 0), False),
        (datetime(2026, 9, 8, 23, 0, 0), False),
    ],
)
def test_window_is_open_includes_start_and_excludes_end(now, expected):
    assert window_is_open(now, FIRST) is expected


def test_pending_lesson_inside_window_is_checked_in():
    plan = plan_tick(datetime(2026, 9, 8, 9, 5), [FIRST], {})
    assert plan.to_check_in == (FIRST,)
    assert plan.to_verify == ()


def test_pending_lesson_outside_window_is_left_alone():
    plan = plan_tick(datetime(2026, 9, 8, 8, 30), [FIRST], {})
    assert plan.to_check_in == ()
    assert plan.to_verify == ()


def test_done_lesson_is_never_touched_again():
    state = {FIRST.key: LessonState.DONE}
    plan = plan_tick(datetime(2026, 9, 8, 9, 5), [FIRST], state)
    assert plan.to_check_in == ()
    assert plan.to_verify == ()


def test_sent_lesson_goes_to_verification_not_to_resend():
    state = {FIRST.key: LessonState.SENT}
    plan = plan_tick(datetime(2026, 9, 8, 9, 5), [FIRST], state)
    assert plan.to_check_in == ()
    assert plan.to_verify == (FIRST,)


def test_sent_lesson_is_verified_even_after_window_closed():
    state = {FIRST.key: LessonState.SENT}
    plan = plan_tick(datetime(2026, 9, 8, 18, 0), [FIRST], state)
    assert plan.to_verify == (FIRST,)


def test_empty_day_produces_empty_plan():
    plan = plan_tick(datetime(2026, 9, 8, 9, 5), [], {})
    assert plan.to_check_in == ()
    assert plan.to_verify == ()


def test_only_the_lesson_whose_window_is_open_is_checked_in():
    plan = plan_tick(datetime(2026, 9, 8, 11, 0), [FIRST, SECOND], {})
    assert plan.to_check_in == (SECOND,)


def test_overlapping_windows_are_both_checked_in():
    overlapping = make_lesson(num="3", beg="09:10:00", end="10:30:00")
    plan = plan_tick(datetime(2026, 9, 8, 9, 15), [FIRST, overlapping], {})
    assert plan.to_check_in == (FIRST, overlapping)


def test_unknown_state_value_is_treated_as_pending():
    plan = plan_tick(datetime(2026, 9, 8, 9, 5), [FIRST], {FIRST.key: "щось дивне"})
    assert plan.to_check_in == (FIRST,)
```

- [ ] **Step 2: Запустити тести і переконатися, що падають**

Run: `python -m pytest tests/test_decide.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tt_checkin.decide'`

- [ ] **Step 3: Реалізувати мінімум**

`tt_checkin/decide.py`:

```python
"""Що робити на цьому тіку. Чиста логіка: без мережі, без диска, без годинника —
`now` завжди приходить аргументом."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .model import Lesson


class LessonState(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    DONE = "done"


@dataclass(frozen=True)
class TickPlan:
    to_verify: tuple[Lesson, ...]
    to_check_in: tuple[Lesson, ...]


def window_is_open(now: datetime, lesson: Lesson) -> bool:
    """Початок включно, кінець виключно."""
    beg, end = lesson.window()
    return beg <= now < end


def plan_tick(
    now: datetime,
    lessons: Sequence[Lesson],
    state: Mapping[str, str],
) -> TickPlan:
    to_verify: list[Lesson] = []
    to_check_in: list[Lesson] = []

    for lesson in lessons:
        current = state.get(lesson.key, LessonState.PENDING)
        if current == LessonState.DONE:
            continue
        if current == LessonState.SENT:
            # Відправлено, але не підтверджено: спершу дивимо журнал,
            # щоб не надіслати відмітку вдруге і не накрутити «Завантаження».
            to_verify.append(lesson)
            continue
        if window_is_open(now, lesson):
            to_check_in.append(lesson)

    return TickPlan(tuple(to_verify), tuple(to_check_in))
```

- [ ] **Step 4: Запустити тести і переконатися, що проходять**

Run: `python -m pytest tests/test_decide.py -v`
Expected: PASS (15 тестів)

- [ ] **Step 5: Коміт**

```bash
git add tt_checkin/decide.py tests/test_decide.py
git commit -m "feat: чиста логіка рішень тіку з трьома станами пари"
```

---

### Task 5: Сховище стану на диску

**Files:**
- Create: `tt_checkin/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Lesson` з Task 2.
- Produces: `Store(root: Path)` з методами
  `read_token() -> str | None`, `write_token(value: str) -> None`,
  `read_state() -> dict[str, str]`, `write_state(state: Mapping[str, str]) -> None`,
  `read_plan(day: date) -> tuple[Lesson, ...] | None`,
  `write_plan(day: date, lessons: Sequence[Lesson]) -> None`,
  `log(message: str) -> None`, властивість `log_path: Path`.

- [ ] **Step 1: Написати падаючі тести**

`tests/test_store.py`:

```python
import stat
from datetime import date, datetime

import pytest

from tt_checkin.model import Lesson
from tt_checkin.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / ".tt-checkin")


LESSON = Lesson(
    id_lesson="3570",
    num_lesson="1",
    kind="ЛК",
    week_numbers="2, 4",
    teacher="доцент\nТестовий Викладач Іванович",
    id_fio="647",
    group="БЦІ-26д-2",
    dateles="08.09.2026",
    timebeg="09:00:00",
    timeend="10:20:00",
    discipline="Вища математика. Частина 1.",
)


def test_missing_token_is_none(store):
    assert store.read_token() is None


def test_token_round_trip(store):
    store.write_token("abc123")
    assert store.read_token() == "abc123"


def test_token_file_is_not_readable_by_others(store):
    store.write_token("abc123")
    mode = (store.root / "token").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_root_directory_is_private(store):
    store.write_token("abc123")
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700


def test_missing_state_is_empty_dict(store):
    assert store.read_state() == {}


def test_state_round_trip(store):
    store.write_state({"08.09.2026#3570#1": "done"})
    assert store.read_state() == {"08.09.2026#3570#1": "done"}


def test_corrupted_state_is_treated_as_empty(store):
    store.write_state({})
    (store.root / "state.json").write_text("{не json", encoding="utf-8")
    assert store.read_state() == {}


def test_missing_plan_is_none(store):
    assert store.read_plan(date(2026, 9, 8)) is None


def test_plan_round_trip_preserves_every_field(store):
    store.write_plan(date(2026, 9, 8), [LESSON])
    restored = store.read_plan(date(2026, 9, 8))
    assert restored == (LESSON,)


def test_plan_is_per_day(store):
    store.write_plan(date(2026, 9, 8), [LESSON])
    assert store.read_plan(date(2026, 9, 9)) is None


def test_log_appends_lines_with_timestamp(store):
    store.log("перший")
    store.log("другий")
    lines = store.log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("перший")
    assert lines[1].endswith("другий")
    # рядок починається з ISO-часу
    datetime.fromisoformat(lines[0].split(" ", 1)[0])
```

- [ ] **Step 2: Запустити тести і переконатися, що падають**

Run: `python -m pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tt_checkin.store'`

- [ ] **Step 3: Реалізувати мінімум**

`tt_checkin/store.py`:

```python
"""Стан на диску: токен, стани пар, кеш розкладу, лог.

Каталог створюється з правами 0700, файли — 0600. Токен у лог не пишеться
ніколи, пароль тут не зберігається взагалі.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from .model import Lesson

DEFAULT_ROOT = Path.home() / ".tt-checkin"


class Store:
    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)

    # --- службове ---

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def _write_private(self, name: str, content: str) -> None:
        self._ensure_root()
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

    # --- токен ---

    def read_token(self) -> str | None:
        path = self.root / "token"
        if not path.exists():
            return None
        value = path.read_text(encoding="utf-8").strip()
        return value or None

    def write_token(self, value: str) -> None:
        self._write_private("token", value)

    # --- стани пар ---

    def read_state(self) -> dict[str, str]:
        path = self.root / "state.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def write_state(self, state: Mapping[str, str]) -> None:
        self._write_private("state.json", json.dumps(dict(state), ensure_ascii=False, indent=1))

    # --- кеш розкладу ---

    def _plan_name(self, day: date) -> str:
        return f"plan-{day.isoformat()}.json"

    def read_plan(self, day: date) -> tuple[Lesson, ...] | None:
        path = self.root / self._plan_name(day)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, list):
            return None
        return tuple(Lesson(**item) for item in data)

    def write_plan(self, day: date, lessons: Sequence[Lesson]) -> None:
        payload = [asdict(lesson) for lesson in lessons]
        self._write_private(
            self._plan_name(day), json.dumps(payload, ensure_ascii=False, indent=1)
        )

    # --- лог ---

    @property
    def log_path(self) -> Path:
        return self.root / "log.txt"

    def log(self, message: str) -> None:
        self._ensure_root()
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        self.log_path.chmod(0o600)
```

- [ ] **Step 4: Запустити тести і переконатися, що проходять**

Run: `python -m pytest tests/test_store.py -v`
Expected: PASS (11 тестів)

- [ ] **Step 5: Коміт**

```bash
git add tt_checkin/store.py tests/test_store.py
git commit -m "feat: сховище стану з приватними правами на файли"
```

---

### Task 6: HTTP-клієнт

**Files:**
- Create: `tt_checkin/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `Lesson`, `User` з Task 2.
- Produces: `BASE_URL`, `NetworkError`, `TimeTableClient(session, base_url=BASE_URL)` з методами
  `set_token(value: str) -> None`, `token -> str | None`,
  `login(login: str, password: str) -> None`,
  `check_access(year: str, semestr: int) -> User | None`,
  `day_schedule(user: User, year: str, semestr: int, day: date) -> str`,
  `check_in(user: User, lesson: Lesson) -> str`,
  `journal(user: User, lesson: Lesson) -> str`.

- [ ] **Step 1: Написати падаючі тести**

`tests/test_client.py`:

```python
import json
from datetime import date

import pytest

import requests

from tt_checkin.client import NetworkError, TimeTableClient
from tt_checkin.model import Lesson, User

USER = User(
    fio="Тестовий Студент Іванович",
    id_user="623",
    id_fio="0",
    group="БЦІ-26д-2",
    id_group="42",
)

LESSON = Lesson(
    id_lesson="3570",
    num_lesson="1",
    kind="ЛК",
    week_numbers="2, 4, 6",
    teacher="доцент\nТестовий Викладач Іванович",
    id_fio="647",
    group="БЦІ-26д-2",
    dateles="08.09.2026",
    timebeg="09:00:00",
    timeend="10:20:00",
    discipline="Вища математика. Частина 1.",
)


class FakeResponse:
    def __init__(self, text: str = "", payload=None) -> None:
        self.text = text
        self._payload = payload
        self.status_code = 200

    def json(self):
        if self._payload is None:
            raise ValueError("не JSON")
        return self._payload

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.response = response or FakeResponse()
        self.calls: list[dict] = []
        self.cookies = {}

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "data": data, "json": json, "headers": headers, "timeout": timeout}
        )
        return self.response


@pytest.fixture
def session():
    return FakeSession()


def client_for(session) -> TimeTableClient:
    return TimeTableClient(session)


def test_login_posts_json_with_exact_field_names(session):
    client_for(session).login("bci-26d-2-623@snu.edu.ua", "секрет")
    call = session.calls[0]
    assert call["url"].endswith("php/Avtorization.php")
    assert call["json"] == {
        "method": "checkPassword",
        "login": "bci-26d-2-623@snu.edu.ua",
        "password": "секрет",
    }
    assert call["data"] is None


def test_check_access_parses_nested_user_json():
    payload = {
        "status": "ok",
        "user": json.dumps(
            {
                "fio": "Тестовий Студент Іванович",
                "id_user": 623,
                "id_fio": 0,
                "group": "БЦІ-26д-2",
                "id_group": 42,
            }
        ),
    }
    session = FakeSession(FakeResponse(payload=payload))
    user = client_for(session).check_access("2026/2027", 1)
    assert user == USER
    assert session.calls[0]["json"] == {
        "method": "checkAccess",
        "year": "2026/2027",
        "semestr": 1,
    }


def test_check_access_returns_none_when_status_is_not_ok():
    session = FakeSession(FakeResponse(payload={"status": "error"}))
    assert client_for(session).check_access("2026/2027", 1) is None


def test_check_access_returns_none_when_group_is_empty():
    payload = {"status": "ok", "user": json.dumps({"fio": "х", "id_user": 1, "id_fio": 0, "group": "", "id_group": 0})}
    session = FakeSession(FakeResponse(payload=payload))
    assert client_for(session).check_access("2026/2027", 1) is None


def test_check_access_returns_none_on_non_json_response():
    session = FakeSession(FakeResponse(text="<b>Warning</b>"))
    assert client_for(session).check_access("2026/2027", 1) is None


def test_day_schedule_posts_form_encoded_fields(session):
    client_for(session).day_schedule(USER, "2026/2027", 1, date(2026, 9, 8))
    call = session.calls[0]
    assert call["url"].endswith("php/getQuery.php")
    assert call["json"] is None
    assert call["data"] == {
        "param": "get_listLessonTodayStudent",
        "group": "БЦІ-26д-2",
        "id_group": "42",
        "date": "08.09.2026",
        "year": "2026/2027",
        "semestr": 1,
    }


def test_check_in_sends_swapped_fields_exactly_as_the_site_does(session):
    client_for(session).check_in(USER, LESSON)
    assert session.calls[0]["data"] == {
        "param": "get_checkZoom",
        "state": "s",
        "group": "БЦІ-26д-2",
        "fioUser": "Тестовий Студент Іванович",
        "idUser": "623",
        "numLesson": "1",
        "lesson": "ЛК",
        "id_lesson": "3570",
        "type": "2, 4, 6",
        "fioTeacher": "доцент\nТестовий Викладач Іванович",
        "id_fio": "647",
        "dateles": "08.09.2026",
        "timebeg": "09:00:00",
        "timeend": "10:20:00",
    }


def test_journal_uses_date_not_dateles(session):
    client_for(session).journal(USER, LESSON)
    data = session.calls[0]["data"]
    assert data["date"] == "08.09.2026"
    assert "dateles" not in data
    assert data["param"] == "get_listStudent"
    assert data["akademGroup"] == "1"


def test_set_token_puts_cookie_on_session(session):
    client_for(session).set_token("abc123")
    assert session.cookies["tt_tokin"] == "abc123"


def test_transport_failure_becomes_network_error():
    class BrokenSession(FakeSession):
        def post(self, *args, **kwargs):
            raise requests.ConnectionError("немає мережі")

    with pytest.raises(NetworkError):
        client_for(BrokenSession()).check_access("2026/2027", 1)
```

- [ ] **Step 2: Запустити тести і переконатися, що падають**

Run: `python -m pytest tests/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tt_checkin.client'`

- [ ] **Step 3: Реалізувати мінімум**

`tt_checkin/client.py`:

```python
"""HTTP до TimeTable SNU.

Два скрипти з різним кодуванням тіла:
`php/Avtorization.php` приймає JSON, `php/getQuery.php` — form-urlencoded.
"""

from __future__ import annotations

import json
from datetime import date

import requests

from .model import DATE_FMT, Lesson, User

BASE_URL = "https://timetable.lond.lg.ua/"
AVTORIZATION = "php/Avtorization.php"
GET_QUERY = "php/getQuery.php"
TIMEOUT = 30
JSON_HEADERS = {"Content-Type": "application/json;charset=utf-8"}


class NetworkError(Exception):
    """Мережа недоступна або сервер не відповів. Тік має це переживати,
    а не падати: наступний тік у межах вікна повторить."""


class TimeTableClient:
    def __init__(self, session, base_url: str = BASE_URL) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/") + "/"

    # --- токен ---

    @property
    def token(self) -> str | None:
        return self._session.cookies.get("tt_tokin")

    def set_token(self, value: str) -> None:
        self._session.cookies["tt_tokin"] = value

    # --- запити ---

    def _post(self, url: str, **kwargs):
        try:
            return self._session.post(url, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as error:
            raise NetworkError(str(error)) from error

    def _post_json(self, payload: dict):
        return self._post(
            self._base_url + AVTORIZATION, json=payload, headers=JSON_HEADERS
        )

    def _post_form(self, payload: dict):
        return self._post(self._base_url + GET_QUERY, data=payload)

    def login(self, login: str, password: str) -> None:
        """Надсилає пароль так, як це робить сама сторінка входу: відкритим
        текстом у JSON по HTTPS. Успіх перевіряється не тут, а `check_access`."""
        self._post_json({"method": "checkPassword", "login": login, "password": password})

    def check_access(self, year: str, semestr: int) -> User | None:
        """Контекст користувача. `None` означає «токен не працює або період
        не той» — розрізняти ці випадки нам не потрібно."""
        response = self._post_json({"method": "checkAccess", "year": year, "semestr": semestr})
        try:
            data = response.json()
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("status") != "ok":
            return None
        try:
            raw = json.loads(data.get("user", ""))
        except (ValueError, TypeError):
            return None
        if not raw.get("group"):
            return None
        return User(
            fio=str(raw.get("fio", "")),
            id_user=str(raw.get("id_user", "")),
            id_fio=str(raw.get("id_fio", "")),
            group=str(raw["group"]),
            id_group=str(raw.get("id_group", "")),
        )

    def day_schedule(self, user: User, year: str, semestr: int, day: date) -> str:
        response = self._post_form(
            {
                "param": "get_listLessonTodayStudent",
                "group": user.group,
                "id_group": user.id_group,
                "date": day.strftime(DATE_FMT),
                "year": year,
                "semestr": semestr,
            }
        )
        return response.text

    def check_in(self, user: User, lesson: Lesson) -> str:
        """Поля `lesson` і `type` навмисно несуть Вид і № тижня — саме так
        робить сайт (див. §2 спеки)."""
        response = self._post_form(
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
        return response.text

    def journal(self, user: User, lesson: Lesson) -> str:
        response = self._post_form(
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
        return response.text
```

- [ ] **Step 4: Запустити тести і переконатися, що проходять**

Run: `python -m pytest tests/test_client.py -v`
Expected: PASS (11 тестів)

- [ ] **Step 5: Коміт**

```bash
git add tt_checkin/client.py tests/test_client.py
git commit -m "feat: HTTP-клієнт з точними контрактами обох скриптів"
```

---

### Task 7: Оркестрація тіку + нотифікації

**Files:**
- Create: `tt_checkin/notify.py`
- Create: `tt_checkin/tick.py`
- Test: `tests/test_tick.py`

**Interfaces:**
- Consumes: усе з Task 2–6.
- Produces: `notify(title: str, message: str) -> None`, `MAX_ATTEMPTS = 3`,
  `run_tick(now: datetime, client, store, notifier=notify) -> str`.

- [ ] **Step 1: Написати падаючі тести**

`tests/test_tick.py`:

```python
import json
from datetime import date, datetime

import pytest

from tt_checkin.client import NetworkError
from tt_checkin.decide import LessonState
from tt_checkin.model import Lesson, User
from tt_checkin.store import Store
from tt_checkin.tick import run_tick

USER = User(
    fio="Тестовий Студент Іванович",
    id_user="623",
    id_fio="0",
    group="БЦІ-26д-2",
    id_group="42",
)

LESSON = Lesson(
    id_lesson="3570",
    num_lesson="1",
    kind="ЛК",
    week_numbers="2, 4",
    teacher="доцент\nТестовий Викладач Іванович",
    id_fio="647",
    group="БЦІ-26д-2",
    dateles="08.09.2026",
    timebeg="09:00:00",
    timeend="10:20:00",
    discipline="Вища математика. Частина 1.",
)

DURING_LESSON = datetime(2026, 9, 8, 9, 5)

SCHEDULE_RESPONSE = (
    'ПОНЕДІЛОК|2|<tr data-id_lesson="3570" data-group="БЦІ-26д-2" data-numlesson="1"'
    ' data-lesson="Вища математика. Частина 1." data-id_fio="647" data-dateles="08.09.2026"'
    ' data-timebeg="09:00:00" data-timeend="10:20:00">'
    "<td>1</td><td>09:00-<br>10:20</td><td>ЛК</td><td>Вища математика. Частина 1.</td>"
    "<td>-</td><td>2, 4</td><td><i>доцент</i><br>Тестовий Викладач Іванович</td>"
    "<td>БЦІ-26д-2</td><td>401(ІП)</td></tr>"
)


def journal_with(fio: str) -> str:
    return (
        '<tr><td>1</td><td>' + fio + "</td><td>студент</td>"
        "<td>БЦІ-26д-2</td><td>09:06:11</td><td>1</td></tr>|"
    )


class FakeClient:
    def __init__(self, user=USER, schedule=SCHEDULE_RESPONSE, journal_text=None) -> None:
        self._user = user
        self._schedule = schedule
        self._journal = journal_text if journal_text is not None else journal_with(USER.fio)
        self.check_ins: list[Lesson] = []
        self.journal_calls: list[Lesson] = []
        self.schedule_calls = 0

    def check_access(self, year, semestr):
        return self._user

    def day_schedule(self, user, year, semestr, day):
        self.schedule_calls += 1
        return self._schedule

    def check_in(self, user, lesson):
        self.check_ins.append(lesson)
        return "ok"

    def journal(self, user, lesson):
        self.journal_calls.append(lesson)
        return self._journal


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / ".tt-checkin")


def collect_notifications():
    sent = []
    return sent, lambda title, message: sent.append((title, message))


def test_checks_in_and_marks_done_when_journal_confirms(store):
    client = FakeClient()
    run_tick(DURING_LESSON, client, store)
    assert [lesson.id_lesson for lesson in client.check_ins] == ["3570"]
    assert store.read_state()[LESSON.key] == LessonState.DONE


def test_marks_sent_and_notifies_when_journal_does_not_confirm(store):
    client = FakeClient(journal_text=journal_with("Хтось Інший Петрович"))
    sent, notifier = collect_notifications()
    run_tick(DURING_LESSON, client, store, notifier=notifier)
    assert store.read_state()[LESSON.key] == LessonState.SENT
    assert len(sent) == 1


def test_second_tick_does_not_resend_when_journal_already_has_the_row(store):
    store.write_state({LESSON.key: LessonState.SENT})
    store.write_plan(date(2026, 9, 8), [LESSON])
    client = FakeClient()
    run_tick(DURING_LESSON, client, store)
    assert client.check_ins == []
    assert client.journal_calls != []
    assert store.read_state()[LESSON.key] == LessonState.DONE


def test_schedule_is_fetched_once_and_then_read_from_cache(store):
    client = FakeClient()
    run_tick(DURING_LESSON, client, store)
    run_tick(DURING_LESSON, client, store)
    assert client.schedule_calls == 1


def test_done_lesson_is_not_checked_in_again(store):
    client = FakeClient()
    run_tick(DURING_LESSON, client, store)
    run_tick(datetime(2026, 9, 8, 9, 40), client, store)
    assert len(client.check_ins) == 1


def test_dead_token_notifies_and_does_nothing_else(store):
    client = FakeClient(user=None)
    sent, notifier = collect_notifications()
    summary = run_tick(DURING_LESSON, client, store, notifier=notifier)
    assert client.check_ins == []
    assert len(sent) == 1
    assert "login" in summary


def test_nothing_happens_outside_the_window(store):
    client = FakeClient()
    run_tick(datetime(2026, 9, 8, 8, 30), client, store)
    assert client.check_ins == []
    assert store.read_state() == {}


def test_every_tick_writes_a_log_line(store):
    run_tick(DURING_LESSON, FakeClient(), store)
    assert store.log_path.read_text(encoding="utf-8").strip() != ""


def test_token_never_appears_in_the_log(store):
    run_tick(DURING_LESSON, FakeClient(), store)
    store.write_token("СЕКРЕТНИЙ_ТОКЕН")
    assert "СЕКРЕТНИЙ_ТОКЕН" not in store.log_path.read_text(encoding="utf-8")


class FlakyClient(FakeClient):
    """Журнал підтверджує відмітку лише з `confirm_after`-го звернення."""

    def __init__(self, confirm_after: int) -> None:
        super().__init__(journal_text=journal_with("Хтось Інший Петрович"))
        self._confirm_after = confirm_after

    def journal(self, user, lesson):
        self.journal_calls.append(lesson)
        if len(self.journal_calls) >= self._confirm_after:
            return journal_with(USER.fio)
        return journal_with("Хтось Інший Петрович")


class OfflineClient(FakeClient):
    def check_access(self, year, semestr):
        raise NetworkError("немає мережі")


def test_check_in_is_retried_until_journal_confirms(store):
    client = FlakyClient(confirm_after=2)
    run_tick(DURING_LESSON, client, store)
    assert len(client.check_ins) == 2
    assert store.read_state()[LESSON.key] == LessonState.DONE


def test_retries_are_bounded_to_three_per_tick(store):
    client = FakeClient(journal_text=journal_with("Хтось Інший Петрович"))
    run_tick(DURING_LESSON, client, store)
    assert len(client.check_ins) == 3
    assert store.read_state()[LESSON.key] == LessonState.SENT


def test_sent_lesson_is_resent_while_the_window_is_open(store):
    store.write_state({LESSON.key: LessonState.SENT})
    store.write_plan(date(2026, 9, 8), [LESSON])
    client = FlakyClient(confirm_after=2)
    run_tick(DURING_LESSON, client, store)
    assert len(client.check_ins) == 1
    assert store.read_state()[LESSON.key] == LessonState.DONE


def test_sent_lesson_is_not_resent_after_the_window_closed(store):
    store.write_state({LESSON.key: LessonState.SENT})
    store.write_plan(date(2026, 9, 8), [LESSON])
    client = FakeClient(journal_text=journal_with("Хтось Інший Петрович"))
    run_tick(datetime(2026, 9, 8, 18, 0), client, store)
    assert client.check_ins == []
    assert store.read_state()[LESSON.key] == LessonState.SENT


def test_network_failure_is_survived_and_logged(store):
    summary = run_tick(DURING_LESSON, OfflineClient(), store)
    assert "мереж" in summary
    assert store.read_state() == {}
    assert store.log_path.read_text(encoding="utf-8").strip() != ""
```

- [ ] **Step 2: Запустити тести і переконатися, що падають**

Run: `python -m pytest tests/test_tick.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tt_checkin.tick'`

- [ ] **Step 3: Реалізувати нотифікації**

`tt_checkin/notify.py`:

```python
"""Нотифікація через Termux API. Якщо `termux-notification` недоступний
(запуск не на телефоні) — тихо нічого не робимо."""

from __future__ import annotations

import subprocess


def notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["termux-notification", "--title", title, "--content", message],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
```

- [ ] **Step 4: Реалізувати оркестрацію**

`tt_checkin/tick.py`:

```python
"""Один прохід: перевірити доступ, взяти план дня, відмітити те, що пора."""

from __future__ import annotations

from datetime import datetime

from .client import NetworkError
from .decide import LessonState, plan_tick, window_is_open
from .notify import notify
from .period import candidates
from .schedule import parse_day, parse_journal, registration_time
from .store import Store

MAX_ATTEMPTS = 3
DEAD_TOKEN_SUMMARY = "токен не працює — запусти `python -m tt_checkin login`"
NO_NETWORK_SUMMARY = "немає мережі — наступний тік повторить"


def _resolve_access(client, today):
    for year, semestr in candidates(today):
        user = client.check_access(year, semestr)
        if user is not None:
            return user, year, semestr
    return None, None, None


def _load_lessons(client, store: Store, user, year, semestr, today):
    cached = store.read_plan(today)
    if cached is not None:
        return cached
    day = parse_day(client.day_schedule(user, year, semestr, today))
    store.write_plan(today, day.lessons)
    return day.lessons


def _confirm(client, user, lesson) -> str | None:
    """Час нашої відмітки в журналі, або `None`, якщо нас там немає."""
    rows = parse_journal(client.journal(user, lesson))
    return registration_time(rows, user.fio)


def _check_in(client, user, lesson) -> str | None:
    """Відмітка з підтвердженням. Не більше MAX_ATTEMPTS спроб за один тік,
    щоб зламаний сервер не накрутив «Завантаження» до сотні."""
    for _ in range(MAX_ATTEMPTS):
        client.check_in(user, lesson)
        confirmed_at = _confirm(client, user, lesson)
        if confirmed_at:
            return confirmed_at
    return None


def run_tick(now: datetime, client, store: Store, notifier=notify) -> str:
    today = now.date()

    try:
        user, year, semestr = _resolve_access(client, today)
    except NetworkError as error:
        store.log(f"{NO_NETWORK_SUMMARY} ({error})")
        return NO_NETWORK_SUMMARY

    if user is None:
        store.log(DEAD_TOKEN_SUMMARY)
        notifier("tt-checkin", DEAD_TOKEN_SUMMARY)
        return DEAD_TOKEN_SUMMARY

    try:
        lessons = _load_lessons(client, store, user, year, semestr, today)
    except NetworkError as error:
        store.log(f"{NO_NETWORK_SUMMARY} ({error})")
        return NO_NETWORK_SUMMARY

    state = store.read_state()
    plan = plan_tick(now, lessons, state)

    done: list[str] = []
    stuck: list[str] = []

    for lesson in plan.to_verify:
        try:
            confirmed_at = _confirm(client, user, lesson)
            if not confirmed_at and window_is_open(now, lesson):
                # Стан `sent`, у журналі нас немає, вікно ще відкрите — значить
                # попередня відправка не дійшла. Надсилаємо ще раз.
                confirmed_at = _check_in(client, user, lesson)
        except NetworkError as error:
            store.log(f"пара {lesson.num_lesson}: мережа впала ({error})")
            continue
        if confirmed_at:
            state[lesson.key] = LessonState.DONE
            done.append(f"{lesson.num_lesson}:{confirmed_at}")
        else:
            stuck.append(lesson.num_lesson)

    for lesson in plan.to_check_in:
        try:
            confirmed_at = _check_in(client, user, lesson)
        except NetworkError as error:
            # Невідомо, чи запит дійшов до сервера, тому позначаємо `sent`:
            # наступний тік спершу подивиться в журнал, а не надішле вдруге.
            state[lesson.key] = LessonState.SENT
            store.log(f"пара {lesson.num_lesson}: мережа впала ({error})")
            continue
        if confirmed_at:
            state[lesson.key] = LessonState.DONE
            done.append(f"{lesson.num_lesson}:{confirmed_at}")
        else:
            state[lesson.key] = LessonState.SENT
            stuck.append(lesson.num_lesson)

    store.write_state(state)

    summary = f"пар у плані: {len(lessons)}; підтверджено: {done or '—'}"
    if stuck:
        summary += f"; без підтвердження: {stuck}"
        notifier("tt-checkin", f"відмітка без підтвердження: пари {stuck}")
    store.log(summary)
    return summary
```

- [ ] **Step 5: Запустити тести і переконатися, що проходять**

Run: `python -m pytest tests/test_tick.py -v`
Expected: PASS (14 тестів)

- [ ] **Step 6: Прогнати всю збірку**

Run: `python -m pytest -v`
Expected: PASS (усі тести з Task 1–7)

- [ ] **Step 7: Коміт**

```bash
git add tt_checkin/notify.py tt_checkin/tick.py tests/test_tick.py
git commit -m "feat: оркестрація тіку з підтвердженням через журнал"
```

---

### Task 8: CLI, запуск у Termux, README

**Files:**
- Create: `tt_checkin/__main__.py`
- Create: `tick.sh`
- Create: `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: усе з Task 2–7.
- Produces: команди `login`, `tick`, `status`; функція
  `build_client(store: Store) -> TimeTableClient`.

- [ ] **Step 1: Написати падаючий тест**

`tests/test_cli.py`:

```python
from datetime import date

import pytest

from tt_checkin.__main__ import cmd_status
from tt_checkin.decide import LessonState
from tt_checkin.model import Lesson
from tt_checkin.store import Store

LESSON = Lesson(
    id_lesson="3570",
    num_lesson="1",
    kind="ЛК",
    week_numbers="2, 4",
    teacher="доцент\nТестовий Викладач Іванович",
    id_fio="647",
    group="БЦІ-26д-2",
    dateles="08.09.2026",
    timebeg="09:00:00",
    timeend="10:20:00",
    discipline="Вища математика. Частина 1.",
)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / ".tt-checkin")


def test_status_reports_each_lesson_with_its_state(store, capsys):
    store.write_plan(date(2026, 9, 8), [LESSON])
    store.write_state({LESSON.key: LessonState.DONE})
    cmd_status(store, date(2026, 9, 8))
    out = capsys.readouterr().out
    assert "09:00:00" in out
    assert "Вища математика" in out
    assert "done" in out


def test_status_says_there_is_no_plan_yet(store, capsys):
    cmd_status(store, date(2026, 9, 8))
    assert "плану на цей день немає" in capsys.readouterr().out


def test_status_never_prints_the_token(store, capsys):
    store.write_token("СЕКРЕТНИЙ_ТОКЕН")
    store.write_plan(date(2026, 9, 8), [LESSON])
    cmd_status(store, date(2026, 9, 8))
    assert "СЕКРЕТНИЙ_ТОКЕН" not in capsys.readouterr().out
```

- [ ] **Step 2: Запустити тест і переконатися, що падає**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tt_checkin.__main__'`

- [ ] **Step 3: Реалізувати CLI**

`tt_checkin/__main__.py`:

```python
"""CLI: `login`, `tick`, `status`."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from getpass import getpass

import requests

from .client import TimeTableClient
from .decide import LessonState
from .period import candidates
from .store import Store
from .tick import run_tick

LOGIN_HINT = "логін — це корпоративна пошта, наприклад ipz-23d-123@snu.edu.ua"


def build_client(store: Store) -> TimeTableClient:
    client = TimeTableClient(requests.Session())
    token = store.read_token()
    if token:
        client.set_token(token)
    return client


def cmd_login(store: Store) -> int:
    print(LOGIN_HINT)
    login = input("логін: ").strip()
    password = getpass("пароль: ")

    client = TimeTableClient(requests.Session())
    client.login(login, password)
    del password

    today = date.today()
    for year, semestr in candidates(today):
        user = client.check_access(year, semestr)
        if user is None:
            continue
        token = client.token
        if not token:
            print("вхід пройшов, але сервер не віддав cookie tt_tokin", file=sys.stderr)
            return 1
        store.write_token(token)
        store.log(f"успішний вхід: {user.group}, період {year}/{semestr}")
        print(f"готово: {user.fio}, група {user.group}, період {year} семестр {semestr}")
        return 0

    print("вхід не вдався: перевір логін і пароль", file=sys.stderr)
    return 1


def cmd_tick(store: Store) -> int:
    if store.read_token() is None:
        print("немає токена — спочатку `python -m tt_checkin login`", file=sys.stderr)
        return 1
    print(run_tick(datetime.now(), build_client(store), store))
    return 0


def cmd_status(store: Store, day: date) -> int:
    lessons = store.read_plan(day)
    if not lessons:
        print(f"плану на цей день немає: {day.isoformat()}")
        return 0

    state = store.read_state()
    print(f"розклад на {day.isoformat()}:")
    for lesson in lessons:
        current = state.get(lesson.key, LessonState.PENDING)
        print(
            f"  {lesson.num_lesson}. {lesson.timebeg}–{lesson.timeend} "
            f"{lesson.discipline} [{lesson.kind}] — {current}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tt_checkin")
    parser.add_argument(
        "command", choices=["login", "tick", "status"], help="що зробити"
    )
    args = parser.parse_args(argv)

    store = Store()
    if args.command == "login":
        return cmd_login(store)
    if args.command == "tick":
        return cmd_tick(store)
    return cmd_status(store, date.today())


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Запустити тест і переконатися, що проходить**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS (3 тести)

- [ ] **Step 5: Створити скрипт запуску**

`tick.sh`:

```sh
#!/data/data/com.termux/files/usr/bin/sh
# Викликається termux-job-scheduler. Тік має бути тихим: усе цікаве пише в лог.
cd "$(dirname "$0")" || exit 1
mkdir -p "$HOME/.tt-checkin"
exec python -m tt_checkin tick >> "$HOME/.tt-checkin/cron.out" 2>&1
```

Зробити виконуваним:

```bash
chmod +x tick.sh
```

- [ ] **Step 6: Написати README**

`README.md`:

```markdown
# tt-checkin

Автоматична відмітка відвідування в TimeTable SNU. Дизайн і причини рішень —
у `docs/superpowers/specs/2026-09-08-tt-checkin-design.md`.

## Встановлення в Termux

    pkg install python termux-api
    pip install requests beautifulsoup4
    python -m tt_checkin login

`login` запитає логін (корпоративна пошта) і пароль. Пароль на диск не
зберігається — лишається тільки cookie `tt_tokin` у `~/.tt-checkin/token`.

## Розписання

    termux-job-scheduler --script ~/tt-checkin/tick.sh \
        --period-ms 1800000 --persisted true

Далі — вимкнути оптимізацію батареї для Termux у налаштуваннях Android,
інакше система приспить job-scheduler і тіки перестануть приходити.

## Перевірка

    python -m tt_checkin status   # план на сьогодні і стан кожної пари
    python -m tt_checkin tick     # один прохід вручну
    tail -f ~/.tt-checkin/log.txt

## Тести

    pip install pytest
    python -m pytest
```

- [ ] **Step 7: Прогнати всі тести**

Run: `python -m pytest -v`
Expected: PASS (усі тести проєкту)

- [ ] **Step 8: Коміт**

```bash
git add tt_checkin/__main__.py tick.sh README.md tests/test_cli.py
git commit -m "feat: CLI, скрипт для job-scheduler і README"
```

---

## Живий димовий тест (виконує користувач)

Автоматизовано це перевірити неможливо: потрібен реальний вхід, а пароль
вводить власник акаунта. Порядок:

1. `python -m tt_checkin login` — має напис на кшталт
   `готово: <ПІП>, група <група>, період 2026/2027 семестр 1`.
2. `python -m tt_checkin status` — має показати сьогоднішні пари з їхнім часом.
3. `python -m tt_checkin tick` під час пари — має напис
   `підтверджено: ['1:09:04:12']`.
4. Відкрити журнал заняття у браузері й переконатися, що рядок з'явився,
   а «Завантаження» дорівнює `1`, а не більше.

Якщо крок 3 дає `без підтвердження`, значить сервер не прийняв якесь із
display-полів (`lesson`, `type`, `fioTeacher`) — тоді треба порівняти наш
payload із перехопленим справжнім запитом у DevTools і вирівняти різницю.
