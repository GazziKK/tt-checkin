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
        if tag == "tr":
            self._flush_row()  # HTML5: новий <tr> неявно закриває попередній
            self._attrs = {name: (value or "") for name, value in attrs}
            self._cells = []
        elif tag == "td" and self._cells is not None:
            self._flush_cell()  # новий <td> неявно закриває попередній
            self._buf = []
        elif tag == "br" and self._buf is not None:
            self._buf.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "br" and self._buf is not None:
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
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
