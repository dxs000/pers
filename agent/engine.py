"""Фасад хранилища: `main` не знает, что под ним."""

from contextlib import contextmanager
from datetime import datetime

import config
from snapshot import DEFAULT_TRAITS, Turn


class PgEngine:
    name = "pg"

    def __init__(self, dsn=None, *, test: bool = False):
        import store_pg

        self._pg = store_pg
        self.conn = store_pg.connect(dsn, test=test)
        self._ensure_agent()

    def _ensure_agent(self) -> None:
        row = self.conn.execute(
            "SELECT name, traits, place_label FROM agent WHERE id = 1"
        ).fetchone() or {}

        with self.conn.transaction():
            if not row.get("traits"):
                self.conn.execute(
                    "UPDATE agent SET traits = %s WHERE id = 1", (list(DEFAULT_TRAITS),)
                )
            if not row.get("place_label") and config.APP_PLACE:
                self.conn.execute(
                    "UPDATE agent SET place_label = %s, place_lat = %s, place_lon = %s "
                    "WHERE id = 1",
                    (config.APP_PLACE, config.APP_LAT, config.APP_LON),
                )
            self.conn.execute(
                "INSERT INTO objects (id, type, label, label_norm, salience) "
                "VALUES (0, 'self', 'self', 'self', 0) ON CONFLICT (id) DO NOTHING"
            )

    @contextmanager
    def unit(self):
        with self.conn.transaction():
            yield self

    def close(self) -> None:
        self.conn.close()

    def snapshot(self, now: datetime) -> Turn:
        return self._pg.build_snapshot(self.conn, now)

    def place(self) -> dict:
        return self._pg.place(self.conn)

    def last_exchange(self):
        return self._pg.last_exchange(self.conn)

    def last_search_ts(self):
        row = self.conn.execute(
            "SELECT last_search_ts FROM agent WHERE id = 1"
        ).fetchone()
        return row["last_search_ts"] if row else None

    def mark_search(self, now: datetime) -> None:
        self.conn.execute(
            "UPDATE agent SET last_search_ts = %s WHERE id = 1", (now,)
        )

    def session_stale(self, now: datetime) -> bool:
        return self._pg.session_stale(self.conn, now)

    def summary_buffer(self) -> dict:
        return self._pg.summary_buffer(self.conn)

    def working_memory(self) -> list[dict]:
        return self._pg.working_memory(self.conn)

    def pending(self) -> list[dict]:
        return self._pg.pending(self.conn)

    def save_place(self, place: dict) -> None:
        self.conn.execute(
            """
            UPDATE agent SET place_label = %s, place_lat = %s, place_lon = %s,
                   place_source = %s, place_asked = %s, place_resolved_at = %s
             WHERE id = 1
            """,
            (place.get("label"), place.get("lat"), place.get("lon"),
             place.get("source"), place.get("asked"), place.get("resolved_at")),
        )

    def set_mood(self, mood: str) -> None:
        self.conn.execute("UPDATE agent SET mood = %s WHERE id = 1", (mood,))

    def touch_exchange(self, now: datetime) -> None:
        self._pg.touch_exchange(self.conn, now)

    def append_exchange(self, user_text: str, answer: str, now: datetime,
                        arrived_at: datetime | None = None) -> int:
        return self._pg.append_exchange(self.conn, user_text, answer, now, arrived_at)

    # --- Инициатива (Шаг 35) ------------------------------------------------
    def append_utterance(self, text: str, now: datetime) -> int:
        return self._pg.append_utterance(self.conn, text, now)

    def last_utterance(self):
        return self._pg.last_utterance(self.conn)

    def utterances_since(self, since: datetime) -> int:
        return self._pg.utterances_since(self.conn, since)

    def record_urge(self, kind: str, subject, amount: float, now: datetime,
                    mode: str = "bump", expires_at=None) -> None:
        self._pg.record_urge(self.conn, kind, subject, amount, now,
                             mode=mode, expires_at=expires_at)

    def strongest_impulse(self, now: datetime, floor: float):
        return self._pg.strongest_impulse(self.conn, now, floor)

    def mark_spoken(self, impulse_id: int, now: datetime, damp: float) -> None:
        self._pg.mark_spoken(self.conn, impulse_id, now, damp)

    def open_impulses(self) -> list[dict]:
        return self._pg.open_impulses(self.conn)

    def push(self, text: str, now: datetime) -> int:
        return self._pg.push_inbox(self.conn, text, now)

    def mark_handled(self, ids, now: datetime, reply_id: int | None = None) -> list[int]:
        return self._pg.mark_handled(self.conn, ids, now, reply_id)

    def bump_dropped(self, n: int) -> bool:
        return self._pg.bump_dropped(self.conn, n)

    def upsert_object(self, candidate: dict, now: datetime) -> None:
        self._pg.upsert_object(self.conn, candidate, now)

    def merge_self_assertions(self, assertions, now: datetime) -> None:
        self._pg.merge_self_assertions(self.conn, assertions, now)

    def remember_outside(self, sky, wx, now: datetime, family) -> None:
        if sky is None and wx is None:
            return
        latch = {"ts": now.isoformat()}
        if sky and sky.get("light"):
            latch["light"] = sky["light"]
        if family:
            latch["weather"] = family
        import json as _json

        self.conn.execute("UPDATE agent SET outside_latch = %s WHERE id = 1",
                          (_json.dumps(latch),))

    def close_session(self, now: datetime, summary: str | None):
        return self._pg.close_session(self.conn, now, summary)

    def enqueue_digest(self, reply_id, findings) -> None:
        self._pg.enqueue_digest(self.conn, reply_id, findings)

    def next_digest(self):
        return self._pg.next_digest(self.conn)

    def mark_digest_done(self, followup_id, now) -> None:
        self._pg.mark_digest_done(self.conn, followup_id, now)

    def exchange_by_reply(self, reply_id):
        return self._pg.exchange_by_reply(self.conn, reply_id)


def open_engine(**kwargs):
    return PgEngine(**kwargs)