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


def build_snapshot(conn, now, limit: int = 7) -> Turn:
    """Снимок хода из базы. Та же форма, что у `store.build_snapshot`.

    Пять запросов на ход, и это осознанно: агрегировать всё в один
    `JOIN` значило бы собирать снимок в SQL, а он собирается в Python —
    иначе `mind` окажется зависим от формы запроса.
    """
    agent = conn.execute(
        """
        SELECT name, traits, mood, place_label, outside_latch
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

    by_object = _assertions_by_object(conn, [r["id"] for r in top] + [SELF_ID])

    return Turn(
        name=agent.get("name", "Некто"),
        traits=tuple(agent.get("traits") or ()),
        mood=agent.get("mood", "нейтральное"),
        place_label=agent.get("place_label"),
        self_assertions=by_object.get(SELF_ID, []),
        outside_latch=agent.get("outside_latch"),
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
    conn.execute("TRUNCATE objects, assertions, episodes, aliases, sessions, "
                 "messages, impulses RESTART IDENTITY CASCADE")
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
        UPDATE agent SET name=%s, traits=%s, mood=%s, place_label=%s,
               place_lat=%s, place_lon=%s, outside_latch=%s, last_exchange_ts=%s,
               last_search_ts=NULL
         WHERE id = 1
        """,
        (
            self.get("name", "Некто"),
            list(self.get("traits", [])),
            self.get("mood", "нейтральное"),
            place.get("label"),
            place.get("lat"),
            place.get("lon"),
            json.dumps(self.get("outside")) if self.get("outside") else None,
            state.get("last_exchange_ts"),
        ),
    )

    # Объект №0 — строка в той же таблице, что и все остальные.
    conn.execute(
        "INSERT INTO objects (id, type, label, label_norm, salience) "
        "VALUES (%s, 'self', 'self', 'self', 0)",
        (SELF_ID,),
    )
    _insert_assertions(conn, SELF_ID, self.get("assertions", []))

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
                mode: str = "bump", expires_at=None) -> None:
    """Записать побуждение. `bump` — прибавить к накопленному, `set` — поверх.

    Два режима, потому что поводы двух пород (см. `cycle.Urge`). Событие
    прибавляется и тает; состояние вычисляется по настоящему и пишется
    поверх, поэтому затухание к нему не применяется — оно бы вычло из
    величины, которая уже верна на данный момент.

    `ON CONFLICT` по частичному уникальному индексу, а не «выбрать и решить»:
    check-then-insert здесь дал бы того же фантома, что у объектов и сессий,
    и дал бы его тем вернее, что побуждение трогают каждый заход.

    Конструкция индекса названа выражением, а не именем колонки:
    `coalesce(subject, '')` — часть ключа, и вывести её Postgres не может.

    Затухание применяется НА ЗАПИСИ, а не по таймеру: повод, о котором ничто
    не напомнило, обязан слабеть сам, но заводить ради этого фоновую уборку
    значило бы завести второго писателя. Тот же приём, что у
    `web._evict_stale`: чистка на обращении.
    """
    if mode not in ("bump", "set"):
        raise ValueError(f"record_urge: неизвестный режим {mode!r}")

    # Затухание выражено в SQL, а не посчитано в Python, по той же причине,
    # что и `effective_salience`: величина, лежащая в базе, должна пересчиты-
    # ваться там же, где лежит, иначе между чтением и записью появляется щель.
    grown = (
        "impulses.urge * pow(%(base)s, greatest(EXTRACT(EPOCH FROM "
        "(EXCLUDED.updated_at - impulses.updated_at)) / 3600.0, 0.0) "
        "/ %(half)s) + EXCLUDED.urge"
    )
    conn.execute(
        f"""
        INSERT INTO impulses (kind, subject, urge, created_at, updated_at, expires_at)
        VALUES (%(kind)s, %(subject)s, %(urge)s, %(now)s, %(now)s, %(exp)s)
        ON CONFLICT (kind, coalesce(subject, '')) WHERE spoken_at IS NULL
        DO UPDATE SET
            urge = {grown if mode == "bump" else "EXCLUDED.urge"},
            updated_at = EXCLUDED.updated_at,
            expires_at = COALESCE(EXCLUDED.expires_at, impulses.expires_at)
        """,
        {"kind": kind, "subject": subject, "urge": amount, "now": now,
         "exp": expires_at, "base": IMPULSE_DECAY_BASE,
         "half": IMPULSE_HALFLIFE_HOURS},
    )


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


def mark_spoken(conn, impulse_id: int, now, damp: float) -> None:
    """Повод отработан. Соседние — приглушить.

    Приглушение соседей не косметика: заговорив о погоде, персонаж заодно
    нарушил и тишину, и оставить побуждение `silence` нетронутым значило бы
    дать ему повод заговорить снова через минуту. Гасится всё несказанное,
    а не только соседи того же рода, потому что человек услышал ОДНУ реплику,
    а не реплику про погоду.
    """
    conn.execute("UPDATE impulses SET spoken_at = %s WHERE id = %s",
                 (now, impulse_id))
    conn.execute(
        "UPDATE impulses SET urge = urge * %s WHERE spoken_at IS NULL",
        (damp,),
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
        SELECT u.text AS user_text, a.text AS answer
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
