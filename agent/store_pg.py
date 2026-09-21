"""Postgres-движок: читающая половина.

Задача была узкая и названа роадмапом: отдать **тот же `snapshot.Turn`**,
что отдавал JSON-движок. Не «переписать store», не «сделать ORM» — сойтись
побайтно на одиннадцати промптах и только потом идти дальше. Сошлось; на
Шаге 26 второй движок удалён, и этот остался единственным.

Пишущая половина (`upsert_object`, `_merge_assertions`, `close_session`)
здесь СОЗНАТЕЛЬНО отсутствует. Сбруя её не видит: промпт показывает, что
прочиталось, но не что записалось, и «записали неправильно, но одинаково
обоими движками» она пропустит. Делать запись под проверку, которая её не
проверяет, — это ровно тот стык, что трижды укусил на Шаге 12.

## Три вещи, которые Шаг 17 велел учесть, и как они учтены

1. **Два среза объектов.** `objects` — top-N по `effective_salience` с
   отсечкой по `SALIENCE_FLOOR`, считается в SQL и стал `ORDER BY`.
   `registry` — ПОЛНЫЙ список без `ORDER BY` и без `LIMIT`: сматчить с
   забытым объектом нужно, а забытый — не удалённый.
2. **Ассершены нормализуются.** `ts` приезжает объектом `datetime`, а в
   промпт рефлектора идёт дословным `json.dumps`. Поэтому `ts` тут же
   приводится к ISO-строке — в том же виде, в каком его писал JSON-движок.
   Без этого `json.dumps` упал бы исключением внутри промпта.
3. **Объект №0.** `self` — строка `objects` с `id = 0`, и его ассершены
   лежат в той же таблице. `merge_self_assertions` как отдельной функции
   больше нет: это `upsert` по `object_id = 0`.

## Формула затухания снова живёт в одном месте

`effective_salience` была и в SQL (функция в схеме), и в Python
(`store.effective_salience`) — роадмап называл это самым опасным местом
переезда, и страховкой служила сбруя: расхождение формул меняет набор
объектов в промпте, то есть **видно** в эталоне.

Со Шага 26 копии не стало вместе с движком: формула живёт только в схеме
и работает `ORDER BY`. Долг «правишь одну — правишь обе» закрыт не
дисциплиной, а вычитанием.
"""

import json

import psycopg
import psycopg.sql
from psycopg.rows import dict_row

import config

from snapshot import (SESSION_GAP_HOURS, WORKING_MEMORY_EXCHANGES, Turn, iso,
                      message, norm_name, normalize_assertion)

# Порог отсечки по важности. Был продублирован из `store` (движок не может
# зависеть от движка); с Шага 26 копия одна и живёт здесь.
SALIENCE_FLOOR = 0.05
THREADS_SNAPSHOT_LIMIT = 3
MEMORIES_LIMIT = 3
SELF_ID = 0

# Затухание побуждения. Форма та же, что у важности объектов
# (`effective_salience`), и намеренно: повод, о котором ничто не напоминает,
# слабеет так же, как объект, о котором не говорят. Период вдвое короче
# суток — новость живёт меньше, чем знакомство.
#
# Живёт ЗДЕСЬ, а не в `cycle`, потому что применяется внутри SQL-выражения
# `bump_impulse`. Порог, при котором персонаж решает заговорить, — наоборот в
# `cycle`: база хранит побуждение, решает цикл.
IMPULSE_DECAY_BASE = 0.5
IMPULSE_HALFLIFE_HOURS = 12.0

# Прибавка к весу за вспоминание и потолок (Шаг 40).
#
# **Это и есть механизм, которым события формируют характер, а не список
# черт.** Воспоминание, попавшее в промпт, чуть тяжелеет; о чём персонаж
# говорит — то всплывает чаще, и через десяток разговоров у него появляется
# ядро, которого никто не назначал. Не всплывающее уходит под воду, но НЕ
# удаляется: `all_memories` по-прежнему сверяет кандидата со всем каноном, и
# забытое противоречие остаётся противоречием.
#
# Потолок нужен не от переполнения, а от вырождения: без него первое же
# воспоминание, попавшее в тройку, набирало бы вес быстрее остальных просто
# потому, что оно уже в тройке. Положительная обратная связь без предела
# сходится к одной строке, и биография превращается в одну историю.
#
# Живут здесь, а не в `cycle`, по правилу `IMPULSE_DECAY_BASE`: применяются
# внутри SQL-выражения. Порог, при котором персонаж решает заговорить или
# уснуть, — наоборот в `cycle`.
RECALL_BUMP = 0.1
RECALL_CEILING = 3.0


def connect(dsn: str | None = None, *, test: bool = False,
            read_only: bool = False) -> psycopg.Connection:
    """Соединение. DSN берётся из `config`, а не из окружения напрямую.

    **`read_only=True` — соединение, которое база не пустит писать (Шаг 30).**
    `default_transaction_read_only` включается на сессии, и всякая попытка
    записи падает `ReadOnlySqlTransaction` (SQLSTATE 25006) — не «мы решили
    не писать», а «нам не дали». Так «один писатель памяти» перестаёт быть
    свойством внимательности читающей стороны и становится свойством её
    соединения; у `cli.py` (Шаг 29) это держалось тем, что фасад туда не
    импортирован, — верно, но проверяется только чтением кода.

    Почему параметр здесь, а не отдельная функция в `inspector`: правила
    подключения (пояс сессии, `autocommit`, фабрика строк) — одно место на
    проект, и вторая точка `psycopg.connect` немедленно стала бы копией,
    обязанной совпадать с первой. Таких копий проект вычистил уже три.

    Отдельной РОЛИ с `GRANT SELECT` тут нет намеренно. Роль — вещь
    кластерная, её не поднять миграцией (Шаг 34; до него — `schema.sql`),
    и нужна она с того момента,
    когда читатель перестанет быть нашим кодом (Node, Фаза 4b) и получит
    свои учётные данные. Это работа 4c вместе с аутентификацией; пока
    читатель свой, режим соединения даёт ту же гарантию дешевле.

    Через `config` — потому что там он проходит проверку: при `test=True`
    `require_dsn` не пустит, если тестовая база совпала с рабочей. Читать
    `os.environ` тут значило бы обойти единственный предохранитель, стоящий
    между `golden.py` и памятью персонажа.

    **`timezone=UTC` на сессии — не настройка вкуса, а исполнение правила
    проекта «хранение UTC, рендер локальный».** `TIMESTAMPTZ` хранит момент,
    но ОТДАЁТ его в поясе сессии, а пояс сессии берётся с сервера. Без этой
    строки `ts` приезжает как `+03:00` на московской машине и как `+00:00`
    на UTC-машине — тот же момент, другая строка. Для `humanize_age` разницы
    нет (математика моментов), но `_build_reflector_prompt` кладёт `ts` в
    промпт **дословным `json.dumps`**, и промпт начинает зависеть от
    настройки сервера. Поймано сбруей на первом же прогоне на чужой машине.

    **`autocommit=True` — не «коммитить почаще», а перенос границы в код.**
    Пока соединение жило в неявной транзакции, границей хода было место,
    где стоит единственный `commit`, — то есть её нельзя было прочитать,
    её приходилось выводить. Теперь по умолчанию каждый стейтмент сам себе
    единица, а всё, что обязано упасть вместе или не упасть вовсе,
    обёрнуто явным `with conn.transaction():` и видно глазами.

    Побочность, о которую легко удариться: `conn.commit()` под autocommit
    **молча ничего не делает** (`transaction_status == IDLE` — и выход), а
    внутри `with conn.transaction():` — бросает `ProgrammingError`. То есть
    забытый старый `commit()` не покраснеет, а притворится работающим.
    Поэтому на Шаге 23 они вычищены поимённо, а не оставлены «на всякий».
    """
    options = "-c timezone=UTC"
    if read_only:
        options += " -c default_transaction_read_only=on"
    return psycopg.connect(
        dsn or config.require_dsn(test=test),
        row_factory=dict_row,
        options=options,
        autocommit=True,
    )


def _assertions_by_object(conn, object_ids: list[int]) -> dict[int, list[dict]]:
    """Ассершены пачкой, а не запросом на объект.

    Порядок внутри объекта — `id`, то есть порядок вставки. Это ровно то,
    чем был порядок списка в JSON, и он значим: `_pick_assertions` режет по
    рангу, но ничьи разрешает порядком.
    """
    if not object_ids:
        return {}

    rows = conn.execute(
        """
        SELECT object_id, key, value, confidence, hits, ts, source, confirmed
          FROM assertions
         WHERE object_id = ANY(%s)
         ORDER BY object_id, id
        """,
        (object_ids,),
    ).fetchall()

    out: dict[int, list[dict]] = {}
    for row in rows:
        raw = dict(row)
        raw["ts"] = iso(raw["ts"])
        # `confirmed=False` выкидываем ДО нормализации: в JSON-движке флага
        # у неподтверждённого факта нет вовсе, а `normalize_assertion`
        # дописывает его только истинным. Пустое поле и отсутствующее поле —
        # в json.dumps разные вещи.
        if not raw.pop("confirmed", False):
            raw.pop("confirmed", None)
        else:
            raw["confirmed"] = True
        out.setdefault(row["object_id"], []).append(normalize_assertion(raw))
    return out


# Сколько побуждений видит разговор. Меньше, чем нитей: нить — дело, и их у
# человека много; побуждений, которые правда определяют день, два-три.
DRIVES_SNAPSHOT_LIMIT = 3


def _snapshot_drives(conn, now) -> list[dict]:
    import store_character
    return [{"kind": d["kind"], "text": d["text"]}
            for d in store_character.open_drives(conn, now, DRIVES_SNAPSHOT_LIMIT)]


def build_snapshot(conn, now, limit: int = 7) -> Turn:
    """Снимок хода из базы. Та же форма, что у `store.build_snapshot`.

    Шесть запросов на ход, и это осознанно: агрегировать всё в один
    `JOIN` значило бы собирать снимок в SQL, а он собирается в Python —
    иначе `mind` окажется зависим от формы запроса.
    """
    agent = conn.execute(
        """
        SELECT name, born_at, birthplace, traits, mood, mood_reason, mood_since,
               place_label, outside_latch
          FROM agent WHERE id = 1
        """
    ).fetchone() or {}

    # --- срез 1: top-N по важности, отсечка и порядок — в SQL ---------------
    top = conn.execute(
        """
        SELECT id, type, label, last_seen,
               effective_salience(salience, last_seen, %s) AS eff
          FROM objects
         WHERE id <> %s
           AND effective_salience(salience, last_seen, %s) >= %s
         ORDER BY eff DESC, id
         LIMIT %s
        """,
        (now, SELF_ID, now, SALIENCE_FLOOR, limit),
    ).fetchall()

    # --- срез 2: ПОЛНЫЙ реестр для матчинга ---------------------------------
    registry_rows = conn.execute(
        """
        SELECT o.id, o.label,
               -- ORDER BY a.id, а НЕ a.alias: порядок псевдонимов есть
               -- порядок появления, и он доезжает до промпта экстрактора.
               coalesce(array_agg(a.alias ORDER BY a.id)
                        FILTER (WHERE a.alias IS NOT NULL), '{}') AS aliases
          FROM objects o
          LEFT JOIN aliases a ON a.object_id = o.id
         WHERE o.id <> %s
         GROUP BY o.id, o.label
         ORDER BY o.id
        """,
        (SELF_ID,),
    ).fetchall()

    episodes = conn.execute(
        """
        SELECT id, started_at, ended_at, exchanges, summary
          FROM episodes
         ORDER BY ended_at, id
        """
    ).fetchall()

    # Правило отбора ЗАВЕДЕНО (Шаг 40), и прежняя оговорка снята вместе с
    # причиной, по которой она стояла. Стояла она так: затухания у
    # воспоминаний нет, писателя `weight` не существует, все значения
    # приходят из фикстура, и потому `ORDER BY weight DESC` детерминирован и
    # безобиден. Сон отменил каждое из трёх утверждений в один шаг.
    #
    # `recall_score` живёт в схеме (`0005_dream.sql`) — по тому же правилу,
    # по которому там живёт `effective_salience`: величина пересчитывается
    # там, где лежит, иначе между чтением и записью появляется щель.
    #
    # Вторым и третьим ключом — `happened_at, id`, а не только `id`: при
    # равных счётах (а они равны у всего, что ни разу не вспоминали в одну
    # секунду записи) порядок обязан быть порядком жизни, а не порядком
    # вставки. Разница видна ровно на чистом старте, где равны все.
    memories = conn.execute(
        """
        SELECT id, happened_at, precision, text, source, weight
          FROM memories
         ORDER BY recall_score(weight, last_recalled, created_at, %s) DESC,
                  happened_at, id
         LIMIT %s
        """,
        (now, MEMORIES_LIMIT),
    ).fetchall()

    by_object = _assertions_by_object(conn, [r["id"] for r in top] + [SELF_ID])

    return Turn(
        name=agent.get("name"),
        born_at=iso(agent.get("born_at")),
        birthplace=agent.get("birthplace"),
        traits=tuple(agent.get("traits") or ()),
        mood=agent.get("mood", "нейтральное"),
        mood_reason=agent.get("mood_reason"),
        mood_since=iso(agent.get("mood_since")),
        place_label=agent.get("place_label"),
        self_assertions=by_object.get(SELF_ID, []),
        outside_latch=agent.get("outside_latch"),
        threads=open_threads(conn, "self", THREADS_SNAPSHOT_LIMIT),
        reading=current_reading(conn),
        drives=_snapshot_drives(conn, now),
        episodes=[
            {
                "id": f"ep_{e['id']}",
                "started_at": iso(e["started_at"]),
                "ended_at": iso(e["ended_at"]),
                "exchanges": e["exchanges"],
                "summary": e["summary"],
            }
            for e in episodes
        ],
        objects=[
            {
                "id": f"obj_{o['id']}",
                "type": o["type"],
                "label": o["label"],
                "last_seen": iso(o["last_seen"]) or "",
                "assertions": by_object.get(o["id"], []),
            }
            for o in top
        ],
        registry=[
            {"id": f"obj_{r['id']}", "label": r["label"], "aliases": list(r["aliases"])}
            for r in registry_rows
        ],
        memories=[
            {
                "id": f"mem_{m['id']}",
                "happened_at": iso(m["happened_at"]),
                "precision": m["precision"],
                "text": m["text"],
                "source": m["source"],
                "weight": m["weight"],
            }
            for m in memories
        ],
    )


