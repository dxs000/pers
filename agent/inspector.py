"""Окно внутрь: читающая сторона инспектора (Шаг 30).

Фаза 4a обещает браузер, а этот шаг браузера не делает. Порядок такой же,
каким был на Шаге 17, и по той же причине: **сначала контракт между слоями,
потом технология**. Тогда снимок для `mind` собрали на знакомом движке до
переезда в Postgres — и стык, который трижды кусал на Шаге 12, развалился
надвое. Здесь то же самое: если первым делом поставить Express, знание о
том, ЧТО ЗНАЧИТ показать память (порядок затухания, порог отсечки, ось
источника, отбор фактов в промпт, три состояния очереди), уедет в JS второй
копией. Копий, обязанных совпадать, проект вычистил уже четыре — формулу
затухания, досев объекта №0, `sky_snapshot` в сбруе, форму `iso`, — и
каждый раз задним числом. А эту сбруя не удержала бы вовсе: `golden.py`
ходит через фасад и сравнивает байты, до Node ей не дотянуться.

Поэтому читающая сторона написана на Python, до всякого HTTP, и покрыта
восемнадцатым сценарием. Express в 4a останется тонким видом: взял
структуру, отдал JSON.

## Почему соединение, а не фасад

`engine.PgEngine.__init__` зовёт `_ensure_agent`, а тот ПИШЕТ (доливает
черты, досевает объект №0). То есть открыть фасад на read-only соединении
физически нельзя — и это не досадность, а верный ответ: фасад есть дверь
писателя, и список его методов ровно про то, что `agent` умеет делать с
памятью. Инспектор в эту дверь не входит. У `cli.py` (Шаг 29) развязка та
же и по тому же поводу.

Отсюда сигнатуры: всё принимает `conn`, а не `eng`.

## Чего здесь нет

**Часов.** `now` приезжает параметром, как в `mind`: нечистое живёт на краю.
**Сети.** Инспектор не ходит ни за погодой, ни в поиск.
**Записи.** Ни строки `UPDATE`. И это не обещание: соединение открывается
`store_pg.connect(read_only=True)`, и попытку записи отклоняет база.
**HTTP.** Транспорт — работа следующего шага; здесь только то, что он отдаст.

## Что показывает и чего не показывает страница промпта

`prompt()` собирает системный промпт из ПАМЯТИ — и не собирает того, что
принадлежит ходу: неба и погоды за окном, находок ретривера. Это названо
списком `caveats`, а не умолчано: инспектор, показывающий «промпт» на
полблока беднее настоящего, врал бы ровно там, где на него будут смотреть
как на источник истины.

**Небо с Шага 32 показывается.** Оно считается офлайн и детерминированно,
то есть принадлежало странице по природе данных, а не попадало на неё из-за
формы вызова: `cycle.sky_snapshot` брал место через фасад, а фасад —
дверь писателя. Расчёт переехал в `sky.local_snapshot`, место приезжает
параметром, и копии при этом не завелось: считает по-прежнему один код, тот
же, которым считает ход.

**Погода не показывается и не будет.** Это сетевой запрос, а латч среды
инспектор трогать не вправе: страница, обновляющая латч открытием, меняла бы
ровно то, на что человек пришёл смотреть. Разница между «небом» и «погодой»
здесь не в важности, а в том, что одно чистая арифметика, а другое край.
"""

import sky
import store_pg
from snapshot import iso

# Три состояния строки `inbox` (Шаг 28). Имена живут ЗДЕСЬ, а не в `cli`,
# потому что читателей у правила стало два: оболочка спрашивает про свою
# строку, инспектор показывает очередь целиком. Правило одно — «помечено ли
# и есть ли ответ», — и разъехаться ему нельзя: разойдись эти два чтения, и
# человек увидел бы в инспекторе «ответ есть» там, где клиент ждёт до
# таймаута.
WAITING, ANSWERED, DROPPED = "waiting", "answered", "dropped"

# Причина, по которой факт не доехал до промпта. Пустая строка — доехал.
CUT_CONFIDENCE = "низкое доверие"
CUT_LIMIT = "не вошёл в предел"


def connect(dsn: str | None = None, *, test: bool = False):
    """Соединение инспектора: то же, что у всех, но писать им нельзя."""
    return store_pg.connect(dsn, test=test, read_only=True)


def inbox_state(row) -> str:
    """Строка `inbox` -> одно из трёх состояний. Единственное место правила.

    Строки нет вовсе — `WAITING`, и это не ошибка чтения: клиент переживает
    `db.py --reset`, который уносит очередь целиком, и подождать до таймаута
    там честнее, чем уронить оболочку человеку в лицо.
    """
    if not row or row.get("handled_at") is None:
        return WAITING
    return ANSWERED if row.get("reply_id") is not None else DROPPED


