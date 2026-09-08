# tt-checkin

Автоматична відмітка відвідування в TimeTable SNU. Один файл, тільки
стандартна бібліотека Python — на телефоні нічого встановлювати не треба,
крім самого Python.

Дизайн і причини рішень: `docs/superpowers/specs/2026-09-08-tt-checkin-design.md`

## Встановлення в Termux

```
pkg install python termux-api
# скопіювати tt_checkin.py і tick.sh у ~/tt-checkin/
cd ~/tt-checkin && chmod +x tick.sh
python3 tt_checkin.py login
```

`login` запитає логін (корпоративна пошта) і пароль. Пароль на диск не
зберігається — лишається тільки cookie `tt_tokin` у `~/.tt-checkin/token`.

## Розписання

```
termux-job-scheduler --script ~/tt-checkin/tick.sh \
    --period-ms 1800000 --persisted true
```

Далі вимкнути оптимізацію батареї для Termux у налаштуваннях Android,
інакше система приспить job-scheduler і тіки перестануть приходити.

## Команди

```
python3 tt_checkin.py status              # план на сьогодні і стан пар
python3 tt_checkin.py tick                # один прохід
python3 tt_checkin.py tick --dry-run      # показати, нічого не надсилаючи
python3 tt_checkin.py tick --at "2026-09-09 09:05"   # підставити час
tail -f ~/.tt-checkin/log.txt
```

## Файли стану

`~/.tt-checkin/`: `token`, `plan-YYYY-MM-DD.json`, `state.json`, `log.txt`.
Каталог 0700, файли 0600.