# =============================================================================
# Загрузка фикстура. ТОЛЬКО для сбруи — это не пишущая половина движка
# =============================================================================
def load_fixture(conn, state: dict) -> None:
    """Залить снимок-фикстур в пустую базу, чтобы было с чем сверяться.

    Живёт здесь, а не в `store_pg` как рабочий код, и не в `golden.py`:
    это тестовый загрузчик, знающий и форму фикстура, и форму схемы.
    Продакшн-пути через него не идут — в жизни база наполняется записью.

    Идентификаторы `obj_N` разбираются в числа: в JSON id был строкой с
    префиксом, в базе это `BIGINT`. Префикс — свойство рендера, и он
    возвращается на выходе `build_snapshot`.

    **Граница явная, потому что под autocommit её не стало.** Раньше
    заливка держалась на `conn.commit()` в конце: до него ничего не было
    видно, падение посреди откатывало всё. Теперь каждый `execute` сам
    себе транзакция, и без обёртки TRUNCATE фиксировался бы отдельно от
    того, что за ним, — то есть сорванная заливка оставляла бы сбрую на
    пустой базе, а сценарий краснел бы не на своей причине.
    """
    with conn.transaction():
        _fill_fixture(conn, state)


def all_memories(conn) -> list[dict]:
    """ВСЁ записанное, в порядке жизни. Для биографа и сверки (Шаг 38).

    Отличается от среза в `build_snapshot` тем, что здесь нет `LIMIT`, и это
    существенно. Снимок отдаёт то, что персонаж СЕЙЧАС вспомнил, — три штуки,
    отобранные по весу. Сверять кандидата надо со всем: противоречить он
    может и тому, чего персонаж в эту минуту не вспомнил, а забытое
    противоречие остаётся противоречием.

    Предела нет намеренно и до поры. Пока биография — десятки строк, весь
    канон влезает в промпт сверки. Когда их станут сотни, понадобится отбор
    релевантного — и это будет тот же вопрос, что у `build_snapshot`, только
    с другой ценой ошибки: там пропущенное воспоминание просто не всплыло,
    здесь пропущенное даёт записанное противоречие.
    """
    rows = conn.execute(
        """
        SELECT id, happened_at, precision, text, source, weight
          FROM memories ORDER BY happened_at, id
        """
    ).fetchall()
    return [
        {"id": r["id"], "happened_at": iso(r["happened_at"]),
         "precision": r["precision"], "text": r["text"],
         "source": r["source"], "weight": float(r["weight"])}
        for r in rows
    ]


def add_memory(conn, happened_at, precision: str, text: str, source: str,
               weight: float = 1.0, now=None) -> int:
    """Записать воспоминание. Только INSERT — переписывания не бывает.

    Неизменяемость здесь не осторожность, а единственное, что отличает
    биографию от потока галлюцинаций (см. `0003_life.sql`): модель без
    состояния при каждом вопросе «расскажи о детстве» сочиняет новое детство,
    и только уже записанное не даёт ей это сделать дважды.

    Отсюда же отсутствие `ON CONFLICT`: сливать воспоминания не по чему.
    Повтор — не конфликт ключа, а вопрос смысла, и отвечает на него сверка
    (`mind.check_memory`) до вызова, а не база после.

    **`now` передаётся, а не берётся из `DEFAULT now()` (Шаг 43.1).** Это было
    единственное место в проекте, где момент брала база: везде «сейчас» едет
    параметром от вызывающего — и в фоновых заходах, и в T2, и в обещаниях.
    Расхождение было незаметным ровно до тех пор, пока никто не спрашивал у
    таблицы, давно ли это записано; а `last_dream_at` спрашивает, и заслонка
    «не чаще раза в двадцать часов» сравнивала переданное `now` с часами
    базы. В бою они совпадают, под сбруёй — расходятся на месяцы, и заслонка
    сна не закрывалась вовсе. Ось записи теперь приходит оттуда же, откуда
    ось события.
    """
    return conn.execute(
        """
        INSERT INTO memories (happened_at, precision, text, source, weight,
                              created_at)
        VALUES (%s, %s, %s, %s, %s, coalesce(%s, now())) RETURNING id
        """,
        (happened_at, precision, text.strip(), source, weight, now),
    ).fetchone()["id"]


def touch_recall(conn, memories, now, bump: float = RECALL_BUMP) -> None:
    """Отметить всплывшее вспомненным: сдвинуть метку и чуть прибавить вес.

    **Зовётся не на чтении снимка, а там, где снимок УЕХАЛ В МОДЕЛЬ.** Разница
    не косметическая. Снимок собирают инспектор, сбруя и всякий, кто хочет
    посмотреть на состояние; вспомнил персонаж только то, что попало в промпт.
    Штампуй мы на чтении — вес рос бы от разглядывания, а не от разговора, и
    отбор перестал бы означать то, ради чего заведён.

    Ставится ВНУТРИ уже существующей единицы работы вызывающего
    (`prompt_and_latch`, фоновый заход), а не своей: отдельная транзакция на
    каждое чтение вернула бы фонового писателя, которого Шаг 37 вычитал.

    Префикс `mem_` снимается здесь, потому что навешен тоже здесь
    (`build_snapshot`). Отдавать наружу разбор собственной формы значило бы
    завести знание о ней у второго читателя.
    """
    ids = []
    for m in memories or ():
        raw = m.get("id") if isinstance(m, dict) else m
        try:
            ids.append(int(str(raw).split("_")[-1]))
        except (TypeError, ValueError):
            continue
    if not ids:
        return
    conn.execute(
        """
        UPDATE memories
           SET last_recalled = %s,
               weight = least(weight + %s, %s)
         WHERE id = ANY(%s)
        """,
        (now, bump, RECALL_CEILING, ids),
    )


def set_traits(conn, traits, now) -> None:
    """Заменить черты целиком и подвинуть водяной знак (Шаг 42).

    Одной строкой, а не двумя `UPDATE`: список и метка — один факт («вот что
    мы решили и когда»), и разъехаться им нельзя. Разъезд не гипотетический:
    метка без списка означала бы «считали и ничего не вышло», список без
    метки — «считали и забыли», и второе давало бы пересчёт каждую ночь.

    Пустой список ЗАКОНЕН и метку двигает. Так пересчёт, вернувший меньше
    `TRAITS_MIN`, не повторяется на следующую же ночь при том же каноне:
    неудача стоила вызова модели, и платить за неё дважды незачем. Решает,
    писать ли пустое, вызывающий (`cycle.reconsider_traits`); хранилище его
    не переспрашивает.
    """
    conn.execute(
        "UPDATE agent SET traits = %s, traits_at = %s WHERE id = 1",
        (list(traits), now),
    )


def traits_at(conn):
    """Когда черты пересчитывали. `None` — ни разу."""
    row = conn.execute("SELECT traits_at FROM agent WHERE id = 1").fetchone()
    return row["traits_at"] if row else None


def memories_since(conn, at) -> int:
    """Сколько воспоминаний ЗАПИСАНО после метки. Без метки — все.

    Ось записи (`created_at`), а не жизни (`happened_at`), и это существенно:
    приснившееся сегодня про семилетнего — новое знание о человеке, хотя
    датируется двадцатью годами назад. Считай мы по оси жизни, такая запись
    не сдвинула бы счёт вовсе, и пересчёт черт не случился бы никогда у
    персонажа, которому снится детство.
    """
    if at is None:
        return conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"]
    return conn.execute(
        "SELECT count(*) AS n FROM memories WHERE created_at > %s", (at,)
    ).fetchone()["n"]


# =============================================================================
# Нити (Шаг 47): незакрытые линии
# =============================================================================
def open_threads(conn, side: str = "self", limit: int | None = None) -> list[dict]:
    """Открытые нити, свежие сверху. Порядок — свежесть возвращения.

    `limit` задаёт вызывающий, а не константа здесь: промпту персонажа нужны
    три, вечернему проходу — все, инспектору — все. Хранилище отбирает, но не
    решает сколько (то же правило, что у `MEMORIES_LIMIT`, который живёт рядом
    со своим единственным читателем).
    """
    rows = conn.execute(
        """
        SELECT id, side, text, opened_at, touched_at
          FROM threads
         WHERE closed_at IS NULL AND side = %s
         ORDER BY touched_at DESC, id
         LIMIT %s
        """,
        (side, limit),
    ).fetchall()
    return [{"id": r["id"], "side": r["side"], "text": r["text"],
             "opened_at": iso(r["opened_at"]), "touched_at": iso(r["touched_at"])}
            for r in rows]


def open_thread(conn, side: str, text: str, now) -> int:
    """Завести нить. `touched_at` = `opened_at`: открыть значит вернуться."""
    return conn.execute(
        """
        INSERT INTO threads (side, text, opened_at, touched_at)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (side, text.strip(), now, now),
    ).fetchone()["id"]


def touch_threads(conn, ids, now) -> int:
    """К нитям вернулись. Возвращает, скольких это коснулось.

    Закрытые не трогаются: вернуться к закрытому нельзя, а молча воскрешать
    его по номеру из ответа модели — значит дать проходу власть, которой у
    него нет.
    """
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    rows = conn.execute(
        "UPDATE threads SET touched_at = %s "
        " WHERE id = ANY(%s) AND closed_at IS NULL RETURNING id",
        (now, ids),
    ).fetchall()
    return len(rows)


def close_threads(conn, ids, now, why: str) -> int:
    """Закрыть названные. `why` отличает доведённое от брошенного."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    rows = conn.execute(
        "UPDATE threads SET closed_at = %s, closed_why = %s "
        " WHERE id = ANY(%s) AND closed_at IS NULL RETURNING id",
        (now, why, ids),
    ).fetchall()
    return len(rows)


def forget_threads(conn, now, older_than_days: float, why: str) -> list[dict]:
    """Закрыть то, к чему давно не возвращались. Без модели — это арифметика.

    Возвращает закрытое, а не число: забытое стоит того, чтобы попасть в лог
    поимённо. Брошенная линия — единственное в проекте, что исчезает из виду
    само, и заметить это должно быть легко.
    """
    rows = conn.execute(
        """
        UPDATE threads SET closed_at = %(now)s, closed_why = %(why)s
         WHERE closed_at IS NULL
           AND touched_at < %(now)s - make_interval(days => %(days)s)
        RETURNING id, text
        """,
        {"now": now, "why": why, "days": int(older_than_days)},
    ).fetchall()
    return [{"id": r["id"], "text": r["text"]} for r in rows]