# =============================================================================
# Объект №0 и счётчики
# =============================================================================
def overview(conn, now) -> dict:
    """Кто персонаж сейчас и сколько чего у него накопилось.

    Счётчики выбраны не для полноты, а по читателям, которые появятся:

    - `episodes_blank` — эпизоды с `summary IS NULL`. Тот самый мёртвый
      груз, который Curator (Фаза 5) однажды уберёт; сегодня узнать, сколько
      его, можно только руками в `psql`.
    - `inbox_pending` — непомеченное в очереди. Первый вопрос при живом
      демоне: он думает или он лёг.
    - `session_open` — открытая сессия. Инвариант «она одна» держит индекс,
      а вот «она есть» — свойство момента, и его видно только так.
    """
    agent = conn.execute(
        """
        SELECT name, traits, mood, place_label, place_lat, place_lon,
               place_source, place_asked, place_resolved_at,
               outside_latch, last_exchange_ts
          FROM agent WHERE id = 1
        """
    ).fetchone() or {}

    counts = conn.execute(
        """
        SELECT (SELECT count(*) FROM objects WHERE id <> 0)          AS objects,
               (SELECT count(*) FROM assertions WHERE object_id <> 0) AS facts,
               (SELECT count(*) FROM assertions WHERE object_id = 0)  AS self_facts,
               (SELECT count(*) FROM episodes)                        AS episodes,
               (SELECT count(*) FROM episodes WHERE summary IS NULL)  AS episodes_blank,
               (SELECT count(*) FROM sessions)                        AS sessions,
               (SELECT count(*) FROM messages)                        AS messages,
               (SELECT count(*) FROM inbox WHERE handled_at IS NULL)  AS inbox_pending
        """
    ).fetchone()

    open_session = conn.execute(
        "SELECT id FROM sessions WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return {
        "as_of": iso(now),
        "name": agent.get("name"),
        # `traits` приезжает из `TEXT[]` списком; кортеж тут был бы формой
        # снимка, а инспектор отдаёт то, что уедет в JSON.
        "traits": list(agent.get("traits") or ()),
        "mood": agent.get("mood"),
        "place": {
            "label": agent.get("place_label"),
            "lat": agent.get("place_lat"),
            "lon": agent.get("place_lon"),
            "source": agent.get("place_source"),
            "asked": agent.get("place_asked"),
            "resolved_at": iso(agent.get("place_resolved_at")),
        },
        "outside_latch": agent.get("outside_latch"),
        "last_exchange": iso(agent.get("last_exchange_ts")),
        "session_open": open_session["id"] if open_session else None,
        "counts": dict(counts),
    }


# =============================================================================
# Память: объекты и факты
# =============================================================================
def objects(conn, now) -> list[dict]:
    """ВСЕ объекты, с эффективной важностью и отметкой «доехал до промпта».

    Полный список, а не top-N, и в этом половина смысла инспектора:
    **забытый объект — не удалённый**. Промпт показывает семь верхних,
    `psql` показывает таблицу без порядка, а увидеть, что «кофе» просел под
    порог, но лежит на месте, сегодня негде.

    Отметка `in_prompt` НЕ считается заново по формуле — она берётся из
    `store_pg.build_snapshot`, то есть у той же функции, которая собирает
    промпт ходу. Пересчитать порог и лимит здесь было бы проще на три
    строки и завело бы пятую копию, обязанную совпадать.
    """
    rows = conn.execute(
        """
        SELECT o.id, o.type, o.label, o.salience, o.last_seen,
               effective_salience(o.salience, o.last_seen, %s) AS eff,
               coalesce(array_agg(a.alias ORDER BY a.id)
                        FILTER (WHERE a.alias IS NOT NULL), '{}') AS aliases,
               (SELECT count(*) FROM assertions s WHERE s.object_id = o.id) AS facts
          FROM objects o
          LEFT JOIN aliases a ON a.object_id = o.id
         WHERE o.id <> %s
         GROUP BY o.id
         ORDER BY eff DESC, o.id
        """,
        (now, store_pg.SELF_ID),
    ).fetchall()

    turn = store_pg.build_snapshot(conn, now)
    in_prompt = {int(o["id"].removeprefix("obj_")) for o in turn.objects}

    return [
        {
            "id": r["id"],
            "type": r["type"],
            "label": r["label"],
            "aliases": list(r["aliases"]),
            "salience": r["salience"],
            # Округление — ради переносимости эталона, а не ради красоты.
            # `eff` считает `pow` в базе, и разные сборки Postgres вправе
            # разойтись в последнем бите; сбруя сравнивает БАЙТЫ, и такое
            # расхождение покраснело бы на чужой машине, ничего не значив.
            # Шесть знаков заведомо больше, чем различает человек, и
            # заведомо крупнее, чем ULP. Порог при этом считается по
            # НЕокруглённому: округление — свойство показа, а не решения.
            "eff": round(r["eff"], 6),
            "last_seen": iso(r["last_seen"]),
            "facts": r["facts"],
            "in_prompt": r["id"] in in_prompt,
            # Порог отсечки — не то же, что предел числа: под порогом объект
            # не всплывёт даже в пустой памяти, а вне предела — всплыл бы,
            # если бы не нашлось семи важнее. Разница смысловая, и человек,
            # правящий пороги, обязан её видеть.
            "below_floor": r["eff"] < store_pg.SALIENCE_FLOOR,
        }
        for r in rows
    ]


