"""Память эссе. conn приходит снаружи — писатель тот же, что у store_pg."""

from __future__ import annotations


def current_essay(conn):
    return conn.execute(
        """
        SELECT id, title, why, file_path, opened_at
          FROM essays
         WHERE closed_at IS NULL
        """
    ).fetchone()


def open_essay(conn, title: str, why: str, file_path: str, now):
    return conn.execute(
        """
        INSERT INTO essays (title, why, file_path, opened_at)
        SELECT %(title)s, %(why)s, %(path)s, %(now)s
         WHERE NOT EXISTS (
               SELECT 1 FROM essays WHERE closed_at IS NULL
         )
        RETURNING id, title, why, file_path, opened_at
        """,
        {"title": title.strip(), "why": why.strip(), "path": file_path,
         "now": now},
    ).fetchone()


def add_passage(conn, essay_id: int, text: str, now):
    body = (text or "").strip()
    if not body:
        return None
    conspectus = body if len(body) <= 400 else body[:400].rstrip() + "…"
    return conn.execute(
        """
        INSERT INTO essay_passages (essay_id, at, chars, conspectus)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (essay_id, now, len(body), conspectus),
    ).fetchone()


def close_essay(conn, essay_id: int, why: str, now):
    return conn.execute(
        """
        UPDATE essays SET closed_at = %s, closed_why = %s
         WHERE id = %s AND closed_at IS NULL
        RETURNING id
        """,
        (now, why, essay_id),
    ).fetchone()


def passages_so_far(conn, essay_id: int, limit: int):
    rows = conn.execute(
        """
        SELECT conspectus FROM essay_passages
         WHERE essay_id = %s AND conspectus IS NOT NULL
         ORDER BY at DESC, id DESC
         LIMIT %s
        """,
        (essay_id, limit),
    ).fetchall()
    return [r["conspectus"] for r in reversed(rows)]