def all_threads(conn) -> list[dict]:
    """Все, включая закрытые. Для инспектора и сбруи."""
    rows = conn.execute(
        """
        SELECT id, side, text, opened_at, touched_at, closed_at, closed_why
          FROM threads ORDER BY id
        """
    ).fetchall()
    return [{"id": r["id"], "side": r["side"], "text": r["text"],
             "opened_at": iso(r["opened_at"]), "touched_at": iso(r["touched_at"]),
             "closed_at": iso(r["closed_at"]), "closed_why": r["closed_why"]}
            for r in rows]


def last_lived(conn):
    """Последняя прожитая сцена: когда записана и что в ней (Шаг 46).

    Два поля одним запросом, потому что оба нужны одному вызывающему и оба
    берутся из одной строки: `created_at` отвечает заслонке «не чаще раза в
    сутки», текст — промпту («чем кончился прошлый день»).

    `created_at`, а не `happened_at`, по тому же доводу, что у `last_dream_at`:
    вопрос «давно ли он записывал день» — про ось записи. У прожитого они
    совпадают, как совпадают у сна, и совпадение так же держится на том, что
    писатель один.
    """
    return conn.execute(
        """
        SELECT created_at, text FROM memories
         WHERE source = 'lived' ORDER BY created_at DESC LIMIT 1
        """
    ).fetchone()


# =============================================================================
# Чтение (Шаг 48)
# =============================================================================
# Хранилище НЕ знает про файлы. Каталог приезжает параметром — его собирает
# `library.catalog()`, край над файловой системой, — а здесь только строки.
# То же разделение, что у погоды: `outside` добывает, `store` хранит, и знать
# друг о друге им незачем.


def sync_books(conn, shelf: list[dict]) -> dict:
    """Свести полку с таблицей. Возвращает, что произошло.

    Три исхода, и различать их обязательно.

    **Новое заводится.** Обычный случай: положили файл, сконвертировали.

    **Пропавшее НЕ ТРОГАЕТСЯ, если книгу брали.** Строка взятой книги — часть
    биографии: к ней привязаны порции и заметки, и снести её значило бы
    стереть месяц чтения из-за того, что файл переименовали. Пропавшее
    невзятое, наоборот, удаляется без сожаления: невзятая строка — чистый
    каталог, за ней ничего не стоит.

    **Изменившаяся длина — конфликт, а не обновление.** Перегнанная книга
    почти наверняка другой длины, и записанная позиция после этого указывает
    не туда. У невзятой это безразлично (позиция ноль), и длина просто
    обновляется. У взятой — нет: молчаливая правка сдвинула бы чтение на
    произвольное число страниц, а `CHECK (position <= length)` вдобавок уронил
    бы транзакцию посреди сверки. Поэтому такая книга остаётся как есть, а
    решение принимает человек: он эту книгу и перегнал.
    """
    paths = [b["text_path"] for b in shelf]
    known = {
        r["text_path"]: r
        for r in conn.execute(
            "SELECT text_path, length, picked_at FROM books").fetchall()
    }

    added, updated, conflicts = [], [], []
    for book in shelf:
        row = known.get(book["text_path"])
        if row is None:
            conn.execute(
                """
                INSERT INTO books (text_path, source_path, source_kind,
                                   title, author, length, quality)
                VALUES (%(text_path)s, %(source_path)s, %(source_kind)s,
                        %(title)s, %(author)s, %(length)s, %(quality)s)
                """,
                {**book, "quality": json.dumps(book.get("quality") or {},
                                               ensure_ascii=False)},
            )
            added.append(book["text_path"])
            continue
        if row["length"] != book["length"] and row["picked_at"] is not None:
            conflicts.append({"text_path": book["text_path"],
                              "was": row["length"], "now": book["length"]})
            continue
        changed = conn.execute(
            """
            UPDATE books
               SET title = %(title)s, author = %(author)s,
                   source_path = %(source_path)s, source_kind = %(source_kind)s,
                   length = %(length)s, quality = %(quality)s
             WHERE text_path = %(text_path)s
               AND (title, author, length, source_path) IS DISTINCT FROM
                   (%(title)s, %(author)s, %(length)s, %(source_path)s)
            RETURNING text_path
            """,
            {**book, "quality": json.dumps(book.get("quality") or {},
                                           ensure_ascii=False)},
        ).fetchone()
        if changed:
            updated.append(book["text_path"])

    # **Пустая полка НИЧЕГО не удаляет.** `<> ALL('{}')` истинно для всех
    # строк, то есть буквальное исполнение правила снесло бы каталог целиком —
    # и снесло бы ровно в том случае, который чаще всего означает не «книг не
    # стало», а «каталог не примонтировался». Отказ от уборки стоит одной
    # лишней строки в таблице; уборка по ошибке стоит полки.
    gone = []
    if paths:
        gone = conn.execute(
            "DELETE FROM books WHERE picked_at IS NULL "
            " AND text_path <> ALL(%s) RETURNING text_path",
            (paths,),
        ).fetchall()
    on_shelf = set(paths)
    missing = [path for path, row in known.items()
               if path not in on_shelf and row["picked_at"] is not None]
    return {"added": added, "updated": updated, "conflicts": conflicts,
            "removed": [r["text_path"] for r in gone], "missing": missing}


def current_book(conn):
    """Книга на руках. `None` — не читает ничего.

    Открытая ровно одна, и держит это `books_one_open_uq`, а не запрос:
    `LIMIT 1` здесь был бы заметанием второй строки под ковёр.
    """
    return conn.execute(
        """
        SELECT id, text_path, title, author, length, position,
               picked_at, picked_why, read_at
          FROM books
         WHERE picked_at IS NOT NULL AND closed_at IS NULL
        """
    ).fetchone()


def current_reading(conn) -> dict | None:
    """То, что о книге помнит сам персонаж. Для снимка.

    Позиции в символах здесь нет намеренно: это мера хранилища, а не память
    человека. Наружу — доля и последняя своя мысль, то есть ровно то, чем
    книга присутствует в голове между заходами.
    """
    row = current_book(conn)
    if row is None:
        return None
    note = conn.execute(
        "SELECT text FROM notes WHERE book_id = %s ORDER BY at DESC, id DESC "
        "LIMIT 1",
        (row["id"],),
    ).fetchone()
    length = row["length"] or 1
    return {
        "title": row["title"],
        "author": row["author"],
        "progress": round(row["position"] / length, 4),
        "started": iso(row["picked_at"]),
        "note": note["text"] if note else None,
    }


def shelf_state(conn) -> dict:
    """Что можно взять и что уже было. Вход прохода выбора.

    Прочитанное подаётся вместе с непрочитанным, и это не удобство: проход,
    видящий только свободные книги, второй раз берётся за брошенное и не
    может сказать, почему взял именно эту.
    """
    free = conn.execute(
        """
        SELECT id, text_path, title, author, length
          FROM books WHERE picked_at IS NULL ORDER BY id
        """
    ).fetchall()
    past = conn.execute(
        """
        SELECT title, author, closed_why, closed_at
          FROM books WHERE closed_at IS NOT NULL ORDER BY closed_at DESC
        """
    ).fetchall()
    return {
        "free": [dict(r) for r in free],
        "past": [{"title": r["title"], "author": r["author"],
                  "why": r["closed_why"], "at": iso(r["closed_at"])}
                 for r in past],
    }


def pick_book(conn, text_path: str, why: str, now):
    """Взять книгу. `None` — не вышло, и ничего не тронуто.

    Защита УСЛОВНЫМ `UPDATE`, а не проверкой перед ним, по тому же доводу, что
    у `record_birth`: «посмотреть, не читает ли он что-то, и взять» — это
    check-then-act, и вторая открытая книга разошлась бы с уникальным
    индексом уже внутри транзакции. Условие живёт в `WHERE`, решает база.
    """
    return conn.execute(
        """
        UPDATE books SET picked_at = %(now)s, picked_why = %(why)s
         WHERE text_path = %(path)s
           AND picked_at IS NULL
           AND NOT EXISTS (SELECT 1 FROM books
                            WHERE picked_at IS NOT NULL AND closed_at IS NULL)
        RETURNING id, text_path, title, author, length, position
        """,
        {"now": now, "why": why.strip(), "path": text_path},
    ).fetchone()


def advance_reading(conn, book_id: int, from_pos: int, to_pos: int,
                    conspectus: str | None, now) -> int | None:
    """Записать порцию и сдвинуть позицию. `None` — позиция уже не та.

    Сдвиг условный (`WHERE position = %(from_pos)s`), и это не перестраховка.
    Между тем, как проход взял порцию, и тем, как он её записывает, лежит
    вызов модели — то есть минуты. Транзакция столько не живёт (правило,
    по которому Шаг 28 снял `FOR UPDATE SKIP LOCKED` у очереди), и
    единственная настоящая развязка — условие в `WHERE`.

    Позиция не совпала — значит прочитанное относится к другому месту книги,
    и записывать его нельзя: конспект лёг бы не к тому куску, а сдвиг
    перепрыгнул бы через страницы. Всё или ничего.
    """
    moved = conn.execute(
        """
        UPDATE books SET position = %(to_pos)s, read_at = %(now)s
         WHERE id = %(id)s AND position = %(from_pos)s AND closed_at IS NULL
        RETURNING id
        """,
        {"id": book_id, "from_pos": from_pos, "to_pos": to_pos, "now": now},
    ).fetchone()
    if moved is None:
        return None
    return conn.execute(
        """
        INSERT INTO readings (book_id, at, from_pos, to_pos, conspectus)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (book_id, now, from_pos, to_pos, conspectus),
    ).fetchone()["id"]


def conspectus_so_far(conn, book_id: int, limit: int) -> list[str]:
    """Конспекты по порядку чтения, последние `limit`.

    Порядок — чтения, а не записи: перечитывание книги с начала сегодня
    невозможно, но станет возможным в тот день, когда появится `position`
    назад, и переворачивать смысл запроса тогда было бы поздно.

    `limit` задаёт вызывающий: сколько прошлого влезает в промпт — вопрос
    промпта. То же правило, что у `open_threads`.
    """
    rows = conn.execute(
        """
        SELECT conspectus FROM readings
         WHERE book_id = %s AND conspectus IS NOT NULL
         ORDER BY from_pos DESC, id DESC LIMIT %s
        """,
        (book_id, limit),
    ).fetchall()
    return [r["conspectus"] for r in reversed(rows)]


def add_note(conn, book_id: int, reading_id: int | None, text: str,
             now, at_pos: int | None = None) -> int:
    """Заметка на полях. В канон НЕ пишет — это делает вечерний проход."""
    return conn.execute(
        """
        INSERT INTO notes (book_id, reading_id, at, at_pos, text)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (book_id, reading_id, now, at_pos, text.strip()),
    ).fetchone()["id"]