def facts(conn, object_id: int) -> list[dict]:
    """Ассершены объекта — включая те, что до промпта не доезжают, и почему.

    `object_id = 0` — это self, та же таблица (обещание «Я — объект №0»
    стало строкой ещё на 3b).

    **Колонка `cut` и есть причина, по которой шаг стоит того.** Роадмап
    третий раз подряд носит долг «`confidence` схлопнут»: экстрактор метит
    `high` всё сказанное прямо, `_TRUSTED_CONF` фактически не фильтрует, а
    развилку «чинить рубрику или обойти осью» на Шаге 15 обошли. Спорить об
    этом можно было только на глаз — последний замер («71 `high` из 73»)
    делали руками перед сбросом состояния. Теперь замер есть у любого, кто
    откроет страницу, и порог правится по данным, а не по памяти о них.

    `mind` импортируется ЛЕНИВО. Он тянет за собой `openai` и `config`, а
    у этого модуля два потребителя, которым думать не надо: `cli.py` берёт
    отсюда только имена состояний очереди, и платить за них клиентским
    импортом сетевой библиотеки незачем.
    """
    import mind

    rows = conn.execute(
        """
        SELECT id, key, value, confidence, hits, ts, source, confirmed
          FROM assertions WHERE object_id = %s ORDER BY id
        """,
        (object_id,),
    ).fetchall()

    limit = (mind.SELF_ASSERTION_LIMIT if object_id == store_pg.SELF_ID
             else mind.OBJECTS_ASSERTIONS_LIMIT)
    ranked = [dict(r, ts=iso(r["ts"])) for r in rows]

    # `_pick_assertions` возвращает ТЕ ЖЕ словари, а не копии, — поэтому
    # сравнение по `id()` законно и не требует ключа. Приватная функция
    # зовётся сознательно: альтернатива — переложить сюда отбор по рангу,
    # то есть завести ровно ту копию, ради отсутствия которой шаг и делается.
    picked = {id(a) for a in mind._pick_assertions(ranked, limit)}

    out = []
    for a in ranked:
        if id(a) in picked:
            cut = ""
        elif a["confidence"] not in mind._TRUSTED_CONF:
            cut = CUT_CONFIDENCE
        else:
            cut = CUT_LIMIT
        out.append({
            "id": a["id"],
            "key": a["key"],
            "value": a["value"],
            "confidence": a["confidence"],
            "hits": a["hits"],
            "ts": a["ts"],
            "source": a["source"],
            "confirmed": a["confirmed"],
            "in_prompt": not cut,
            "cut": cut,
        })
    return out


# =============================================================================
# Разговоры: эпизоды, сессии, реплики
# =============================================================================
def episodes(conn, limit: int | None = None) -> list[dict]:
    """Эпизоды, свежие сверху. `summary: null` — тот самый мёртвый груз.

    Порядок обратный тому, в каком их читает снимок (`ORDER BY ended_at`):
    промпту нужна лента, человеку — последнее сверху. Это не расхождение,
    а разные читатели, и потому запрос здесь свой.
    """
    rows = conn.execute(
        """
        SELECT id, started_at, ended_at, exchanges, summary, source
          FROM episodes ORDER BY ended_at DESC NULLS LAST, id DESC
         LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "started_at": iso(r["started_at"]),
            "ended_at": iso(r["ended_at"]),
            "exchanges": r["exchanges"],
            "summary": r["summary"],
            "source": r["source"],
        }
        for r in rows
    ]


def sessions(conn, limit: int | None = None) -> list[dict]:
    """Сессии со счётом реплик. `dropped` — то, чего в тексте не осталось.

    `exchanges` считается по строкам `user`, как это делает `close_session`:
    эпизод меряет разговор репликами человека, и вторая мерка в том же
    интерфейсе означала бы два разных числа под одним словом.
    """
    rows = conn.execute(
        """
        SELECT s.id, s.started_at, s.ended_at, s.closed_at, s.dropped,
               s.episode_id,
               (SELECT count(*) FROM messages m
                 WHERE m.session_id = s.id AND m.role = 'user') AS exchanges
          FROM sessions s ORDER BY s.id DESC LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "started_at": iso(r["started_at"]),
            "ended_at": iso(r["ended_at"]),
            "closed_at": iso(r["closed_at"]),
            "open": r["closed_at"] is None,
            "exchanges": r["exchanges"],
            "dropped": r["dropped"],
            "episode_id": r["episode_id"],
        }
        for r in rows
    ]


