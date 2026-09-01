"""Миграции схемы: применить, сверить, принять существующую базу (Шаг 34).

## Почему миграции появились именно сейчас

До этого шага здесь стояло прямо противоположное, и стояло не по недосмотру:

    содержательных данных в базе нет и до конца проекта не будет,
    `--reset` бесплатен в любой момент, миграций нет и не планируется

Это было верно, пока база держала отладочное состояние: цена смены формы
лежала не в данных, а в `build_fixture` и в перерезке эталонов. Утверждение
перестаёт быть верным ровно в тот день, когда персонаж начинает жить в фоне
и накапливать биографию. С этого дня `--reset --prod` — не «поднять заново»,
а стереть единственный экземпляр того, что не выводится ни из чего: эпизоды
выводимы из `messages`, а `messages` — ни из чего.

Дальше схему предстоит резать много раз (события жизни, черты с весами,
импульсы, внутренняя речь), и каждый раз без миграций означал бы выбор
между «не менять форму» и «убить персонажа». Поэтому шаг делается ДО первой
такой правки, а не после.

## Устройство

Миграция — файл `migrations/NNNN_имя.sql`. Порядок задаётся номером, а не
временем изменения файла: время файла не переживает `git clone`.

Журнал — таблица `schema_migrations`. Её создаёт этот модуль, а не миграция:
журнал нужен ДО того, как можно записать хоть одну строку о применении, и
миграция, заводящая собственный учёт, была бы курицей из анекдота про яйцо.

**Контрольная сумма — половина смысла журнала.** Список применённых имён
отвечает на «что накатывали», но не на «то ли самое накатывали». Правка уже
применённого файла — самый обычный способ развести код и базу, и разводит
она их молча: на машине автора всё работает, потому что там база помнит
старую редакцию, а на чистой накатывается новая. Сумма превращает это в
строку отчёта.

**Каждая миграция — своя транзакция, и журнал пишется В НЕЙ ЖЕ.** Postgres
умеет транзакционный DDL, и это не мелочь: упавшая посередине миграция
откатывается целиком вместе со своей записью в журнале. Без этого возможно
худшее из состояний — половина применена, а журнал говорит «применена вся»,
и следующий прогон пропустит недоделанное.

## Принятие живой базы

`--stamp` записывает миграции как применённые, НЕ выполняя их. Нужен ровно
один раз и ровно для одного случая: база уже поднята прежним `--init` из
`schema.sql`, схема в ней та самая, а журнала нет, потому что журнала тогда
не существовало. Накатить `0001` на неё нельзя (упадёт на `CREATE TABLE`),
а без записи в журнале она навсегда останется «непонятно какой».

Пустую базу `--stamp` штамповать отказывается. Это не вежливость: штамп на
пустой базе — запись в журнал о работе, которой не было, и обнаружится она
только следующей миграцией, упавшей на отсутствующей таблице.

Команды:
    uv run db.py --check            # что применено, что ждёт (тестовая)
    uv run db.py --check --prod     # то же для рабочей
    uv run db.py --init             # накатить недостающие
    uv run db.py --init --prod      # то же для рабочей
    uv run db.py --stamp            # принять существующую базу без выполнения
    uv run db.py --reset            # снести и накатить заново (тестовая)
    uv run db.py --reset --prod     # то же для рабочей — потребует --yes

Предохранитель на рабочей базе — не формальность. `--reset` это `DROP SCHEMA
public CASCADE`, то есть ровно та команда, которой стирают память персонажа,
и отличается она от безобидной одним словом в строке.
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

import psycopg

import config

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# Как Postgres пишет UTC. Имён несколько, смысл один.
_UTC_NAMES = {"UTC", "ETC/UTC", "UCT", "UNIVERSAL", "ZULU"}

# `NNNN_имя.sql`. Номер — четыре цифры, чтобы сортировка строк совпадала с
# числовой без разбора: `0010` идёт после `0009`, а `10` шло бы после `1`.
MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# Журнал заводится этим модулем, а не миграцией: см. докстринг.
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id         TEXT        PRIMARY KEY,
    name       TEXT        NOT NULL,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Штампованная миграция не выполнялась, и это надо отличать от
    -- выполненной: если следующая правка схемы вдруг не сойдётся, первый
    -- вопрос будет «а эта точно накатывалась или её приняли на веру».
    stamped    BOOLEAN     NOT NULL DEFAULT FALSE
)
"""