def untold_notes(conn, limit: int | None = None) -> list[dict]:
    """Нерассказанное, свежее сверху. Вход вечернего прохода и заговаривания."""
    rows = conn.execute(
        """
        SELECT n.id, n.text, n.at, n.at_pos, b.title, b.author, b.text_path
          FROM notes n JOIN books b ON b.id = n.book_id
         WHERE n.told_at IS NULL
         ORDER BY n.at DESC, n.id DESC LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [{"id": r["id"], "text": r["text"], "at": iso(r["at"]),
             "at_pos": r["at_pos"], "title": r["title"], "author": r["author"],
             "text_path": r["text_path"]} for r in rows]


def mark_notes_told(conn, ids, now) -> int:
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    rows = conn.execute(
        "UPDATE notes SET told_at = %s WHERE id = ANY(%s) AND told_at IS NULL "
        "RETURNING id",
        (now, ids),
    ).fetchall()
    return len(rows)


def notes_between(conn, since, until) -> list[dict]:
    """Заметки за окно. Вход вечернего прохода: что он сегодня отметил.

    Окно, а не «нерассказанное»: день подводит ИТОГ ДНЯ, и заметка недельной
    давности, до которой не дошли руки, в сегодняшнюю сцену не годится.
    """
    rows = conn.execute(
        """
        SELECT n.id, n.text, n.at, b.title, b.author
          FROM notes n JOIN books b ON b.id = n.book_id
         WHERE n.at >= %s AND n.at < %s
         ORDER BY n.at, n.id
        """,
        (since, until),
    ).fetchall()
    return [{"id": r["id"], "text": r["text"], "at": iso(r["at"]),
             "title": r["title"], "author": r["author"]} for r in rows]


def deeds_between(conn, since, until) -> dict:
    """Что он ДЕЛАЛ за окно. Вход вечернего прохода (Шаг 50).

    **Первый вход дня, который не является его же памятью.** До этого шага
    `day_tick` получал канон, вчерашнюю сцену, нити и погоду — и всё, кроме
    погоды, было написано им самим. Отсюда болезнь, названная в
    `0011_reading.sql`: у модели нет источника разнообразия, кроме
    собственного распределения, и через два месяца вечеров получается человек,
    который ходит по редакции и откладывает письмо. Промпт дня лечил симптом
    («Чего в сцене быть НЕ должно: значительности»), потому что лечить причину
    ему было нечем.

    Здесь появляется причина: прочитанная порция либо есть, либо её нет, и
    спорить с этим модель не может.

    **Имя выбрано на вырост, а форма — нет.** «Дела» — это то, чем однажды
    станет общий каркас (намерение → работа → результат), и назвать функцию
    `readings_between` значило бы переименовывать её на первом же втором деле.
    Но знает она СЕГОДНЯ только про книги, и никакого общего каркаса не
    угадывает: правило `0011` («каркас извлекается из двух настоящих дел, а не
    угадывается до них») остаётся в силе, а второго дела ещё нет.

    Четыре списка, и все четыре — разные события, а не один с пометками:
    взял книгу, читал, подумал на полях, закрыл. Свести их в один список с
    колонкой «род» значило бы завести ту самую схему под неизвестное.

    Конспекты НЕ возвращаются намеренно. Вечеру нужно, ЧТО он делал, а не
    пересказ книги: попади конспект в промпт дня, и сцена съехала бы в
    изложение прочитанного — день стал бы читательским дневником.
    """
    readings = conn.execute(
        """
        SELECT b.title, b.author, r.to_pos - r.from_pos AS chars
          FROM readings r JOIN books b ON b.id = r.book_id
         WHERE r.at >= %s AND r.at < %s
         ORDER BY r.at, r.id
        """,
        (since, until),
    ).fetchall()
    picked = conn.execute(
        """
        SELECT title, author, picked_why FROM books
         WHERE picked_at >= %s AND picked_at < %s ORDER BY picked_at
        """,
        (since, until),
    ).fetchall()
    closed = conn.execute(
        """
        SELECT title, author, closed_why FROM books
         WHERE closed_at >= %s AND closed_at < %s ORDER BY closed_at
        """,
        (since, until),
    ).fetchall()
    return {
        "readings": [{"title": r["title"], "author": r["author"],
                      "chars": int(r["chars"])} for r in readings],
        # Заметки берутся готовым читателем, а не четвёртым запросом здесь:
        # `notes_between` написан, задокументирован и отвечает ровно на этот
        # вопрос. Вторая копия того же `SELECT` разошлась бы с первой молча.
        "notes": notes_between(conn, since, until),
        "picked": [{"title": r["title"], "author": r["author"],
                    "why": r["picked_why"]} for r in picked],
        "closed": [{"title": r["title"], "author": r["author"],
                    "why": r["closed_why"]} for r in closed],
        # Второе дело (Шаг 57), и каркас «намерение -> работа -> результат»
        # теперь извлекается из настоящего: журнал `pursuits` так и устроен.
        # Сюда едут только дела, у которых нет своей таблицы: чтение уже
        # видно выше, эссе живёт файлом, новости — поводом.
        "pursuits": [p for p in _pursuits_between(conn, since, until)
                     if p["action"] in ("explore", "recall", "daydream")],
    }


def _pursuits_between(conn, since, until) -> list[dict]:
    import store_agenda
    return store_agenda.pursuits_between(conn, since, until)


def close_book(conn, book_id: int, now, why: str):
    """Закрыть книгу: дочитал или бросил. Строка остаётся (`0011_reading.sql`)."""
    return conn.execute(
        """
        UPDATE books SET closed_at = %s, closed_why = %s
         WHERE id = %s AND closed_at IS NULL
        RETURNING id, title, author, position, length
        """,
        (now, why, book_id),
    ).fetchone()


def all_books(conn) -> list[dict]:
    """Вся полка, включая закрытое. Для инспектора и сбруи."""
    rows = conn.execute(
        """
        SELECT b.id, b.text_path, b.title, b.author, b.length, b.position,
               b.picked_at, b.picked_why, b.read_at, b.closed_at, b.closed_why,
               count(r.id) AS readings, max(r.to_pos) AS read_to
          FROM books b LEFT JOIN readings r ON r.book_id = b.id
         GROUP BY b.id ORDER BY b.id
        """
    ).fetchall()
    return [{"id": r["id"], "text_path": r["text_path"], "title": r["title"],
             "author": r["author"], "length": r["length"],
             "position": r["position"], "picked_at": iso(r["picked_at"]),
             "picked_why": r["picked_why"], "read_at": iso(r["read_at"]),
             "closed_at": iso(r["closed_at"]), "closed_why": r["closed_why"],
             "readings": r["readings"]} for r in rows]


def last_dream_at(conn):
    """Когда снилось в последний раз. `None` — не снилось ни разу.

    Отдельной метки под это нет и не будет: ответ выводится из самой таблицы,
    а второй факт об одном и том же расходится с первым ровно тогда, когда на
    него смотрят (`0003_life.sql`, `0004_nameless.sql`).

    Метка — `created_at`, а не `happened_at`, и они здесь не совпадают только
    по видимости: сон датируется ночью, когда приснился, то есть обеими
    метками одинаково. Спрашивается всё же ось записи — потому что вопрос
    «давно ли он спал» про запись и есть, а совпадение осей у снов случайно и
    держится на том, что их пишет один вызывающий.
    """
    row = conn.execute(
        "SELECT max(created_at) AS at FROM memories WHERE source = 'dream'"
    ).fetchone()
    return row["at"] if row else None


def record_birth(conn, name: str, born_at, birthplace: str | None,
                 reason: str) -> bool:
    """Записать рождение. False — персонаж уже родился, ничего не тронуто.

    **Рождение необратимо и происходит ровно один раз.** Отсюда два решения.

    Первое: защита УСЛОВНЫМ `UPDATE`, а не проверкой перед ним. «Прочитать
    `born_at`, увидеть NULL, записать» — тот же check-then-act, что отвергнут
    у объектов и сессий, и здесь цена гонки выше всех: два запуска подряд
    дали бы персонажа с именем от одного рождения и датой от другого, а
    поправить это нечем — воспоминание уже легло. Условие живёт в `WHERE`,
    и решает его база.

    Второе: `born_at IS NULL` как признак, а не отдельный флаг. Так записано
    в `0003_life.sql`, и здесь это правило впервые применяется: флаг,
    дублирующий состояние, расходится с ним ровно тогда, когда на него
    смотрят.

    Имя, дата, место и первое воспоминание пишутся ОДНОЙ единицей. Родиться
    наполовину нельзя: персонаж с датой, но без имени, — это не персонаж, а
    поломка, которую нечем отличить от незавершившегося рождения.
    """
    row = conn.execute(
        """
        UPDATE agent SET name = %s, born_at = %s, birthplace = %s
         WHERE id = 1 AND born_at IS NULL
        RETURNING id
        """,
        (name, born_at, birthplace),
    ).fetchone()
    if not row:
        return False

    # Первое воспоминание — не украшение. Это самое раннее, что персонаж о
    # себе знает, и знает он это С ЧУЖИХ СЛОВ: своего рождения не помнит
    # никто. Отсюда `precision='era'` — оно и не должно датироваться точнее,
    # хотя дата известна до секунды.
    add_memory(conn, born_at, "era", reason, "genesis")
    return True


def _fill_fixture(conn, state: dict) -> None:
    """Тело заливки. Зовётся только из `load_fixture`, всегда под границей."""
    self = state.get("self", {})
    place = self.get("place", {})

    # `impulses` перечислена ЯВНО, и это не избыточность. Остальные таблицы
    # очереди и переваривания (`inbox`, `followups`) сюда доезжают через
    # CASCADE — у них есть внешний ключ на `messages`. У импульсов ключа нет
    # и быть не должно: повод заговорить не принадлежит ни одной реплике, он
    # существует до неё. Значит CASCADE до него не достаёт, и не назови его
    # здесь — побуждения переживали бы заливку фикстура, копясь от сценария
    # к сценарию и от прогона к прогону. Поймано сбруей на первом же
    # повторном прогоне: сила импульса отличалась в третьем знаке.
    #
    # Это второй случай той же породы за два шага (первым был
    # `agent.last_search_ts`). Общее правило: таблица, не связанная ключом с
    # `messages`, обязана быть названа тут поимённо, иначе изоляция сценариев
    # через неё течёт.
    # `books, readings, notes` дописаны Шагом 48. Перечень поимённый и о
    # новом не напоминает — ровно тем, чем он уже дважды подвёл
    # (`last_search_ts`, `traits_at`): забытая таблица переживает сценарий,
    # и соседний видит её остатки. Полка тут особенно опасна: книга,
    # оставшаяся открытой от прошлого сценария, делает читающий проход
    # зависимым от порядка прогона.
    #
    # С Шага 56 перечень НЕ поимённый. Поимённый подвёл в четвёртый раз:
    # `essays` и `essay_passages` (Шаг 53) в него не попали вовсе, а
    # `trait_history`, заведённая Шагом 56, унесла основания черт из сценария
    # `traits` в сценарий `drives`. Список теперь берётся из каталога: все
    # таблицы схемы, кроме строки `agent` (она одна и обязана жить) и журнала
    # миграций (он описывает базу, а не персонажа). Новая таблица попадает
    # сюда сама, и забыть её больше нельзя.
    tables = [r["tablename"] for r in conn.execute(
        """SELECT tablename FROM pg_tables
            WHERE schemaname = current_schema()
              AND tablename NOT IN ('agent', 'schema_migrations')
            ORDER BY tablename"""
    ).fetchall()]
    conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables)
                 + " RESTART IDENTITY CASCADE")

    conn.execute(
        """
        -- `last_search_ts` сбрасывается в NULL, а не приезжает из фикстура:
        -- заслонка ретривера — состояние ПРОГОНА, а не описанного снимка, и
        -- значения у неё в фикстуре нет. До Шага 33 колонка тут не
        -- упоминалась вовсе, и это было незаметно ровно до тех пор, пока
        -- заслонка ездила параметром: сбруя подавала её сама, база при этом
        -- молча хранила метку от ПРЕДЫДУЩЕГО сценария. С переездом заслонки
        -- в хранилище метка стала входом хода, и течь проступила первой же
        -- строкой эталона — `turn` начинался с чужого времени и зависел от
        -- того, что прогонялось перед ним. Изоляцию сценариев даёт TRUNCATE,
        -- а `agent` он не трогает (строка одна и обязана жить), поэтому
        -- каждое поле здесь названо поимённо.
        -- `traits_at` сбрасывается здесь по той же причине и той же ценой
        -- (Шаг 43.1). Колонка появилась на Шаге 42, в этом перечне названа не
        -- была, и `TRUNCATE` её не трогает — строка `agent` одна и обязана
        -- жить. В результате метка пересчёта переживала не только сценарий, а
        -- весь прогон: сценарий `traits` видел водяной знак, оставленный
        -- соседом, и второй заход, обязанный промолчать, звал модель. Течь
        -- та же самая, что описана выше про `last_search_ts`, — и повторилась
        -- она ровно потому, что перечень поимённый: он ничего не забывает
        -- сам, но и не напоминает о новом.
        -- `read_at` дописан Шагом 49 — тем же движением и по третьему разу.
        -- Метка захода чтения переживает `TRUNCATE` (строка `agent` одна),
        -- и не сбрось её здесь, читающий сценарий видел бы водяной знак от
        -- соседа: заход, обязанный состояться, молчал бы по интервалу.
        UPDATE agent SET name=%s, born_at=%s, birthplace=%s, traits=%s, mood=%s,
               place_label=%s, place_lat=%s, place_lon=%s, outside_latch=%s,
               last_exchange_ts=%s, last_search_ts=NULL, traits_at=%s,
               mood_reason=%s, mood_since=%s, day_at=NULL, read_at=NULL,
               essay_at=NULL, news_at=NULL, drives_at=NULL, agenda_at=NULL
         WHERE id = 1
        """,
        (
            self.get("name"),
            self.get("born_at"),
            self.get("birthplace"),
            list(self.get("traits", [])),
            self.get("mood", "нейтральное"),
            place.get("label"),
            place.get("lat"),
            place.get("lon"),
            json.dumps(self.get("outside")) if self.get("outside") else None,
            state.get("last_exchange_ts"),
            self.get("traits_at"),
            self.get("mood_reason"),
            self.get("mood_since"),
        ),
    )

    # Объект №0 — строка в той же таблице, что и все остальные.
    conn.execute(
        "INSERT INTO objects (id, type, label, label_norm, salience) "
        "VALUES (%s, 'self', 'self', 'self', 0)",
        (SELF_ID,),
    )
    _insert_assertions(conn, SELF_ID, self.get("assertions", []))

    for m in state.get("memories", []):
        conn.execute(
            """
            -- `created_at` приезжает из фикстура, а не берётся `DEFAULT now()`
            -- (Шаг 43.1). Это вторая ось `memories`: `happened_at` — когда
            -- случилось, `created_at` — когда записали, и вторая ось у
            -- фикстура до сих пор была настенными часами машины. Отсюда две
            -- беды сразу. `memories_since` мерит от неё накопление, и все
            -- строки фикстура всегда оказывались «новее» любой метки, из-за
            -- чего заслонка черт не закрывалась никогда. А `recall_score`
            -- считает от неё затухание — то есть в эталонах, объявленных
            -- независимыми от часов, сидела величина, зависящая от даты
            -- прогона; спасало лишь то, что метки у всех строк совпадали и
            -- порядок держался на весе.
            INSERT INTO memories (happened_at, precision, text, source, weight,
                                  created_at)
            VALUES (%s, %s, %s, %s, %s, coalesce(%s, now()))
            """,
            (m["happened_at"], m["precision"], m["text"], m["source"],
             m.get("weight", 1.0), m.get("created_at")),
        )

    for t in state.get("threads", []):
        conn.execute(
            """
            INSERT INTO threads (side, text, opened_at, touched_at,
                                 closed_at, closed_why)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (t.get("side", "self"), t["text"], t["opened_at"], t["touched_at"],
             t.get("closed_at"), t.get("closed_why")),
        )

    for oid, o in state.get("objects", {}).items():
        num = int(str(oid).removeprefix("obj_"))
        conn.execute(
            """
            INSERT INTO objects (id, type, label, label_norm, salience, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (num, o.get("type", "other"), o.get("label", ""),
             norm_name(o.get("label", "")),
             o.get("salience", 1.0), o.get("last_seen") or None),
        )
        for alias in o.get("aliases", []):
            conn.execute(
                "INSERT INTO aliases (object_id, alias, alias_norm) VALUES (%s, %s, %s)",
                (num, alias, norm_name(alias)))
        _insert_assertions(conn, num, o.get("assertions", []))

    # Открытый буфер фикстура -> открытая сессия. Без этого разговор,
    # который шёл в момент снимка, пропадал бы, и эпизод при закрытии
    # получался бы короче на всю свою голову.
    buf = state.get("buffer") or {}
    if buf:
        sid = conn.execute(
            "INSERT INTO sessions (started_at, dropped) VALUES (%s, %s) RETURNING id",
            (buf.get("started_at"), int(buf.get("dropped", 0))),
        ).fetchone()["id"]
        for item in buf.get("exchanges", []):
            for role in ("user", "assistant"):
                conn.execute(
                    "INSERT INTO messages (session_id, ts, role, text) "
                    "VALUES (%s, %s, %s, %s)",
                    (sid, item.get("ts"), role, item.get(role, "")),
                )

    for ep in state.get("episodes", []):
        conn.execute(
            """
            INSERT INTO episodes (id, started_at, ended_at, exchanges, summary)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (int(str(ep["id"]).removeprefix("ep_")), ep.get("started_at"),
             ep.get("ended_at"), ep.get("exchanges", 0), ep.get("summary")),
        )
    # Последовательность обязана продолжиться ПОСЛЕ залитых вручную id,
    # иначе первый же новый объект столкнётся с существующим. В жизни этот
    # путь не возникает — id всегда выдаёт база, — но фикстур льётся с
    # готовыми номерами, и без setval сверка записи падала бы на вставке.
    conn.execute("SELECT setval('objects_id_seq', greatest((SELECT max(id) FROM objects), 1))")
    conn.execute("SELECT setval('episodes_id_seq', greatest((SELECT max(id) FROM episodes), 1))")


