"""Журнал выбранных дел (Шаг 57). Схема — `migrations/0016_agenda.sql`.

Как `store_character`: `conn` снаружи, транзакцией владеет вызывающий.
"""

from __future__ import annotations

from snapshot import iso


def add_pursuit(conn, at, action: str, why: str, about: str | None,
                drive_id: int | None) -> int:
    row = conn.execute(
        """INSERT INTO pursuits (at, action, why, about, drive_id)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (at, action, why.strip(), (about or "").strip() or None, drive_id),
    ).fetchone()
    return row["id"]


def set_outcome(conn, pursuit_id: int, outcome: str | None) -> None:
    conn.execute("UPDATE pursuits SET outcome = %s WHERE id = %s",
                 ((outcome or "").strip() or None, pursuit_id))


def _rows(rows) -> list[dict]:
    return [{"id": r["id"], "at": iso(r["at"]), "action": r["action"],
             "why": r["why"], "about": r["about"], "drive_id": r["drive_id"],
             "outcome": r["outcome"]} for r in rows]


def pursuits_between(conn, since, until) -> list[dict]:
    return _rows(conn.execute(
        """SELECT id, at, action, why, about, drive_id, outcome FROM pursuits
            WHERE at >= %s AND at < %s ORDER BY at, id""",
        (since, until),
    ).fetchall())


def recent_pursuits(conn, before, limit: int) -> list[dict]:
    """Последние дела ДО метки, в порядке жизни (свежие в конце)."""
    rows = conn.execute(
        """SELECT id, at, action, why, about, drive_id, outcome FROM pursuits
            WHERE at < %s ORDER BY at DESC, id DESC LIMIT %s""",
        (before, limit),
    ).fetchall()
    return list(reversed(_rows(rows)))


def all_pursuits(conn) -> list[dict]:
    return _rows(conn.execute(
        "SELECT id, at, action, why, about, drive_id, outcome FROM pursuits ORDER BY id"
    ).fetchall())


def agenda_at(conn):
    row = conn.execute("SELECT agenda_at FROM agent WHERE id = 1").fetchone()
    return row["agenda_at"] if row else None


def set_agenda_at(conn, now) -> None:
    conn.execute("UPDATE agent SET agenda_at = %s WHERE id = 1", (now,))