class Migration:
    """Файл миграции: номер, имя, текст, сумма."""

    def __init__(self, path: Path):
        match = MIGRATION_RE.match(path.name)
        if not match:
            raise RuntimeError(
                f"{path.name}: имя не по образцу NNNN_имя.sql. Порядок миграций "
                f"держится номером, и файл без номера встал бы в него наугад."
            )
        self.path = path
        self.id = match.group(1)
        self.name = path.name
        self.sql = path.read_text(encoding="utf-8")
        # sha256 ТЕКСТА файла, а не одного SQL. Комментарий здесь — носитель
        # решений наравне с кодом, и его правка в применённой миграции такой
        # же повод показать расхождение: файл, объясняющий схему иначе, чем
        # объяснял в момент применения, вводит в заблуждение ровно так же.
        self.checksum = hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return f"<{self.name}>"


def discover() -> list[Migration]:
    """Миграции с диска, по возрастанию номера. Дубль номера — ошибка.

    Два файла с одним номером — не «какой-нибудь да применится», а
    неопределённый порядок, который на разных машинах разрешится по-разному.
    Ловится здесь, а не в базе, потому что база про файлы не знает.
    """
    if not MIGRATIONS_DIR.is_dir():
        raise RuntimeError(f"нет каталога миграций: {MIGRATIONS_DIR}")
    found = [Migration(p) for p in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    seen: dict[str, str] = {}
    for m in found:
        if m.id in seen:
            raise RuntimeError(
                f"номер {m.id} занят дважды: {seen[m.id]} и {m.name}"
            )
        seen[m.id] = m.name
    if not found:
        raise RuntimeError(f"в {MIGRATIONS_DIR} нет ни одной миграции")
    return found


def _connect(prod: bool):
    return psycopg.connect(config.require_dsn(test=not prod), autocommit=True)


def _label(prod: bool) -> str:
    return "РАБОЧАЯ" if prod else "тестовая"


def _ledger(conn) -> dict[str, dict]:
    """Что записано в журнале. Журнала нет — пустой словарь, а не ошибка."""
    conn.execute(LEDGER_DDL)
    rows = conn.execute(
        "SELECT id, name, checksum, stamped FROM schema_migrations ORDER BY id"
    ).fetchall()
    return {r[0]: {"name": r[1], "checksum": r[2], "stamped": r[3]} for r in rows}


def _tables(conn) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    }


def _virgin(conn) -> bool:
    """Пуста ли база. `schema_migrations` не в счёт — её заводим мы сами."""
    return not (_tables(conn) - {"schema_migrations"})


def _drifted(files: list[Migration], done: dict[str, dict]) -> list[str]:
    """Применённые миграции, разошедшиеся с файлами. Две беды, не одна.

    Совпал номер, но не имя — номер переиспользовали: на диске лежит ДРУГАЯ
    миграция, а журнал считает её той. Лечится переименованием файла в
    свободный номер, и говорить надо именно это.

    Совпало имя, но не сумма — файл правили после применения. Лечится
    откатом правки либо новой миграцией поверх, и это уже другой разговор.

    Одним сообщением на оба случая было бы удобно писать и вредно читать:
    «файл изменён после применения» про файл, который никто не менял,
    отправляет искать несуществующую правку.
    """
    out = []
    for m in files:
        row = done.get(m.id)
        if not row:
            continue
        if row["name"] != m.name:
            out.append(
                f"  номер {m.id} в журнале за {row['name']}, "
                f"а на диске {m.name}\n"
                f"    Это другая миграция под занятым номером — дайте ей свободный."
            )
        elif row["checksum"] != m.checksum:
            out.append(
                f"  {m.name} — файл изменён после применения\n"
                f"    в журнале {row['checksum'][:12]}…, на диске {m.checksum[:12]}…"
            )
    return out