def _insert_assertions(conn, object_id: int, assertions) -> None:
    for a in assertions:
        conn.execute(
            """
            INSERT INTO assertions (object_id, key, value, confidence, hits, ts,
                                    source, confirmed)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (object_id, a.get("key"), a.get("value"), a.get("confidence", "low"),
             a.get("hits", 1), a.get("ts") or None, a.get("source") or "user",
             bool(a.get("confirmed"))),
        )


# =============================================================================
# Пишущая половина: память. Сессия и `messages` — отдельным шагом
# =============================================================================
# Область шага сужена намеренно. Объекты, псевдонимы и ассершены — это ПОРТ:
# та же семантика на другом движке, и её можно сверить снимком. Буфер сессии
# — не порт, а смена смысла: `BUFFER_EXCHANGE_LIMIT` перестаёт быть обрезкой
# и становится `LIMIT` в запросе выжимки (ROADMAP, 3b). Смешать порт со
# сменой смысла — ровно тот стык, что трижды укусил на Шаге 12.

def _match_object(conn, candidate: dict) -> int | None:
    """Найти объект: по id, затем по label/алиасу. Порядок как в `store`.

    Сравнение через `lower()` — то же, что `store._norm`. Уникальный индекс
    `aliases_norm_idx` считает так же, иначе «Аня» и «аня» разъехались бы в
    две строки, а сматчились бы в одну.
    """
    cid = candidate.get("id")
    if cid:
        num = _num(cid)
        if num is not None:
            row = conn.execute("SELECT id FROM objects WHERE id = %s", (num,)).fetchone()
            if row:
                return row["id"]

    names = [candidate.get("label", "")] + list(candidate.get("aliases", []))
    names = [norm_name(n) for n in names if n and n.strip()]
    if not names:
        return None

    # Сравниваются ГОТОВЫЕ нормализованные строки: складывание регистра
    # сделано в Python, база к нему не причастна. См. `snapshot.norm_name`.
    row = conn.execute(
        """
        SELECT o.id FROM objects o
         WHERE o.id <> %s
           AND (o.label_norm = ANY(%s)
                OR EXISTS (SELECT 1 FROM aliases a
                            WHERE a.object_id = o.id AND a.alias_norm = ANY(%s)))
         ORDER BY o.id
         LIMIT 1
        """,
        (SELF_ID, names, names),
    ).fetchone()
    return row["id"] if row else None


def _num(oid) -> int | None:
    try:
        return int(str(oid).removeprefix("obj_"))
    except (TypeError, ValueError):
        return None


def upsert_object(conn, candidate: dict, now) -> int:
    """Вплавить кандидата от экстрактора. Возвращает id объекта.

    Семантика — та же, что у `store.upsert_object`, буква в букву: матчинг
    по id -> label/alias -> новый; псевдонимы и ассершены дописываются без
    дублей; противоречия не резолвятся; `last_seen` и `salience` обновляются
    всегда. Отличие одно и вынужденное: id выдаёт последовательность, а не
    поле состояния, — счётчик в состоянии был ровно тем, чем счётчики
    бывают при двух писателях.
    """
    oid = _match_object(conn, candidate)

    if oid is None:
        # Между `_match_object` и этой вставкой умещается второй писатель:
        # оба не нашли «Аню», оба вставляют. `objects_label_norm_uq` не даёт
        # разъехаться, а `DO NOTHING` превращает проигранную гонку из
        # исключения в пустой RETURNING — дальше работаем со строкой того,
        # кто успел, ровно как если бы мы её сматчили.
        label = candidate.get("label", "")
        lnorm = norm_name(label)
        row = conn.execute(
            """
            INSERT INTO objects (type, label, label_norm, salience, last_seen)
            VALUES (%s, %s, %s, 1.0, %s)
            ON CONFLICT (label_norm) WHERE label_norm <> '' AND id <> 0
            DO NOTHING
            RETURNING id
            """,
            (candidate.get("type", "other"), label, lnorm, now),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM objects WHERE label_norm = %s AND id <> %s",
                (lnorm, SELF_ID),
            ).fetchone()
        oid = row["id"]

    for alias in candidate.get("aliases", []):
        alias = (alias or "").strip()
        if alias:
            # ON CONFLICT по нормализованной форме: повтор в другом регистре
            # не заводит вторую строку, но и не переписывает первую —
            # порядок появления сохраняется, а он доезжает до промпта.
            conn.execute(
                "INSERT INTO aliases (object_id, alias, alias_norm) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (object_id, alias_norm) DO NOTHING",
                (oid, alias, norm_name(alias)),
            )

    merge_assertions(conn, oid, candidate.get("assertions", []), now)

    # `salience` растёт всегда, даже если фактов не принесли: важен сам факт
    # касания. То же, что `obj["salience"] + 0.5` в JSON-движке.
    conn.execute(
        "UPDATE objects SET last_seen = %s, salience = salience + 0.5 WHERE id = %s",
        (now, oid),
    )
    return oid


def merge_assertions(conn, object_id: int, incoming, now) -> None:
    """Вплавить ассершены по паре (key, value).

    Три решения Шагов 14–15 переложены в `ON CONFLICT`, и каждое видно
    строкой:
      - `hits` растёт, `ts` обновляется — повтор есть касание;
      - `source` НЕ переписывается: первый утвердивший и есть происхождение;
      - `confirmed` ставится при СМЕНЕ источника, где в паре участвует `web`.
        Пара `user`↔`self` подтверждением не считается — это одна сторона
        разговора смотрит на факт дважды, а не два источника.
    Флаг только взводится (`confirmed OR ...`) и никогда не снимается:
    подтверждение — событие, а не текущее состояние.
    """
    for a in incoming or []:
        key, value = a.get("key"), a.get("value")
        if not key or not value:
            continue
        conn.execute(
            """
            INSERT INTO assertions (object_id, key, value, confidence, hits, ts,
                                    source, confirmed)
            VALUES (%s, %s, %s, %s, 1, %s, %s, FALSE)
            ON CONFLICT (object_id, key, value) DO UPDATE SET
                hits = assertions.hits + 1,
                ts   = EXCLUDED.ts,
                confirmed = assertions.confirmed
                    OR (assertions.source <> EXCLUDED.source
                        AND 'web' IN (assertions.source, EXCLUDED.source))
            """,
            (object_id, key, value, a.get("confidence", "low"), now,
             a.get("source") or "user"),
        )


def merge_self_assertions(conn, assertions, now) -> None:
    """Self-ассершены — та же таблица, `object_id = 0`.

    Отдельной функции по существу больше нет: обещание «Я — объект №0»
    стало строкой в `objects` и ветвью в общем `merge_assertions`. Обёртка
    оставлена ради имени, знакомого `main`.
    """
    merge_assertions(conn, SELF_ID, assertions, now)


# =============================================================================
# Сессия и реплики. НЕ порт — смена смысла, и она названа вслух
# =============================================================================
# В JSON буфер был окном: сверх `BUFFER_EXCHANGE_LIMIT` реплики выпадали
# насовсем, а счётчик `dropped` помнил, сколько их было. В базе **не выпадает
# ничего**: `messages` хранит разговор целиком, а предел становится `LIMIT` в
# запросе выжимки (ROADMAP, 3b).
#
# Отсюда `dropped` меняет определение — и меняет к лучшему:
#     было:  сколько реплик вытолкнули из буфера (total - BUFFER_LIMIT)
#     стало: сколько реплик СУЩЕСТВУЕТ сверх показанных (total - показано)
# Пока `BUFFER_EXCHANGE_LIMIT == SUMMARY_EXCHANGES_LIMIT == 40`, оба
# определения дают одно число, и паритет сходится побайтно. Но равенство
# этих двух констант нигде не закреплено, а на нём висит предупреждение
# «начало не сохранилось». Опусти кто-нибудь предел выжимки до 20 — и
# JSON-движок начал бы молча врать про начало разговора, потому что
# `dropped` остался бы нулём. Новое определение верно по построению и от
# согласованности констант не зависит. Это единственное место шага, где
# поведение улучшено, а не перенесено, — и сбруя показывает, что сегодня
# улучшение ничего не сдвинуло.

# `SESSION_GAP_HOURS` уехал в `snapshot` на Шаге 28: читателей у него стало
# два, а копий должно остаться ноль. Здесь он только импортируется.
SUMMARY_EXCHANGES_LIMIT = 40


def outside_latch(conn) -> dict:
    """Латч среды одной колонкой. Рядом с `place`, и по той же причине.

    Заведено на Шаге 37. До него единственным способом узнать латч была
    `build_snapshot` — то есть сборка ВСЕЙ памяти: объекты с пересчётом
    затухания, ассершены с ранжированием, эпизоды, self. Для реплики это
    оправдано (снимок нужен целиком), для фонового захода — нет: он спрашивал
    у памяти одно JSON-поле, которое меняется дважды в сутки, и платил за
    него полной сборкой — шестью запросами вместо одного.

    Замер на фикстуре: `sense_impulses` стоил 1.39 мс, из них 1.09 мс —
    снимок. При фоне раз в пять секунд это 17 280 сборок памяти в сутки.
    Чтение колонки стоит 0.06 мс, то есть в двадцать раз меньше.
    """
    row = conn.execute(
        "SELECT outside_latch FROM agent WHERE id = 1"
    ).fetchone()
    return (row["outside_latch"] or {}) if row else {}


def place(conn) -> dict:
    """Место объекта №0. Читателей стало два — фасад и инспектор (Шаг 32).

    До Шага 32 этот `SELECT` стоял прямо в `engine.place()`, и это было
    верно ровно пока читатель был один. Инспектору фасад недоступен по
    устройству (`_ensure_agent` — запись), а свой запрос был бы второй
    копией знания о том, из каких колонок собирается место.

    Пустые поля выбрасываются: `resolve_place` смотрит на ОТСУТСТВИЕ
    ключа, а не на None, и колонка со значением NULL не должна выглядеть
    как заполненная.
    """
    row = conn.execute(
        "SELECT place_label AS label, place_lat AS lat, place_lon AS lon, "
        "place_source AS source, place_asked AS asked FROM agent WHERE id = 1"
    ).fetchone()
    return {k: v for k, v in (row or {}).items() if v is not None}


def last_exchange(conn):
    row = conn.execute("SELECT last_exchange_ts FROM agent WHERE id = 1").fetchone()
    return row["last_exchange_ts"] if row else None


def touch_exchange(conn, now) -> None:
    conn.execute("UPDATE agent SET last_exchange_ts = %s WHERE id = 1", (now,))


def _open_session(conn, now) -> int:
    """Открытая сессия или новая. Открытая — та, у которой нет `closed_at`."""
    row = conn.execute(
        "SELECT id FROM sessions WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return row["id"]

    # Тот же фантом, что у объектов, и та же развязка. `ON CONFLICT` без
    # указания конструкции — потому что `sessions_one_open_uq` стоит на
    # выражении, и выводить его по имени колонки нечем; DO NOTHING покрывает
    # любой конфликт, а конфликт тут возможен ровно один.
    row = conn.execute(
        "INSERT INTO sessions (started_at) VALUES (%s) "
        "ON CONFLICT DO NOTHING RETURNING id",
        (now,),
    ).fetchone()
    if row:
        return row["id"]

    return conn.execute(
        "SELECT id FROM sessions WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]


def append_exchange(conn, user_text: str, answer: str, now, arrived_at=None) -> int:
    """Записать обмен. Две строки `messages`, ничего не выталкивая.

    **Обрезки здесь больше нет (Шаг 24).** Она стояла ради промпта выжимки —
    «в выжимку не должна уезжать простыня», — но платило за это хранилище:
    длинная реплика теряла хвост навсегда. С тех пор как рабочая память
    поднимается из `messages`, потеря стала видимой — персонаж после
    рестарта цитировал бы себя оборванным. Предел уехал к читателю
    (`mind._build_summarizer_prompt`), где он и мотивирован.

    **Форм времени на ход стало две (Шаг 28).** Раньше обе строки писались
    одним `now`, и роадмап называл это принципом — верным ровно до тех пор,
    пока реплика приходила в тот же момент, в который на неё отвечали. С
    очередью моменты разошлись: `arrived_at` — когда человек договорил (метка
    последней склеенной строки `inbox`), `now` — когда персонаж ответил.
    Разница не косметическая: `session_stale`, возраст разговора и `ts`
    обмена в выжимке меряют РАЗНЫЕ вещи в зависимости от того, какой момент
    туда попал. Не задан — оба момента совпадают, как до шага, и все
    четырнадцать эталонов остаются на месте.

    Сессия открывается моментом ПРИХОДА: разговор начинается тогда, когда
    человек написал, а не тогда, когда демон освободился.

    **Возвращает id строки `assistant`**, а не сессии. Возврат сессии не
    читал никто (проверено grep'ом перед правкой), а id ответа нужен
    `inbox.reply_id` — терминальному состоянию реплики, у которой своего
    ответа не будет.
    """
    at = arrived_at or now
    sid = _open_session(conn, at)
    conn.execute(
        "INSERT INTO messages (session_id, ts, role, text) VALUES (%s, %s, 'user', %s)",
        (sid, at, (user_text or "").strip()),
    )
    reply_id = conn.execute(
        "INSERT INTO messages (session_id, ts, role, text) "
        "VALUES (%s, %s, 'assistant', %s) RETURNING id",
        (sid, now, (answer or "").strip()),
    ).fetchone()["id"]
    conn.execute("UPDATE sessions SET ended_at = %s WHERE id = %s", (now, sid))
    return reply_id


def append_utterance(conn, text: str, now) -> int:
    """Записать реплику, сказанную по своей воле. ОДНА строка `messages`.

    Второй канал записи рядом с `append_exchange`, и отдельный он не для
    удобства. `append_exchange` пишет ДВЕ строки и тем утверждает, что у
    сказанного персонажем есть причина в сказанном человеком. Для реплики,
    начатой самим персонажем, это неправда, и подсунуть сюда пустую строку
    `user` значило бы записать в разговор слова, которых никто не говорил, —
    их прочитала бы и рабочая память, и выжимка, и экстрактор.

    Сессия открывается той же функцией, что у обмена: персонаж, заговоривший
    в тишину, начинает разговор — и если человек ответит, ответ ляжет в ту же
    сессию, а не в новую.

    `last_exchange_ts` здесь НЕ трогается, и это решение. Метка отвечает на
    вопрос «когда мы в последний раз разговаривали» — её читает
    `_render_silence` и по ней же копится побуждение `silence`. Обнови её
    собственной репликой — и персонаж, заговорив в пустоту, решил бы, что
    поговорил, а молчание началось бы заново. Он бы сам себя утешил.
    """
    sid = _open_session(conn, now)
    mid = conn.execute(
        "INSERT INTO messages (session_id, ts, role, text, spontaneous) "
        "VALUES (%s, %s, 'assistant', %s, TRUE) RETURNING id",
        (sid, now, (text or "").strip()),
    ).fetchone()["id"]
    conn.execute("UPDATE sessions SET ended_at = %s WHERE id = %s", (now, sid))
    return mid


def last_utterance(conn):
    """Когда персонаж в последний раз заговорил сам. Ни разу — `None`.

    Считается из `messages`, а не из колонки в `agent`. Колонка была бы
    вторым фактом об одном событии, и разъехаться ей есть с чем: строку
    может убрать Curator (Фаза 5), а счётчик остался бы.
    """
    row = conn.execute(
        "SELECT max(ts) AS ts FROM messages WHERE spontaneous"
    ).fetchone()
    return row["ts"] if row else None


def utterances_since(conn, since) -> int:
    """Сколько раз заговорил сам начиная с момента. Бюджет суток считается им."""
    return conn.execute(
        "SELECT count(*) AS n FROM messages WHERE spontaneous AND ts >= %s",
        (since,),
    ).fetchone()["n"]


# =============================================================================
# Импульсы (Шаг 35): побуждение заговорить, которое копится
# =============================================================================
def record_urge(conn, kind: str, subject: str | None, amount: float, now,
                expires_at=None) -> None:
    """Записать СОБЫТИЙНОЕ побуждение: прибавить к накопленному, с затуханием.

    **Режима `set` здесь больше нет (Шаг 37), и это упрощение отменяет
    усложнение того же автора.** Введён он был под тишину — побуждение, сила
    которого не копится, а вычисляется по настоящему. Записывать такое в
    таблицу значило держать в базе производное значение и переписывать его,
    чтобы оно не устаревало: фоновый заход правил строку `silence` каждые пять
    секунд, из-за чего `updated_at` дёргался постоянно и по нему нельзя было
    понять, когда повод возник на самом деле.

    Тишина считается на лету (`cycle.silence_urge`) и в таблицу не попадает.
    Здесь остались только события — то, что случилось однажды и второй раз не
    случится. Ровно та порода, ради которой накопление и затухание нужны.

    `ON CONFLICT` по частичному уникальному индексу, а не «выбрать и решить»:
    check-then-insert дал бы того же фантома, что у объектов и сессий.

    Конструкция индекса названа выражением, а не именем колонки:
    `coalesce(subject, '')` — часть ключа, и вывести её Postgres не может.

    Затухание применяется НА ЗАПИСИ, а не по таймеру: повод, о котором ничто
    не напомнило, обязан слабеть сам, но заводить ради этого фоновую уборку
    значило бы завести второго писателя. Тот же приём, что у
    `web._evict_stale`: чистка на обращении.
    """
    # Затухание выражено в SQL, а не посчитано в Python, по той же причине,
    # что и `effective_salience`: величина, лежащая в базе, пересчитывается
    # там же, где лежит, иначе между чтением и записью появляется щель.
    conn.execute(
        """
        INSERT INTO impulses (kind, subject, urge, created_at, updated_at, expires_at)
        VALUES (%(kind)s, %(subject)s, %(urge)s, %(now)s, %(now)s, %(exp)s)
        ON CONFLICT (kind, coalesce(subject, '')) WHERE spoken_at IS NULL
        DO UPDATE SET
            urge = impulses.urge * pow(%(base)s, greatest(EXTRACT(EPOCH FROM
                   (EXCLUDED.updated_at - impulses.updated_at)) / 3600.0, 0.0)
                   / %(half)s) + EXCLUDED.urge,
            updated_at = EXCLUDED.updated_at,
            expires_at = COALESCE(EXCLUDED.expires_at, impulses.expires_at)
        """,
        {"kind": kind, "subject": subject, "urge": amount, "now": now,
         "exp": expires_at, "base": IMPULSE_DECAY_BASE,
         "half": IMPULSE_HALFLIFE_HOURS},
    )


def born_at(conn):
    """Дата рождения, узким запросом. `None` — ещё не родился.

    Отдельно от снимка по тому же доводу, что `outside_latch` (Шаг 37):
    фоновому заходу нужно одно поле, а `build_snapshot` собирает ради него всю
    память шестью запросами.
    """
    row = conn.execute("SELECT born_at FROM agent WHERE id = 1").fetchone()
    return row["born_at"] if row else None


def memories_on(conn, month: int, day: int) -> list[dict]:
    """Воспоминания, случившиеся в этот день календаря. Только `precision='day'`.

    **Дата сравнивается как есть, без перевода в местный пояс, и это не
    упрощение.** У `precision='day'` метка — не момент, а КАЛЕНДАРНАЯ ДАТА:
    «14 марта 2011-го», записанная полуночью. Перевести её в другой пояс
    значило бы сдвинуть саму дату на сутки — то есть испортить то
    единственное, что в ней есть. Местным при этом остаётся «сегодня»
    (вызывающий считает его по месту персонажа), и в этом нет противоречия:
    сегодня — момент, годовщина — дата.

    Грубее `day` ничего не годится: у `month`, `year` и `era` дня нет, и
    годовщину им назначить не из чего.

    Индекса под это нет и не заводится — тот же довод, что у
    `memories_since`: канон измеряется сотнями строк, и последовательный
    проход дешевле поддержания индекса по выражению.
    """
    rows = conn.execute(
        """
        SELECT id, happened_at, text
          FROM memories
         WHERE precision = 'day'
           AND EXTRACT(MONTH FROM happened_at) = %s
           AND EXTRACT(DAY   FROM happened_at) = %s
         ORDER BY happened_at
        """,
        (month, day),
    ).fetchall()
    return [{"id": r["id"], "happened_at": r["happened_at"], "text": r["text"]}
            for r in rows]


def note_anniversary(conn, subject: str, amount: float, now, expires_at,
                     within_hours: float) -> bool:
    """Завести повод-годовщину, если за `within_hours` такого ещё не заводили.

    **Отдельно от `record_urge`, и это несущее решение шага.** Накопление там
    устроено под СОБЫТИЯ: случилось однажды, второй раз не случится, и
    прибавлять к накопленному правильно. Годовщина — не событие, а свойство
    дня: она «происходит» на каждом фоновом заходе, то есть каждые несколько
    секунд. Прибавляй её `record_urge` — и за час сила ушла бы за все мыслимые
    пороги, а персонаж заговорил бы о дне рождения с одержимостью.

    Проверяется существование, а НЕ `ON CONFLICT`, потому что частичный
    уникальный индекс стоит с условием `spoken_at IS NULL`: сказав про
    годовщину, персонаж снял бы себе запрет — и следующий же заход завёл бы её
    заново. Здесь смотрят на все строки, сказанные тоже.

    Возвращает, завелась ли. Ложь — обычный исход: за сутки он истинен один
    раз.
    """
    row = conn.execute(
        """
        INSERT INTO impulses (kind, subject, urge, created_at, updated_at,
                              expires_at)
        SELECT 'anniversary', %(subject)s, %(urge)s, %(now)s, %(now)s, %(exp)s
         WHERE NOT EXISTS (
               SELECT 1 FROM impulses
                WHERE kind = 'anniversary'
                  AND coalesce(subject, '') = coalesce(%(subject)s, '')
                  AND created_at > %(now)s - make_interval(secs => %(gap)s)
         )
        RETURNING id
        """,
        {"subject": subject, "urge": amount, "now": now, "exp": expires_at,
         "gap": within_hours * 3600.0},
    ).fetchone()
    return row is not None


def strongest_impulse(conn, now, floor: float):
    """Самый сильный несказанный повод выше порога. Протухшее не считается.

    Протухшее не удаляется, а пропускается: строка — свидетельство, что повод
    был, и по ней потом будет видно, о чём персонаж хотел заговорить и не
    успел. Убирать её — работа Curator'а (Фаза 5), как и с эпизодами.
    """
    return conn.execute(
        """
        SELECT id, kind, subject, urge, created_at, updated_at
          FROM impulses
         WHERE spoken_at IS NULL
           AND urge >= %s
           AND (expires_at IS NULL OR expires_at > %s)
         ORDER BY urge DESC, id
         LIMIT 1
        """,
        (floor, now),
    ).fetchone()


def mark_spoken(conn, impulse_id: int, now) -> None:
    """Повод отработан. Только он — приглушение соседей отдельной функцией.

    Разделено на Шаге 37: тишина считается на лету и строки в таблице не
    имеет, а приглушать соседей после неё всё равно надо. Слитая функция
    потребовала бы `impulse_id = None` со смыслом «отметить нечего, но
    приглушить надо» — то есть параметр, чьё отсутствие меняет смысл вызова.
    """
    conn.execute("UPDATE impulses SET spoken_at = %s WHERE id = %s",
                 (now, impulse_id))


def damp_impulses(conn, factor: float) -> None:
    """Приглушить все несказанные поводы.

    Зовётся после ЛЮБОЙ сказанной реплики, а не только после той, что выросла
    из хранимого импульса. Приглушение не косметика: заговорив о погоде,
    персонаж заодно нарушил и тишину, и оставить остальные побуждения
    нетронутыми значило бы дать ему повод заговорить снова через паузу.
    Гасится всё, потому что человек услышал ОДНУ реплику, а не реплику про
    погоду.
    """
    conn.execute(
        "UPDATE impulses SET urge = urge * %s WHERE spoken_at IS NULL",
        (factor,),
    )


def open_impulses(conn) -> list[dict]:
    """Все несказанные, сильные сверху. Для инспектора и сбруи."""
    rows = conn.execute(
        """
        SELECT id, kind, subject, urge, created_at, updated_at, expires_at
          FROM impulses WHERE spoken_at IS NULL
         ORDER BY urge DESC, id
        """
    ).fetchall()
    return [
        {"id": r["id"], "kind": r["kind"], "subject": r["subject"],
         "urge": round(float(r["urge"]), 3),
         "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"]),
         "expires_at": iso(r["expires_at"])}
        for r in rows
    ]


# =============================================================================
# Обещания (Шаг 43). Долг, а не побуждение
# =============================================================================
def add_promise(conn, due_at, text: str, message_id=None, now=None) -> int:
    """Записать обещание. Возвращает id.

    `created_at` передаётся, а не берётся из `now()` в SQL: писатель живёт в
    T2, то есть может отстать от разговора на минуты, а `promises_due_ck`
    сравнивает срок именно с моментом, когда просили. Возьми базу за часы —
    и обещание «через минуту», записанное с опозданием, упало бы на проверке.
    """
    return conn.execute(
        """
        INSERT INTO promises (created_at, due_at, text, message_id)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (now, due_at, text, message_id),
    ).fetchone()["id"]


def close_acknowledged_promises(conn, spoke_at) -> int:
    """Закрыть напомненное, на что собеседник отозвался. Возвращает сколько.

    Подтверждение — любая реплика человека ПОСЛЕ напоминания, а не слово
    «принял»: см. `0007_promises.sql`. Метка берётся из `agent`, потому что
    там она уже есть — заводить запрос к `messages` ради того же числа значило
    бы спросить самую длинную таблицу о том, что лежит в самой короткой.
    """
    if spoke_at is None:
        return 0
    rows = conn.execute(
        """
        UPDATE promises SET closed_at = %(now)s
         WHERE closed_at IS NULL
           AND said_at IS NOT NULL
           AND said_at < %(spoke)s
        RETURNING id
        """,
        {"now": spoke_at, "spoke": spoke_at},
    ).fetchall()
    return len(rows)


def due_promise(conn, now, repeat_after_hours: float):
    """Самое раннее обещание, о котором пора сказать, или `None`.

    Сроком, а не силой: у долгов нет `ORDER BY urge`, и первым идёт тот, кто
    ждёт дольше. Одно за заход — человек услышит одну реплику, а не список.

    Ещё не сказанное берётся сразу по наступлении срока; сказанное — не раньше
    чем через `repeat_after_hours`, и только если его не закрыли как
    подтверждённое.
    """
    return conn.execute(
        """
        SELECT id, created_at, due_at, text, said_at, repeats
          FROM promises
         WHERE closed_at IS NULL
           AND due_at <= %(now)s
           AND (said_at IS NULL
                OR said_at <= %(now)s - make_interval(secs => %(gap)s))
         ORDER BY due_at, id
         LIMIT 1
        """,
        {"now": now, "gap": repeat_after_hours * 3600.0},
    ).fetchone()


def mark_promise_said(conn, promise_id: int, now, *, close: bool) -> None:
    """Напомнил. `close` — попытки исчерпаны, больше не возвращаться.

    Закрытие решает вызывающий, а не SQL: предел попыток — правило поведения
    («спросить один раз — забота, три — надзор»), и жить ему рядом с
    заслонками инициативы, а не в хранилище.
    """
    conn.execute(
        """
        UPDATE promises
           SET said_at = %(now)s,
               repeats = repeats + 1,
               closed_at = CASE WHEN %(close)s THEN %(now)s ELSE closed_at END
         WHERE id = %(id)s
        """,
        {"now": now, "close": close, "id": promise_id},
    )


def open_promises(conn) -> list[dict]:
    """Все незакрытые, ближний срок сверху. Для инспектора и сбруи."""
    rows = conn.execute(
        """
        SELECT id, created_at, due_at, text, said_at, repeats
          FROM promises WHERE closed_at IS NULL
         ORDER BY due_at, id
        """
    ).fetchall()
    return [
        {"id": r["id"], "text": r["text"], "repeats": r["repeats"],
         "created_at": iso(r["created_at"]), "due_at": iso(r["due_at"]),
         "said_at": iso(r["said_at"])}
        for r in rows
    ]


# =============================================================================
# Очередь входящих (Шаг 28). Потребитель есть, демона ещё нет
# =============================================================================
def push_inbox(conn, text: str, now) -> int:
    """Положить реплику в очередь. Единственный писатель, которому это можно.

    В Фазе 4b сюда будет писать Node — и ровно это, одну строку; всё
    остальное в базе по-прежнему пишет агент. С Шага 28 через ту же дверь
    ходит и REPL: у клиента нет своего входа, иначе `agent.py` принёс бы
    вторую копию провода, обязанную совпадать с первой.
    """
    return conn.execute(
        "INSERT INTO inbox (ts, text) VALUES (%s, %s) RETURNING id",
        (now, (text or "").strip()),
    ).fetchone()["id"]


def pending(conn) -> list[dict]:
    """Вся непомеченная пачка, по возрастанию id. Без блокировки — намеренно.

    Роадмап (Шаг 22) предписывал `FOR UPDATE SKIP LOCKED`, и здесь его нет.
    Причина в том же правиле, из-за которого отклонили `claimed_at`: **лок не
    переживает вызов LLM**. Взять его тут значило бы отпустить через
    миллисекунду, до того как ответ начнёт считаться, — то есть получить
    защиту, которая ни от чего не защищает, но выглядит как защита. Это
    ровно тот молчаливый no-op, за который Шаг 23 поимённо вычищал
    `commit()`, а Шаг 25 завёл `_require_unit`.

    Настоящая развязка — условная пометка в T1 (`mark_handled`), и она
    атомарна сама по себе. `LIMIT` нет тоже: пока не известна последняя
    реплика, неизвестно, что склеивать.
    """
    return [dict(r) for r in conn.execute(
        "SELECT id, ts, text FROM inbox WHERE handled_at IS NULL ORDER BY id"
    ).fetchall()]


def mark_handled(conn, ids, now, reply_id=None) -> list[int]:
    """Пометить пачку обработанной. Возвращает id, которые пометили МЫ.

    Условие `handled_at IS NULL` — единственное, что стоит между двумя
    поднявшимися демонами. Пометка идёт ТЕМ ЖЕ коммитом, что и строки
    `messages` (решение Шага 22): иначе она стала бы отдельным фактом,
    способным разойтись с записью обмена.

    `reply_id` — терминальное состояние для клиента. Проставляется всем
    строкам пачки, включая склеенные: их слова тоже получили ответ, просто
    один на всех. Пусто он остаётся у реплик, отброшенных разрывом сессии, —
    и это второе терминальное состояние, «прочитано поздно, ответа не будет».
    """
    if not ids:
        return []
    rows = conn.execute(
        "UPDATE inbox SET handled_at = %s, reply_id = %s "
        " WHERE id = ANY(%s) AND handled_at IS NULL RETURNING id",
        (now, reply_id, list(ids)),
    ).fetchall()
    return [r["id"] for r in rows]


def bump_dropped(conn, n: int) -> bool:
    """Прибавить к счётчику потерянного у ОТКРЫТОЙ сессии.

    Текста нет, счёт есть — колонка `sessions.dropped` существует ровно для
    этого. Зовётся до `close_session`: та считает длину эпизода как
    `обмены + dropped`, и после закрытия прибавлять уже не к чему.
    """
    if n <= 0:
        return False
    return bool(conn.execute(
        "UPDATE sessions SET dropped = dropped + %s "
        " WHERE closed_at IS NULL RETURNING id", (n,)
    ).fetchone())


def working_memory(conn, limit: int = WORKING_MEMORY_EXCHANGES) -> list[dict]:
    """Разговор открытой сессии в форме сообщений модели.

    Пар здесь не собирается — в отличие от `summary_buffer`, которому пары
    нужны для транскрипта. Модели нужна лента ролей, и читать её лентой
    честнее: сборка пар предполагает, что строки идут строго по две, а
    предполагать это на пути, который переживёт падение процесса, не стоит.
    `LIMIT` при этом в обменах, а не в строках, — отсюда `limit * 2`.

    Сессия берётся только открытая: закрытая уже пересказана эпизодом.
    Открытой нет — рабочей памяти нет, и это не ошибка, а первый ход.
    """
    row = conn.execute(
        "SELECT id FROM sessions WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return []

    # Тот же приём, что в `summary_buffer`: последние N по убыванию `id`,
    # затем разворот. `ORDER BY id`, а не `ts`: в пределах одного обмена обе
    # строки пишутся с ОДНИМ `now`, и сортировка по времени вернула бы их в
    # произвольном порядке — то есть иногда ответом перед репликой.
    rows = conn.execute(
        "SELECT role, text FROM messages WHERE session_id = %s "
        "ORDER BY id DESC LIMIT %s",
        (row["id"], limit * 2),
    ).fetchall()
    rows.reverse()
    return [message(r["role"], r["text"]) for r in rows]


def session_stale(conn, now, gap_hours: float = SESSION_GAP_HOURS) -> bool:
    row = conn.execute(
        """
        SELECT max(m.ts) AS last FROM messages m
          JOIN sessions s ON s.id = m.session_id
         WHERE s.closed_at IS NULL
        """
    ).fetchone()
    last = row and row["last"]
    if last is None:
        return False
    return (now - last).total_seconds() / 3600.0 >= gap_hours


def summary_buffer(conn, limit: int = SUMMARY_EXCHANGES_LIMIT) -> dict:
    """Разговор открытой сессии для суммаризатора: лента реплик + сколько скрыто.

    **Пар здесь больше не собирается (Шаг 35), и это исправление ошибки, а
    не смена вкуса.** Прежний код брал строки по две — `rows[i]` считался
    репликой человека, `rows[i + 1]` ответом, — и держался на предположении,
    что роли строго чередуются. Предположение было верно ровно до тех пор,
    пока персонаж только отвечал. С первой же сказанной по своей воле
    репликой чередование ломается, и сборка пар не падает, а СЪЕЗЖАЕТ: чужая
    реплика склеивается с чужим ответом, и суммаризатор получает разговор,
    которого не было. Молча, и тем вернее, чем длиннее сессия.

    Форма — та же, что у `working_memory`, и по той же причине, которая там
    записана: «читать её лентой честнее, потому что сборка пар предполагает,
    что строки идут строго по две». Один и тот же разговор двумя способами
    читали два места; правым оказалось то, которое ничего не предполагало.

    `spontaneous` доезжает до читателя: транскрипт обязан различать «его
    спросили — он ответил» и «он заговорил сам». Без этого выжимка
    приписывает собеседнику реплики, которых тот не подавал.

    Буфер СОЗНАТЕЛЬНО не входит в снимок: он вход хода, а не память, и
    `mind` получает его отдельным аргументом.
    """
    row = conn.execute(
        "SELECT id, started_at, dropped FROM sessions WHERE closed_at IS NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {}

    sid, lost = row["id"], row["dropped"]
    # Счёт в РЕПЛИКАХ, а не в обменах: обмен перестал быть единицей разговора
    # ровно тогда, когда появилась реплика без пары.
    total = conn.execute(
        "SELECT count(*) AS n FROM messages WHERE session_id = %s",
        (sid,),
    ).fetchone()["n"]
    if not total and not lost:
        return {}

    # `limit` в обменах, а лента в строках — отсюда `limit * 2`. Множитель
    # остаётся прежним намеренно: он про то, сколько разговора влезает в
    # промпт выжимки, и от смены единицы счёта эта величина не менялась.
    rows = conn.execute(
        """
        SELECT ts, role, text, spontaneous FROM messages
         WHERE session_id = %s
         ORDER BY id DESC
         LIMIT %s
        """,
        (sid, limit * 2),
    ).fetchall()
    rows.reverse()

    return {
        "started_at": iso(row["started_at"]),
        "messages": [
            {"ts": iso(r["ts"]), "role": r["role"], "text": r["text"],
             "spontaneous": r["spontaneous"]}
            for r in rows
        ],
        # Два слагаемых, и оба честные:
        #   `total - len(rows)` — реплики ЕСТЬ в базе, но не показаны
        #     (предел выжимки). Верно по построению, от согласованности
        #     констант не зависит.
        #   `lost` — реплик НЕТ вовсе. В жизни этого не бывает (база хранит
        #     всё), но бывает у фикстура, доставшегося от JSON-эпохи, и
        #     будет у сессий, подчищенных Curator'ом (Фаза 5). Колонка
        #     `sessions.dropped` существует ровно для «знаем, что было
        #     больше, а текста нет».
        "dropped": lost + max(0, total - len(rows)),
    }


# =============================================================================
# Шина уведомлений (Шаг 29). Postgres — не только память, но и точка встречи
# =============================================================================
# Два канала, и направление у каждого одно:
#   `inbox_new`   — клиент положил реплику, агенту есть что забрать;
#   `reply_ready` — агент ответил, клиенту есть что показать.
#
# Прямого HTTP между процессами нет и не будет: у них уже есть общая точка
# встречи, и она транзакционная (ROADMAP, 4b). В Фазе 4b на место `cli.py`
# встанет Node с тем же протоколом — он про то, что лежит в базе, а не про
# то, кто читает.
#
# **Полезная нагрузка не несёт смысла.** Клиент на пробуждении перечитывает
# СВОЮ строку `inbox` и смотрит на её терминальное состояние; агент —
# опрашивает очередь целиком. Полагаться на payload значило бы завести
# второй источник истины рядом с таблицей, причём такой, который теряется
# при разрыве соединения. Payload остаётся только для журнала.
CHANNEL_INBOX = "inbox_new"
CHANNEL_REPLY = "reply_ready"


def listen(conn, channel: str) -> None:
    """Подписаться на канал. Требует autocommit — иначе подписка отложена.

    `LISTEN` внутри транзакции вступает в силу только на коммите, и до
    него уведомления идут мимо. У нас соединение в autocommit (Шаг 23),
    так что это верно само собой; проверка стоит потому, что режим отказа
    здесь молчаливый — подписка «есть», уведомлений нет.
    """
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError(
            f"LISTEN {channel} внутри транзакции: подписка вступит в силу "
            "только на коммите, а до него уведомления пройдут мимо"
        )
    # Имя канала — идентификатор, параметром его не подставить; отсюда
    # `pg_notify`/`format`. Каналы в проекте только из констант выше, но
    # склейка строк с именем всё равно идёт через `psycopg.sql`, чтобы
    # правило «SQL не собирается конкатенацией» не имело исключений.
    conn.execute(psycopg.sql.SQL("LISTEN {}").format(psycopg.sql.Identifier(channel)))


def notify(conn, channel: str, payload: str = "") -> None:
    """Толкнуть уведомление. Функцией, а не командой — ради параметров.

    `NOTIFY` берёт канал идентификатором, `pg_notify` — обычной строкой, и
    потому она параметризуется как всё остальное.

    Под autocommit уведомление уходит сразу. **Внутри `unit()` оно ждёт
    коммита**, и это ровно то поведение, какое нужно: разбудить клиента
    раньше, чем запись стала видимой, значило бы позвать его смотреть на
    то, чего ещё нет.
    """
    conn.execute("SELECT pg_notify(%s, %s)", (channel, payload))


def _require_unit(conn) -> None:
    """Упасть, если вызов идёт вне единицы записи.

    Нужен ровно там, где код берёт **блокировку**. Под `autocommit` (Шаг 23)
    стейтмент вне явной транзакции сам себе транзакция, и `SELECT ... FOR
    UPDATE` в нём отпускает замок в ту же миллисекунду, в которую взял, —
    то есть защита не падает, а притворяется работающей. Это ровно тот
    режим отказа, из-за которого на Шаге 23 поимённо вычищали `commit()`:
    молчаливый no-op хуже исключения, потому что его не видно на зелёном
    прогоне.
    """
    if conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError(
            "close_session вызван вне единицы записи: блокировка строки сессии "
            "под autocommit снимается сразу и ни от чего не защищает. "
            "Оберните вызов в `with eng.unit():` (T3)."
        )


def close_session(conn, now, summary: str | None = None) -> dict | None:
    """Закрыть сессию, сложив её в эпизод. Реплики остаются в `messages`.

    В JSON буфер обнулялся — разговор существовал только как выжимка. Здесь
    он остаётся целиком, и это задел под Фазы 5 и 7: Curator сможет
    пересобрать выжимку, Dreamer — перечитать разговор, а не пересказ.

    **`FOR UPDATE` — единственное место проекта, где лок уместен (Шаг 25).**
    Уникальный индекс `sessions_one_open_uq` двойное закрытие не ловит и
    поймать не может: он запрещает вторую ОТКРЫТУЮ сессию, а здесь обе
    транзакции работают с одной и той же существующей строкой и обе законно
    ставят ей `closed_at`. Фантома нет — есть строка, которую надо запереть,
    и потому лечится это локом, а не индексом. Воспроизводится двумя
    потоками: без `FOR UPDATE` получаются два эпизода на одну сессию, оба с
    полным числом обменов, и разговор оказывается прожит дважды.

    Второй ждущий после снятия замка перепроверяет `WHERE closed_at IS NULL`
    (READ COMMITTED так и делает), строки не находит и уходит с `None` —
    отдельная ветка «кто-то опередил» не нужна, её выражает сам предикат.
    """
    _require_unit(conn)
    row = conn.execute(
        "SELECT id, started_at, dropped FROM sessions WHERE closed_at IS NULL "
        "ORDER BY id DESC LIMIT 1 FOR UPDATE"
    ).fetchone()
    if not row:
        return None

    sid = row["id"]
    # Считаются реплики человека И сказанные персонажем по своей воле.
    #
    # До Шага 35 здесь стоял только `role = 'user'`, и это было верно, пока
    # разговор состоял из обменов: каждая реплика человека тянула за собой
    # ровно один ответ, и счёт по одной стороне давал длину разговора.
    # С инициативой появился разговор, в котором реплик человека НОЛЬ —
    # персонаж заговорил, ему не ответили, — и прежний счёт дал бы `total = 0`,
    # то есть сессия закрылась бы БЕЗ эпизода. Монолог исчез бы из памяти
    # целиком, причём молча: ветка «нечего записывать» выглядит как штатная.
    #
    # Ответы по-прежнему не считаются: они следствие реплики, а не событие
    # разговора. Поэтому у сессий без инициативы число не изменилось ни на
    # единицу — эталоны это подтверждают.
    stats = conn.execute(
        """
        SELECT count(*) AS n, min(ts) AS first_ts, max(ts) AS last_ts
          FROM messages
         WHERE session_id = %s AND (role = 'user' OR spontaneous)
        """,
        (sid,),
    ).fetchone()

    # Длина эпизода — весь разговор, включая то, чего в тексте не осталось.
    # `len(items) + dropped` в JSON-движке означало ровно это.
    total = stats["n"] + row["dropped"]

    if not total:
        conn.execute("UPDATE sessions SET closed_at = %s WHERE id = %s", (now, sid))
        return None

    ep = conn.execute(
        """
        INSERT INTO episodes (started_at, ended_at, exchanges, summary)
        VALUES (%s, %s, %s, %s) RETURNING id, started_at, ended_at, exchanges, summary
        """,
        (row["started_at"] or stats["first_ts"], stats["last_ts"] or now,
         total, summary),
    ).fetchone()

    conn.execute(
        "UPDATE sessions SET closed_at = %s, episode_id = %s WHERE id = %s",
        (now, ep["id"], sid),
    )
    return {
        "id": f"ep_{ep['id']}",
        "started_at": iso(ep["started_at"]),
        "ended_at": iso(ep["ended_at"]),
        "exchanges": ep["exchanges"],
        "summary": ep["summary"],
    }

def enqueue_digest(conn, reply_id, findings) -> None:
    conn.execute(
        "INSERT INTO followups (reply_id, findings) VALUES (%s, %s)",
        (reply_id, json.dumps(findings) if findings is not None else None),
    )


def next_digest(conn):
    return conn.execute(
        "SELECT id, reply_id, findings FROM followups "
        "WHERE done_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone()


def mark_digest_done(conn, followup_id, now) -> None:
    conn.execute(
        "UPDATE followups SET done_at = %s WHERE id = %s",
        (now, followup_id),
    )


def exchange_by_reply(conn, reply_id):
    return conn.execute(
        """
        -- `asked_at` добавлен на Шаге 43. T2 отстаёт от разговора на минуты,
        -- а может — на часы, если демон лежал; «через пять часов» обязано
        -- отсчитываться от момента просьбы, а не от момента разбора.
        -- `reply_id` тоже отдаётся наружу: обещанию нужен указатель на то,
        -- откуда оно взялось.
        SELECT u.text AS user_text, a.text AS answer,
               u.ts AS asked_at, u.id AS asked_id
          FROM messages a
          JOIN messages u
            ON u.session_id = a.session_id
           AND u.role = 'user'
           AND u.id < a.id
         WHERE a.id = %s AND a.role = 'assistant'
         ORDER BY u.id DESC
         LIMIT 1
        """,
        (reply_id,),
    ).fetchone()
