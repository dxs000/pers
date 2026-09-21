"""Векторы воспоминаний (Шаг 58). Схема — `migrations/0017_embeddings.sql`."""

from __future__ import annotations


def memories_to_embed(conn, model: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, text FROM memories
            WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM %s
            ORDER BY id LIMIT %s""",
        (model, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def set_memory_embedding(conn, memory_id: int, vec, model: str) -> None:
    conn.execute(
        "UPDATE memories SET embedding = %s, embedding_model = %s WHERE id = %s",
        (list(vec), model, memory_id),
    )


def similar_memories(conn, vec, model: str, limit: int,
                     floor: float = -1.0, exclude=(), skip_sources=()) -> list[dict]:
    """Ближайшие по смыслу, с похожестью. Только посчитанные этой моделью.
    `skip_sources` — какие источники не рассматривать (например, сны)."""
    rows = conn.execute(
        """SELECT id, happened_at, precision, text, source, weight,
                  embed_dot(embedding, %s::real[]) AS sim
             FROM memories
            WHERE embedding_model = %s AND embedding IS NOT NULL
              AND NOT (id = ANY(%s))
              AND NOT (source = ANY(%s))
              AND embed_dot(embedding, %s::real[]) >= %s
            ORDER BY sim DESC, id
            LIMIT %s""",
        (list(vec), model, list(exclude), list(skip_sources), list(vec), floor, limit),
    ).fetchall()
    # `happened_at` сырой, как у всех строк базы: в строку его переводит
    # читатель (`build_snapshot` — через `iso`), и двойной перевод сломал бы его.
    return [{"id": r["id"], "happened_at": r["happened_at"],
             "precision": r["precision"], "text": r["text"], "source": r["source"],
             "weight": r["weight"], "sim": round(float(r["sim"]), 3)} for r in rows]


def embedded_count(conn, model: str) -> tuple[int, int]:
    row = conn.execute(
        """SELECT count(*) FILTER (WHERE embedding_model = %s AND embedding IS NOT NULL) AS done,
                  count(*) AS total FROM memories""",
        (model,),
    ).fetchone()
    return row["done"], row["total"]