def _apply_one(conn, m: Migration, stamped: bool = False) -> None:
    """Одна миграция и запись о ней — ОДНОЙ транзакцией. См. докстринг модуля."""
    with conn.transaction():
        if not stamped:
            conn.execute(m.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id, name, checksum, stamped) "
            "VALUES (%s, %s, %s, %s)",
            (m.id, m.name, m.checksum, stamped),
        )


# =============================================================================
# Команды
# =============================================================================
def cmd_check(prod: bool) -> int:
    files = discover()
    with _connect(prod) as conn:
        done = _ledger(conn)
        virgin = _virgin(conn)
        tables = _tables(conn) - {"schema_migrations"}
        tz = conn.execute("SHOW timezone").fetchone()[0]

    print(f"база: {_label(prod)}")
    # `Etc/UTC` — это UTC, и предупреждать о нём не о чем. Сравнение шло по
    # одной строке и на свежем кластере (где именно `Etc/UTC` стоит по
    # умолчанию) ругалось зря — а предупреждение, кричащее на исправной
    # настройке, приучает не читать предупреждения вообще.
    print(f"пояс сессии: {tz}" + ("" if tz.upper() in _UTC_NAMES else
          "   ← не UTC; код это переопределяет на соединении, но psql покажет иначе"))

    # База со схемой, но без журнала — та самая, ради которой есть `--stamp`.
    # Названа отдельно, потому что «ждут все миграции» на ней читалось бы как
    # «база пустая», и человек накатил бы `--init` на существующие таблицы.
    if not done and not virgin:
        print("схема есть, журнала нет — это база, поднятая до Шага 34.")
        print("  Накатывать нечего и нельзя: `--init` упадёт на CREATE TABLE.")
        print("  Принять её как есть: `db.py --stamp`" + (" --prod" if prod else ""))
        return 1

    pending = [m for m in files if m.id not in done]
    drift = _drifted(files, done)
    # Запись в журнале без файла: миграцию удалили или переименовали. Это не
    # придирка — база в состоянии, которое не воспроизводится из репозитория,
    # то есть чистый клон соберёт ДРУГУЮ схему и никто этого не заметит.
    orphans = [row["name"] for mid, row in done.items()
               if mid not in {m.id for m in files}]

    applied = [m for m in files if m.id in done]
    for m in applied:
        mark = " (штамп)" if done[m.id]["stamped"] else ""
        print(f"  применена  {m.name}{mark}")
    for m in pending:
        print(f"  ЖДЁТ       {m.name}")

    if drift:
        print("РАСХОЖДЕНИЕ С ЖУРНАЛОМ:")
        print("\n".join(drift))
    if orphans:
        print("В ЖУРНАЛЕ ЕСТЬ, НА ДИСКЕ НЕТ: " + ", ".join(sorted(orphans)))
        print("  Схема этой базы из репозитория не воспроизводится.")

    if drift or orphans:
        return 1
    if pending:
        print(f"ждут применения: {len(pending)} — `db.py --init`"
              + (" --prod" if prod else ""))
        return 1
    print(f"схема на месте: применено {len(applied)} миграций, "
          f"таблиц в базе {len(tables)}")
    return 0


