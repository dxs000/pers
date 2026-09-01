"""Применение схемы. Отдельная команда, потому что делать это придётся ещё не раз.

Здесь стояло, что окно бесплатного `--reset` закрывается с первым живым
разговором на Postgres. **Это оказалось неверно и исправлено на Шаге 22** —
роадмап поправлен тогда же, а докстринг прожил в старой редакции до Шага 28,
и это ровно тот разряд долга, который проект считает частью шага, а не
отчётом о нём. Как есть на самом деле: содержательных данных в базе нет и до
конца проекта не будет, `--reset` бесплатен в любой момент, миграций нет и не
планируется. Цена смены формы существует, но она не в данных — в
`build_fixture` и в перерезке эталонов `golden/`.

Команды:
    uv run db.py --init            # схема в тестовую базу (по умолчанию)
    uv run db.py --init --prod     # схема в рабочую
    uv run db.py --reset           # снести и накатить заново (тестовая)
    uv run db.py --reset --prod    # то же для рабочей — потребует подтверждения
    uv run db.py --check           # какая схема стоит и совпадает ли с файлом

Предохранитель на рабочей базе — не формальность. `--reset` это `DROP SCHEMA
public CASCADE`, то есть ровно та команда, которой стирают память персонажа, и
отличается она от безобидной одним словом в строке.
"""

import argparse
import sys
from pathlib import Path

import psycopg

import config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Таблицы, которые схема обязана создать. Список короткий и держится руками:
# автоматическая сверка со схемой проверяла бы файл сам с собой.
EXPECTED_TABLES = {
    "objects", "aliases", "assertions", "episodes",
    "sessions", "messages", "agent", "inbox",
}

# Колонки-признаки: их отсутствие — самый частый способ узнать базу, поднятую
# по старой схеме. Подсказка живёт РЯДОМ с колонкой, а не общим абзацем под
# списком. Абзац тут стоял, и на Шаге 28 он немедленно соврал: колонок стало
# три, а текст по-прежнему объяснял историю про `lower()` — то есть уводил в
# сторону тем увереннее, чем меньше человек знает схему.
EXPECTED_COLUMNS = {
    ("objects", "label_norm"):
        "Похоже на схему до отказа от SQL-lower(). Матчинг по псевдониму "
        "на такой базе молча не работает под локалью C.",
    ("aliases", "alias_norm"):
        "То же самое: нормализованная форма считается в Python "
        "(snapshot.norm_name), и база обязана иметь куда её класть.",
    ("inbox", "reply_id"):
        "Схема до Шага 28. Очередь на такой базе работает, но ход упадёт "
        "на вставке, а клиент не отличит «ответ в другой строке» от "
        "«ответа не будет».",
}


def _connect(prod: bool):
    return psycopg.connect(config.require_dsn(test=not prod), autocommit=True)


def _label(prod: bool) -> str:
    return "РАБОЧАЯ" if prod else "тестовая"


def cmd_check(prod: bool) -> int:
    """Что стоит в базе и похоже ли это на текущую схему."""
    with _connect(prod) as conn:
        tables = {
            r[0] for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
        columns = {
            (r[0], r[1]) for r in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            ).fetchall()
        }
        tz = conn.execute("SHOW timezone").fetchone()[0]

    print(f"база: {_label(prod)}")
    print(f"пояс сессии: {tz}" + ("" if tz.upper() == "UTC" else
          "   ← не UTC; код это переопределяет на соединении, но psql покажет иначе"))

    if not tables:
        print("схемы нет вовсе — `--init`")
        return 1

    missing_t = EXPECTED_TABLES - tables
    missing_c = {c for c in EXPECTED_COLUMNS if c not in columns}

    if missing_t:
        print(f"НЕТ ТАБЛИЦ: {', '.join(sorted(missing_t))}")
    if missing_c:
        print("НЕТ КОЛОНОК:")
        for column in sorted(missing_c):
            print(f"  {column[0]}.{column[1]} — {EXPECTED_COLUMNS[column]}")
        print("  Нужен `--reset`: миграций в проекте нет и не планируется.")

    if missing_t or missing_c:
        return 1
    # Список колонок печатается ИЗ константы, а не пересказывается словами.
    # Здесь стояло «обе norm-колонки есть» — верно ровно до Шага 28, который
    # добавил третью (`inbox.reply_id`) и оставил слово «обе». Проверка была
    # честной, отчёт о ней — нет, и заметить это можно было только чтением
    # кода. Формулировка, считающая себя сама, второй раз так не сможет.
    print(f"схема на месте: {len(tables)} таблиц, "
          f"колонки-признаки ({len(EXPECTED_COLUMNS)}): "
          + ", ".join(f"{t}.{c}" for t, c in sorted(EXPECTED_COLUMNS)))
    return 0


def cmd_apply(prod: bool, reset: bool, yes: bool) -> int:
    if reset and prod and not yes:
        print("Отказ: `--reset --prod` сносит РАБОЧУЮ базу вместе с памятью.")
        print("Если это правда то, что нужно, добавьте --yes.")
        return 1

    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with _connect(prod) as conn:
        if reset:
            print(f"сношу схему: {_label(prod)}")
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        conn.execute(sql)
        # Пояс базы — чтобы `psql` показывал то же, что видит код. Код и так
        # ставит UTC на соединении (store_pg.connect), но человек, зашедший
        # руками, иначе увидит другие метки времени и решит, что нашёл баг.
        conn.execute(
            f"ALTER DATABASE {conn.info.dbname} SET timezone TO 'UTC'"
        )
    print(f"схема применена: {_label(prod)}, пояс базы = UTC")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Применение схемы Postgres.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--init", action="store_true", help="накатить схему")
    mode.add_argument("--reset", action="store_true", help="снести и накатить заново")
    mode.add_argument("--check", action="store_true", help="сверить, что стоит в базе")
    p.add_argument("--prod", action="store_true",
                   help="рабочая база вместо тестовой (DATABASE_URL)")
    p.add_argument("--yes", action="store_true", help="подтвердить --reset --prod")
    args = p.parse_args()

    try:
        if args.check:
            return cmd_check(args.prod)
        return cmd_apply(args.prod, args.reset, args.yes)
    except RuntimeError as err:      # нет DSN / тестовая совпала с рабочей
        print(err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
