"""Память характера: история черт и побуждения (Шаг 56).

Отдельный модуль, а не ещё сотня строк в `store_pg.py`, по образцу
`store_essay.py`: `conn` приходит снаружи, транзакцией владеет вызывающий
(`eng.unit()`), здесь только SQL. Схема — `migrations/0015_character.sql`.
"""

from __future__ import annotations

from snapshot import iso

KINDS = ("want", "fear", "belief", "question")

# Насколько подтверждение подливает силы и где потолок. Потолок нужен по той
# же причине, что у `RECALL_CEILING`: без него побуждение, которое проход
# подтверждал бы каждую ночь, через месяц перекрыло бы всё остальное, и
# персонаж стал бы человеком одной мысли.
DRIVE_BUMP = 0.5
DRIVE_CEILING = 3.0


# =============================================================================
# История черт
# =============================================================================

def record_traits(conn, found: list[dict], now) -> None:
    """Сверить историю с новым списком черт: новые открыть, ушедшие закрыть.

    Оставшиеся не трогаются вовсе — ни основание, ни метка. Основание пишется
    в момент появления черты и дальше не переписывается (см. миграцию).
    """
    names = [f["name"] for f in found]
    conn.execute(
        """UPDATE trait_history SET dropped_at = %s
            WHERE dropped_at IS NULL AND NOT (name = ANY(%s))""",
        (now, names),
    )
    for f in found:
        conn.execute(
            """INSERT INTO trait_history (name, reason, set_at)
               SELECT %(name)s, %(reason)s, %(now)s
                WHERE NOT EXISTS (SELECT 1 FROM trait_history
                                   WHERE name = %(name)s AND dropped_at IS NULL)""",
            {"name": f["name"], "reason": f["reason"], "now": now},
        )


def trait_reasons(conn) -> dict[str, str]:
    """Открытые черты -> основание. Черты, заведённые до Шага 56, основания
    не имеют и в словарь не попадают: выдумывать его задним числом некому."""
    rows = conn.execute(
        "SELECT name, reason FROM trait_history WHERE dropped_at IS NULL ORDER BY id"
    ).fetchall()
    return {r["name"]: r["reason"] for r in rows}


def trait_history(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT name, reason, set_at, dropped_at FROM trait_history ORDER BY id"
    ).fetchall()
    return [{"name": r["name"], "reason": r["reason"], "set_at": iso(r["set_at"]),
             "dropped_at": iso(r["dropped_at"])} for r in rows]


# =============================================================================
# Побуждения
# =============================================================================

def open_drives(conn, now, limit: int | None = None) -> list[dict]:
    """Открытые побуждения, сильные сверху. Сила — с затуханием, в SQL."""
    rows = conn.execute(
        """SELECT d.id, d.kind, d.text, d.basis, d.strength, d.opened_at,
                  d.touched_at, drive_score(d.strength, d.touched_at, %s) AS score,
                  coalesce(array_agg(s.memory_id ORDER BY s.memory_id)
                           FILTER (WHERE s.memory_id IS NOT NULL), '{}') AS sources
             FROM drives d
             LEFT JOIN drive_sources s ON s.drive_id = d.id
            WHERE d.closed_at IS NULL
            GROUP BY d.id
            ORDER BY score DESC, d.id
            LIMIT %s""",
        (now, limit),
    ).fetchall()
    return [
        {"id": r["id"], "kind": r["kind"], "text": r["text"], "basis": r["basis"],
         "strength": r["strength"], "score": round(float(r["score"]), 3),
         "opened_at": iso(r["opened_at"]), "touched_at": iso(r["touched_at"]),
         "sources": list(r["sources"])}
        for r in rows
    ]


def add_drive(conn, kind: str, text: str, basis: str, memory_ids, now) -> int:
    if kind not in KINDS:
        raise ValueError(f"род побуждения: {kind!r}")
    if not memory_ids:
        # Держится и здесь, а не только в разборе: писатель без основания —
        # ровно то, что миграция запрещает словами, а схема не может.
        raise ValueError("побуждение без основания")
    row = conn.execute(
        """INSERT INTO drives (kind, text, basis, opened_at, touched_at)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (kind, text.strip(), basis.strip(), now, now),
    ).fetchone()
    for mid in sorted(set(memory_ids)):
        conn.execute(
            "INSERT INTO drive_sources (drive_id, memory_id) VALUES (%s, %s)",
            (row["id"], mid),
        )
    return row["id"]


def strengthen_drives(conn, ids, now) -> int:
    if not ids:
        return 0
    rows = conn.execute(
        """UPDATE drives
              SET strength = least(%s, strength + %s), touched_at = %s
            WHERE id = ANY(%s) AND closed_at IS NULL
        RETURNING id""",
        (DRIVE_CEILING, DRIVE_BUMP, now, list(ids)),
    ).fetchall()
    return len(rows)


def close_drives(conn, items, now) -> int:
    """`items` — пары (id, почему). Уже закрытые не трогаются."""
    n = 0
    for drive_id, why in items:
        row = conn.execute(
            """UPDATE drives SET closed_at = %s, closed_why = %s
                WHERE id = %s AND closed_at IS NULL RETURNING id""",
            (now, why, drive_id),
        ).fetchone()
        n += 1 if row else 0
    return n


def all_drives(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT d.id, d.kind, d.text, d.basis, d.strength, d.opened_at,
                  d.touched_at, d.closed_at, d.closed_why,
                  coalesce(array_agg(s.memory_id ORDER BY s.memory_id)
                           FILTER (WHERE s.memory_id IS NOT NULL), '{}') AS sources
             FROM drives d LEFT JOIN drive_sources s ON s.drive_id = d.id
            GROUP BY d.id ORDER BY d.id"""
    ).fetchall()
    return [
        {"id": r["id"], "kind": r["kind"], "text": r["text"], "basis": r["basis"],
         "strength": r["strength"], "opened_at": iso(r["opened_at"]),
         "touched_at": iso(r["touched_at"]), "closed_at": iso(r["closed_at"]),
         "closed_why": r["closed_why"], "sources": list(r["sources"])}
        for r in rows
    ]


def drives_at(conn):
    row = conn.execute("SELECT drives_at FROM agent WHERE id = 1").fetchone()
    return row["drives_at"] if row else None


def set_drives_at(conn, now) -> None:
    conn.execute("UPDATE agent SET drives_at = %s WHERE id = 1", (now,))