def cmd_init(prod: bool) -> int:
    files = discover()
    with _connect(prod) as conn:
        done = _ledger(conn)

        if not done and not _virgin(conn):
            print("Отказ: схема есть, а журнала нет.")
            print("Это база до Шага 34. `--init` упал бы на CREATE TABLE; "
                  "принять её надо штампом: `db.py --stamp`"
                  + (" --prod" if prod else ""))
            return 1

        drift = _drifted(files, done)
        if drift:
            # Накатывать поверх расхождения нельзя: следующая миграция
            # рассчитана на схему, которую описывает ТЕКУЩИЙ файл, а в базе
            # лежит результат прежнего. Молчаливое продолжение здесь дороже
            # отказа — оно даёт схему, которой нет ни в одной редакции.
            print("Отказ: применённые миграции разошлись с файлами.")
            print("\n".join(drift))
            return 1

        pending = [m for m in files if m.id not in done]
        if not pending:
            print(f"нечего применять: все {len(files)} миграций уже в журнале")
            return 0

        applied_now = []
        for m in pending:
            print(f"применяю {m.name}")
            try:
                _apply_one(conn, m)
            except psycopg.Error as err:
                # Трассировка сюда не годится: она называет строку `db.py`, а
                # человеку нужно имя файла и состояние базы. Состояние здесь
                # определённое, и сказать его надо явно — ровно эта
                # определённость и есть то, ради чего каждая миграция идёт
                # своей транзакцией.
                print(f"ОШИБКА в {m.name}: {err}".rstrip())
                print(f"  Эта миграция откачена целиком, в журнал не записана.")
                if applied_now:
                    print("  Успели примениться: " + ", ".join(applied_now))
                    print("  Они В ЖУРНАЛЕ и повторно накатываться не будут.")
                else:
                    print("  Ничего применить не успели, база как была.")
                return 1
            applied_now.append(m.name)

        # Пояс базы — чтобы `psql` показывал то же, что видит код. Код и так
        # ставит UTC на соединении (store_pg.connect), но человек, зашедший
        # руками, иначе увидит другие метки времени и решит, что нашёл баг.
        conn.execute(f"ALTER DATABASE {conn.info.dbname} SET timezone TO 'UTC'")

    print(f"применено миграций: {len(pending)}; база: {_label(prod)}, пояс = UTC")
    return 0


def cmd_stamp(prod: bool, upto: str | None) -> int:
    files = discover()
    with _connect(prod) as conn:
        if _virgin(conn):
            print("Отказ: база пуста, штамповать нечего.")
            print("Штамп на пустой базе — запись о работе, которой не было; "
                  "обнаружилась бы она следующей миграцией. Нужен `--init`.")
            return 1

        done = _ledger(conn)
        limit = upto or files[0].id
        target = [m for m in files if m.id <= limit and m.id not in done]
        if not target:
            print(f"нечего штамповать до {limit} включительно")
            return 0

        for m in target:
            print(f"штампую (не выполняя) {m.name}")
            _apply_one(conn, m, stamped=True)

    print(f"принято как применённое: {len(target)}; база: {_label(prod)}")
    print("Проверьте `db.py --check`" + (" --prod" if prod else "")
          + ": оставшиеся миграции накатываются обычным `--init`.")
    return 0


def cmd_reset(prod: bool, yes: bool) -> int:
    if prod and not yes:
        print("Отказ: `--reset --prod` сносит РАБОЧУЮ базу вместе с памятью.")
        print("С Шага 34 это уже не «поднять заново»: биография персонажа "
              "не выводится ни из чего и восстановлению не подлежит.")
        print("Если это правда то, что нужно, добавьте --yes.")
        return 1

    with _connect(prod) as conn:
        print(f"сношу схему: {_label(prod)}")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    return cmd_init(prod)


def main() -> int:
    p = argparse.ArgumentParser(description="Миграции схемы Postgres.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="что применено, что ждёт, не разошлись ли суммы")
    mode.add_argument("--init", action="store_true", help="накатить недостающие")
    mode.add_argument("--stamp", nargs="?", const="", metavar="NNNN",
                      help="принять существующую базу без выполнения "
                           "(по умолчанию — только базовую миграцию)")
    mode.add_argument("--reset", action="store_true",
                      help="снести схему и накатить всё заново")
    p.add_argument("--prod", action="store_true",
                   help="рабочая база вместо тестовой (DATABASE_URL)")
    p.add_argument("--yes", action="store_true", help="подтвердить --reset --prod")
    args = p.parse_args()

    try:
        if args.check:
            return cmd_check(args.prod)
        if args.init:
            return cmd_init(args.prod)
        if args.stamp is not None:
            return cmd_stamp(args.prod, args.stamp or None)
        return cmd_reset(args.prod, args.yes)
    except RuntimeError as err:      # нет DSN / тестовая совпала с рабочей / файлы
        print(err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