def conversation(conn, session_id: int, limit: int | None = None) -> list[dict]:
    """Реплики сессии лентой, в порядке вставки.

    `ORDER BY id`, а не `ts`, — то же решение, что в `working_memory`: две
    формы времени (Шаг 28) означают, что у ответа метка позже прихода
    реплики, но у СКЛЕЕННЫХ реплик метка одна, и сортировка по времени
    вернула бы их в произвольном порядке.

    Текст отдаётся ЦЕЛИКОМ, без `clip_text`: обрезка мотивирована местом в
    промпте выжимки (Шаг 24) и к странице отношения не имеет. Инспектор
    показывает, что лежит, а не что уедет в модель.
    """
    rows = conn.execute(
        "SELECT id, ts, role, text FROM messages WHERE session_id = %s "
        "ORDER BY id LIMIT %s",
        (session_id, limit),
    ).fetchall()
    return [
        {"id": r["id"], "ts": iso(r["ts"]), "role": r["role"], "text": r["text"]}
        for r in rows
    ]


def queue(conn, limit: int | None = None) -> list[dict]:
    """Очередь целиком, включая помеченное, с терминальным состоянием.

    До Шага 30 читающего контракта у этих состояний не было вовсе — сбруя
    лезла в таблицу напрямую и честно об этом сознавалась («завести метод
    ради артефакта значило бы завести метод, которого никто не зовёт»).
    Теперь зовёт: страница очереди — первое, куда смотрят, когда персонаж
    молчит.

    `pending()` у фасада остаётся и остаётся другим: он отдаёт РАБОТУ
    потребителю и по устройству не видит помеченного.
    """
    rows = conn.execute(
        "SELECT id, ts, text, handled_at, reply_id FROM inbox "
        "ORDER BY id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "ts": iso(r["ts"]),
            "text": r["text"],
            "handled_at": iso(r["handled_at"]),
            "reply_id": r["reply_id"],
            "state": inbox_state(r),
        }
        for r in rows
    ]


# =============================================================================
# Страница промпта
# =============================================================================
# Что здесь НЕ собирается и почему — список, а не умолчание.
PROMPT_CAVEATS = (
    "погоды за окном нет: это сетевой запрос, а латч инспектор трогать не "
    "вправе (небо показывается с Шага 32 — оно офлайн)",
    "находок ретривера нет: они принадлежат реплике, а не памяти",
    "это промпт на запрошенный момент, а не тот, что уехал в модель на "
    "последнем ходу: сохранённых промптов в проекте нет",
)


def prompt(conn, now, tz) -> dict:
    """Системный промпт из памяти на момент `now`. Ничего не пишет.

    Собирается теми же двумя вызовами, что у хода (`build_snapshot` +
    `build_system_prompt`), и намеренно НЕ через `cycle.prompt_and_latch`:
    та половиной себя пишет латч среды. Инспектор, обновляющий латч
    открытием страницы, менял бы ровно то, на что человек пришёл смотреть,
    — и следующий ход сравнивал бы погоду не с прошлым разом, а с чужим
    просмотром.

    Пояс приходит параметром, а не из `config`: он настройка запуска, и
    страница может показывать промпт в поясе персонажа, оставаясь при этом
    сверяемой эталоном на любой машине. Небо считается на тот же `now`, что
    и память, — значит `?at=` двигает и его: страница на завтрашнее утро
    покажет завтрашний рассвет, а не сегодняшний.
    """
    from mind import build_system_prompt

    turn = store_pg.build_snapshot(conn, now)
    previous = store_pg.last_exchange(conn)
    place = store_pg.place(conn)
    snap = sky.local_snapshot(place.get("lat"), place.get("lon"), now, tz)
    text = build_system_prompt(turn, now.astimezone(tz), snap, None, previous, None)
    return {"as_of": iso(now), "text": text, "caveats": list(PROMPT_CAVEATS)}
