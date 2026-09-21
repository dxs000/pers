"""Фасад хранилища: `main` не знает, что под ним."""

from contextlib import contextmanager
from datetime import datetime

import config
from snapshot import Turn


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

        # Досева черт здесь больше НЕТ (Шаг 42). Он ставил новорождённому
        # характер до того, как с ним что-либо произошло, — то же по природе,
        # что имя 'Некто' до Шага 36.3, только заметить труднее: три
        # правдоподобных слова не выглядят заглушкой. Черты теперь нажива-
        # ются `cycle.reconsider_traits`, а до первого пересчёта их нет, и
        # читатели промптов пустой список пропускают.
        with self.conn.transaction():
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

    def snapshot(self, now: datetime, about=None) -> Turn:
        # `about` — вектор реплики (Шаг 58); без него снимок прежний.
        if about is None:
            return self._pg.build_snapshot(self.conn, now)
        import embed
        return self._pg.build_snapshot(self.conn, now,
                                       about=(about, embed.model_tag("doc")))

    # --- Векторы (Шаг 58) ----------------------------------------------------
    def memories_to_embed(self, model: str, limit: int) -> list[dict]:
        import store_embed
        return store_embed.memories_to_embed(self.conn, model, limit)

    def set_memory_embedding(self, memory_id: int, vec, model: str) -> None:
        import store_embed
        store_embed.set_memory_embedding(self.conn, memory_id, vec, model)

    def similar_memories(self, vec, model: str, limit: int, floor: float = -1.0,
                         exclude=(), skip_sources=()) -> list[dict]:
        import store_embed
        return store_embed.similar_memories(self.conn, vec, model, limit, floor,
                                            exclude, skip_sources)

    def embedded_count(self, model: str) -> tuple[int, int]:
        import store_embed
        return store_embed.embedded_count(self.conn, model)

    # --- Биография (Шаг 38) --------------------------------------------------
    def all_memories(self) -> list[dict]:
        return self._pg.all_memories(self.conn)

    def add_memory(self, happened_at, precision: str, text: str, source: str,
                   weight: float = 1.0, now: datetime | None = None) -> int:
        return self._pg.add_memory(self.conn, happened_at, precision, text,
                                   source, weight, now)

    # --- Сон и вспоминание (Шаг 40) -----------------------------------------
    def touch_recall(self, memories, now: datetime, bump: float | None = None) -> None:
        # `bump` — Шаг 57.1: вернуться к воспоминанию нарочно весит больше,
        # чем вспомнить его к слову. По умолчанию — прибавка хранилища.
        if bump is None:
            self._pg.touch_recall(self.conn, memories, now)
        else:
            self._pg.touch_recall(self.conn, memories, now, bump)

    # --- Нити (Шаг 47) -------------------------------------------------------
    def open_threads(self, side: str = "self", limit: int | None = None) -> list[dict]:
        return self._pg.open_threads(self.conn, side, limit)

    def open_thread(self, side: str, text: str, now: datetime) -> int:
        return self._pg.open_thread(self.conn, side, text, now)

    def touch_threads(self, ids, now: datetime) -> int:
        return self._pg.touch_threads(self.conn, ids, now)

    def close_threads(self, ids, now: datetime, why: str) -> int:
        return self._pg.close_threads(self.conn, ids, now, why)

    def forget_threads(self, now: datetime, days: float, why: str) -> list[dict]:
        return self._pg.forget_threads(self.conn, now, days, why)

    def all_threads(self) -> list[dict]:
        return self._pg.all_threads(self.conn)

    # --- Чтение (Шаг 48) -----------------------------------------------------
    # Каталог приезжает ПАРАМЕТРОМ, а не читается здесь. Фасад не знает про
    # файловую систему по тому же правилу, по которому он не знает про сеть:
    # погоду приносит `outside`, полку — `library`, а хранилище хранит.
    def sync_books(self, shelf: list[dict]) -> dict:
        return self._pg.sync_books(self.conn, shelf)

    def current_book(self):
        return self._pg.current_book(self.conn)

    def shelf_state(self) -> dict:
        return self._pg.shelf_state(self.conn)

    def pick_book(self, text_path: str, why: str, now: datetime):
        return self._pg.pick_book(self.conn, text_path, why, now)

    def advance_reading(self, book_id: int, from_pos: int, to_pos: int,
                        conspectus: str | None, now: datetime):
        return self._pg.advance_reading(self.conn, book_id, from_pos, to_pos,
                                        conspectus, now)

    def conspectus_so_far(self, book_id: int, limit: int) -> list[str]:
        return self._pg.conspectus_so_far(self.conn, book_id, limit)

    def add_note(self, book_id: int, reading_id, text: str, now: datetime,
                 at_pos: int | None = None) -> int:
        return self._pg.add_note(self.conn, book_id, reading_id, text, now,
                                 at_pos)

    def untold_notes(self, limit: int | None = None) -> list[dict]:
        return self._pg.untold_notes(self.conn, limit)

    def mark_notes_told(self, ids, now: datetime) -> int:
        return self._pg.mark_notes_told(self.conn, ids, now)

    def notes_between(self, since, until) -> list[dict]:
        return self._pg.notes_between(self.conn, since, until)

    def deeds_between(self, since, until) -> dict:
        return self._pg.deeds_between(self.conn, since, until)

    def close_book(self, book_id: int, now: datetime, why: str):
        return self._pg.close_book(self.conn, book_id, now, why)

    def all_books(self) -> list[dict]:
        return self._pg.all_books(self.conn)

    # Метка захода чтения (Шаг 49). Лежит в `agent`, а не в `books`, потому
    # что отвечает на вопрос, который к книге не привязан: «когда он вообще
    # садился читать». У захода, кончившегося отказом взять книгу, книги нет,
    # а заход был — см. `0012_reading_pass.sql`.
    #
    # Сырым SQL, как `day_at` рядом: обе метки — одно поле одной строки, и
    # заводить ради них функции в `store_pg` значило бы писать обёртку над
    # обёрткой.
    def read_at(self):
        row = self.conn.execute(
            "SELECT read_at FROM agent WHERE id = 1").fetchone()
        return row["read_at"] if row else None

    def set_read_at(self, now: datetime) -> None:
        self.conn.execute("UPDATE agent SET read_at = %s WHERE id = 1", (now,))

    # --- День (Шаг 46) -------------------------------------------------------
    def last_lived(self):
        return self._pg.last_lived(self.conn)

    def day_at(self):
        row = self.conn.execute(
            "SELECT day_at FROM agent WHERE id = 1").fetchone()
        return row["day_at"] if row else None

    def set_day_at(self, now: datetime) -> None:
        self.conn.execute("UPDATE agent SET day_at = %s WHERE id = 1", (now,))

    def last_dream_at(self):
        return self._pg.last_dream_at(self.conn)

    # --- Черты (Шаг 42) ------------------------------------------------------
    def set_traits(self, traits, now: datetime) -> None:
        self._pg.set_traits(self.conn, traits, now)

    def traits_at(self):
        return self._pg.traits_at(self.conn)

    def memories_since(self, at) -> int:
        return self._pg.memories_since(self.conn, at)

    # --- Характер (Шаг 56) ---------------------------------------------------
    # Модулем рядом, а не функциями `store_pg`: см. `store_character`.
    def record_traits(self, found, now: datetime) -> None:
        import store_character
        store_character.record_traits(self.conn, found, now)

    def trait_reasons(self) -> dict:
        import store_character
        return store_character.trait_reasons(self.conn)

    def trait_history(self) -> list[dict]:
        import store_character
        return store_character.trait_history(self.conn)

    def open_drives(self, now: datetime, limit: int | None = None) -> list[dict]:
        import store_character
        return store_character.open_drives(self.conn, now, limit)

    def all_drives(self) -> list[dict]:
        import store_character
        return store_character.all_drives(self.conn)

    def add_drive(self, kind: str, text: str, basis: str, memory_ids,
                  now: datetime) -> int:
        import store_character
        return store_character.add_drive(self.conn, kind, text, basis,
                                         memory_ids, now)

    def strengthen_drives(self, ids, now: datetime) -> int:
        import store_character
        return store_character.strengthen_drives(self.conn, ids, now)

    def close_drives(self, items, now: datetime) -> int:
        import store_character
        return store_character.close_drives(self.conn, items, now)

    def drives_at(self):
        import store_character
        return store_character.drives_at(self.conn)

    def set_drives_at(self, now: datetime) -> None:
        import store_character
        store_character.set_drives_at(self.conn, now)

    # --- Воля (Шаг 57) -------------------------------------------------------
    def add_pursuit(self, at, action: str, why: str, about=None,
                    drive_id=None) -> int:
        import store_agenda
        return store_agenda.add_pursuit(self.conn, at, action, why, about, drive_id)

    def set_pursuit_outcome(self, pursuit_id: int, outcome) -> None:
        import store_agenda
        store_agenda.set_outcome(self.conn, pursuit_id, outcome)

    def pursuits_between(self, since, until) -> list[dict]:
        import store_agenda
        return store_agenda.pursuits_between(self.conn, since, until)

    def recent_pursuits(self, before, limit: int) -> list[dict]:
        import store_agenda
        return store_agenda.recent_pursuits(self.conn, before, limit)

    def all_pursuits(self) -> list[dict]:
        import store_agenda
        return store_agenda.all_pursuits(self.conn)

    def agenda_at(self):
        import store_agenda
        return store_agenda.agenda_at(self.conn)

    def set_agenda_at(self, now: datetime) -> None:
        import store_agenda
        store_agenda.set_agenda_at(self.conn, now)

    # --- Собеседник (Шаг 59) и голос (Шаг 60) --------------------------------
    # Модулем рядом, как `store_character`: вся работа с ним — в `store_him`.
    def him_facts(self, limit: int | None = None) -> list[dict]:
        import store_him
        return store_him.open_facts(self.conn, limit)

    def all_him_facts(self) -> list[dict]:
        import store_him
        return store_him.all_facts(self.conn)

    def add_him_fact(self, text: str, now: datetime) -> int:
        import store_him
        return store_him.add_fact(self.conn, text, now)

    def touch_him_facts(self, ids, now: datetime) -> int:
        import store_him
        return store_him.touch_facts(self.conn, ids, now)

    def drop_him_facts(self, items, now: datetime) -> int:
        import store_him
        return store_him.drop_facts(self.conn, items, now)

    def him_view(self) -> dict:
        import store_him
        return store_him.view(self.conn)

    def set_him_view(self, text, now: datetime) -> None:
        import store_him
        store_him.set_view(self.conn, text, now)

    def acquaintance(self) -> dict:
        import store_him
        return store_him.acquaintance(self.conn)

    def reply_stats(self, now: datetime, window_hours: float, sample: int) -> dict:
        import store_him
        return store_him.reply_stats(self.conn, now, window_hours, sample)

    def talk(self) -> dict:
        import store_him
        return store_him.talk(self.conn)

    def set_talk(self, value, why, now: datetime) -> None:
        import store_him
        store_him.set_talk(self.conn, value, why, now)

    def first_breath(self):
        import store_him
        return store_him.first_breath(self.conn)

    def record_birth(self, name: str, born_at, birthplace: str | None,
                     reason: str) -> bool:
        return self._pg.record_birth(self.conn, name, born_at, birthplace, reason)

    def outside_latch(self) -> dict:
        return self._pg.outside_latch(self.conn)

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

    def set_mood(self, mood: str, reason: str | None, now: datetime) -> None:
        """Сменить настроение. Метка двигается ВСЕГДА — зовут только на смене.

        Решение «менять или нет» принимает проход (`mind.reflect_mood`), а не
        хранилище: ответ «прежнее» сюда просто не доходит. Сравнивать слова
        здесь было бы вторым местом, где принимается то же решение, — и
        разошлись бы они на первой же смене регистра.
        """
        self.conn.execute(
            "UPDATE agent SET mood = %s, mood_reason = %s, mood_since = %s "
            "WHERE id = 1",
            (mood, reason, now),
        )

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
                    expires_at=None) -> None:
        self._pg.record_urge(self.conn, kind, subject, amount, now,
                             expires_at=expires_at)

    # --- Годовщины (Шаг 45) --------------------------------------------------
    def born_at(self):
        return self._pg.born_at(self.conn)

    def memories_on(self, month: int, day: int) -> list[dict]:
        return self._pg.memories_on(self.conn, month, day)

    def note_anniversary(self, subject: str, amount: float, now: datetime,
                         expires_at, within_hours: float) -> bool:
        return self._pg.note_anniversary(self.conn, subject, amount, now,
                                         expires_at, within_hours)

    def strongest_impulse(self, now: datetime, floor: float):
        return self._pg.strongest_impulse(self.conn, now, floor)

    def mark_spoken(self, impulse_id: int, now: datetime) -> None:
        self._pg.mark_spoken(self.conn, impulse_id, now)

    def damp_impulses(self, factor: float, kinds=None) -> None:
        # `kinds` — Шаг 60: гасится своё, а не всё подряд. `None` — всё, как
        # было до шага; так зовут только те, кому это и нужно.
        self._pg.damp_impulses(self.conn, factor, kinds)

    def open_impulses(self) -> list[dict]:
        return self._pg.open_impulses(self.conn)

    # --- Обещания (Шаг 43) ---------------------------------------------------
    def add_promise(self, due_at, text: str, message_id=None,
                    now: datetime | None = None) -> int:
        return self._pg.add_promise(self.conn, due_at, text, message_id, now)

    def close_acknowledged_promises(self, spoke_at) -> int:
        return self._pg.close_acknowledged_promises(self.conn, spoke_at)

    def due_promise(self, now: datetime, repeat_after_hours: float):
        return self._pg.due_promise(self.conn, now, repeat_after_hours)

    def mark_promise_said(self, promise_id: int, now: datetime, *,
                          close: bool) -> None:
        self._pg.mark_promise_said(self.conn, promise_id, now, close=close)

    def open_promises(self) -> list[dict]:
        return self._pg.open_promises(self.conn)

    def last_exchange_ts(self):
        """Когда собеседник в последний раз что-то написал.

        Отдельно от `last_exchange()`, который отдаёт текст: обещаниям нужен
        только момент, и тянуть ради него пару реплик из `messages` незачем.
        """
        row = self.conn.execute(
            "SELECT last_exchange_ts FROM agent WHERE id = 1"
        ).fetchone()
        return row["last_exchange_ts"] if row else None

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