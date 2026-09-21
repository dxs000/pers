"""Память о собеседнике (Шаг 59) и счёт отклика (Шаг 60).

Модуль по образцу `store_character`: `conn` снаружи, транзакцией владеет
вызывающий (`eng.unit()`), здесь только SQL. Схема — `migrations/0018_him.sql`.

Что здесь ВЫЧИСЛЯЕТСЯ, а не хранится, и почему: знакомство (первая его
реплика), число разговоров, отклик на реплики по своей воле. Всё это уже
записано в `messages` и `episodes`, и второй факт об одном и том же
разъехался бы с первым — тот же довод, по которому тишина не хранится
(`cycle.silence_urge`).
"""

from __future__ import annotations

from datetime import timedelta

from snapshot import iso

# Сколько фактов о нём показывать. Больше — и блок о собеседнике перевесит
# блок о себе, а промпт персонажа всё-таки про персонажа.
FACTS_SNAPSHOT_LIMIT = 8


# =============================================================================
# Факты
# =============================================================================

def open_facts(conn, limit: int | None = None) -> list[dict]:
    rows = conn.execute(
        """SELECT id, text, noted_at, touched_at, hits FROM him_facts
            WHERE dropped_at IS NULL
            ORDER BY touched_at DESC, id DESC
            LIMIT %s""",
        (limit,),
    ).fetchall()
    return [{"id": r["id"], "text": r["text"], "noted_at": iso(r["noted_at"]),
             "touched_at": iso(r["touched_at"]), "hits": r["hits"]} for r in rows]


def all_facts(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT id, text, noted_at, touched_at, hits, dropped_at, dropped_why
             FROM him_facts ORDER BY id"""
    ).fetchall()
    return [{"id": r["id"], "text": r["text"], "noted_at": iso(r["noted_at"]),
             "touched_at": iso(r["touched_at"]), "hits": r["hits"],
             "dropped_at": iso(r["dropped_at"]), "dropped_why": r["dropped_why"]}
            for r in rows]


def add_fact(conn, text: str, now) -> int:
    return conn.execute(
        """INSERT INTO him_facts (text, noted_at, touched_at)
           VALUES (%s, %s, %s) RETURNING id""",
        (text, now, now),
    ).fetchone()["id"]


def touch_facts(conn, ids, now) -> int:
    ids = list(ids or [])
    if not ids:
        return 0
    return conn.execute(
        """UPDATE him_facts SET touched_at = %s, hits = hits + 1
            WHERE id = ANY(%s) AND dropped_at IS NULL""",
        (now, ids),
    ).rowcount


def drop_facts(conn, items, now) -> int:
    """`items` — пары (id, почему)."""
    n = 0
    for fid, why in items or []:
        n += conn.execute(
            """UPDATE him_facts SET dropped_at = %s, dropped_why = %s
                WHERE id = %s AND dropped_at IS NULL""",
            (now, why, fid),
        ).rowcount
    return n


# =============================================================================
# Взгляд
# =============================================================================

def view(conn) -> dict:
    row = conn.execute("SELECT him_view, him_at FROM agent WHERE id = 1").fetchone() or {}
    return {"view": row.get("him_view"), "at": row.get("him_at")}


def set_view(conn, text: str | None, now) -> None:
    """Метка двигается всегда, текст — только если дали новый: проход,
    решивший «взгляд прежний», тоже состоялся."""
    if text:
        conn.execute("UPDATE agent SET him_view = %s, him_at = %s WHERE id = 1",
                     (text, now))
    else:
        conn.execute("UPDATE agent SET him_at = %s WHERE id = 1", (now,))


# =============================================================================
# Знакомство — вычисляется
# =============================================================================

def acquaintance(conn) -> dict:
    """Когда он написал впервые и сколько было разговоров. Ни разу — `since`
    равно `None`, и это не «нет данных», а «вы не знакомы»."""
    first = conn.execute(
        "SELECT min(ts) AS ts FROM messages WHERE role = 'user'"
    ).fetchone()
    talks = conn.execute(
        "SELECT count(*)::int AS n FROM episodes WHERE exchanges > 0"
    ).fetchone()
    return {"since": first["ts"] if first else None,
            "talks": talks["n"] if talks else 0}


# =============================================================================
# Отклик на реплики по своей воле (Шаг 60) — вычисляется
# =============================================================================

def reply_stats(conn, now, window_hours: float, sample: int) -> dict:
    """Сколько из последних `sample` его реплик по своей воле получили ответ
    в пределах `window_hours`, и сколько подряд остались без ответа сейчас.

    В выборку идут только реплики старше окна: на сказанное десять минут
    назад ответа ещё можно ждать, и считать его неотвеченным — значит
    наказывать за то, что человек не сидит у экрана.

    `streak` — сколько он сказал сам после ПОСЛЕДНЕЙ реплики человека. Это
    другая величина, чем доля: доля — привычка отношений, серия — то, что
    происходит прямо сейчас. Человек, написавший трижды в пустоту, четвёртый
    раз не пишет, как бы ни было заведено между ними.
    """
    rows = conn.execute(
        """SELECT m.ts,
                  EXISTS (SELECT 1 FROM messages u
                           WHERE u.role = 'user' AND u.ts > m.ts
                             AND u.ts <= m.ts + make_interval(secs => %s)) AS answered
             FROM messages m
            WHERE m.spontaneous AND m.ts <= %s
            ORDER BY m.ts DESC
            LIMIT %s""",
        (window_hours * 3600.0, now - timedelta(hours=window_hours), sample),
    ).fetchall()
    last_user = conn.execute(
        "SELECT max(ts) AS ts FROM messages WHERE role = 'user'"
    ).fetchone()["ts"]
    if last_user is None:
        streak = conn.execute(
            "SELECT count(*)::int AS n FROM messages WHERE spontaneous AND ts <= %s",
            (now,),
        ).fetchone()["n"]
    else:
        streak = conn.execute(
            """SELECT count(*)::int AS n FROM messages
                WHERE spontaneous AND ts > %s AND ts <= %s""",
            (last_user, now),
        ).fetchone()["n"]
    return {"total": len(rows), "answered": sum(1 for r in rows if r["answered"]),
            "streak": streak}


def talk(conn) -> dict:
    row = conn.execute(
        "SELECT talk, talk_why, talk_at, traits_at FROM agent WHERE id = 1"
    ).fetchone() or {}
    return {"talk": row.get("talk"), "why": row.get("talk_why"),
            "at": row.get("talk_at"), "traits_at": row.get("traits_at")}


def set_talk(conn, value: float | None, why: str | None, now) -> None:
    if value is None:
        conn.execute("UPDATE agent SET talk_at = %s WHERE id = 1", (now,))
    else:
        conn.execute(
            "UPDATE agent SET talk = %s, talk_why = %s, talk_at = %s WHERE id = 1",
            (value, why, now),
        )


def first_breath(conn):
    """С какого момента он живёт здесь: запись рождения. Нужна тишине, когда
    разговора не было ни разу — иначе тишине не от чего отсчитываться, и
    персонаж, которому никто не написал, не заговорил бы никогда."""
    row = conn.execute(
        "SELECT min(created_at) AS ts FROM memories WHERE source = 'genesis'"
    ).fetchone()
    return row["ts"] if row else None
