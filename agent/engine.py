"""Фасад хранилища: `main` не знает, что под ним.

Появился последним шагом 3b, когда движков стало два и принцип роадмапа
(«`store` — фасад; логика не знает, что под ней») перестал держаться сам
собой. С Шага 26 движок снова один — Postgres, — и фасад остаётся не по
инерции:

- граница между «думать» и «хранить» проходит здесь, и её видно списком
  методов: `main` умеет ровно то, что перечислено ниже, и ничего сверх;
- методов ровно столько, сколько зовёт `main`. Метод про запас — это
  метод, чьё поведение никто не проверял;
- когда придёт третий движок (а он уже приходил дважды), менять придётся
  один файл, а не все вызовы.

## Валюта времени

Фасад принимает **только aware `datetime`**. Это осталось от эпохи двух
движков — JSON брал `now` строкой ISO, — но правило пережило причину и
стало прямым исполнением «нечистое на краю»: строка есть форма хранения,
и знать о ней должен тот, кто хранит.

## Единица записи (Шаг 23)

Метода `commit()` у фасада больше нет — вместо него `unit()`, менеджер
контекста. Разница не косметическая: `commit()` называл **момент**, и
границу приходилось выводить из того, где он стоит; `unit()` называет
**отрезок**, и границу видно глазами, в том числе на беглом чтении.

На `PgEngine` это `with conn.transaction():` поверх соединения в
`autocommit`. Единицы хода названы в `main`: T1 (обмен), T2 (осмысление),
T3 (закрытие сессии) и короткая единица латча среды.

## Рабочая память (Шаг 24)

К снимку добавился второй читающий контракт — `working_memory()`. Разница
между ними существенная и стоит того, чтобы быть названной: **снимок это
память о разговорах, рабочая память — сам разговор**. Первый уезжает в
`mind` и рендерится в системный промпт; вторая уезжает в модель как есть,
сообщениями, и `mind` её не видит вовсе.

До Шага 24 она жила переменной цикла в `main` и хранилища не касалась —
то есть переживала ход, но не переживала процесс. Теперь её отдаёт
хранилище, и персонаж переживает рестарт посреди разговора.

## Как проверен

Восемнадцатью сценариями сбруи: `golden.py` ходит в хранилище ТОЛЬКО
через фасад — кроме двух дампов сценария `inbox`, которые СОЗНАТЕЛЬНО
читают таблицы напрямую, и сценария `inspector`, у которого своё
соединение и своя дверь.

Про эту дверь стоит сказать отдельно, потому что она выглядит нарушением
принципа и им не является. С Шага 30 у хранилища два выхода: фасад — для
того, кто ПИШЕТ, и `inspector` — для того, кто только смотрит. Через
фасад инспектору не пройти буквально: `_ensure_agent` доливает дефолты,
то есть открытие фасада есть запись, а read-only соединение её не
пропустит. Список методов ниже остаётся тем, что `agent` умеет делать с
памятью, — и ровно поэтому он не должен разрастаться читателями страниц. До Шага 26 она сверяла два движка друг с другом, теперь —
выдачу с эталоном на диске; путь от базы до промпта в обоих случаях один
и тот же, и покраснеет он одинаково.
"""

from contextlib import contextmanager
from datetime import datetime

import config
from snapshot import DEFAULT_TRAITS, Turn



class PgEngine:
    """Postgres — единственный движок с Шага 26.

    Соединение держится всю сессию: цикл диалога один и длинный, а пул
    под одного писателя — механика без задачи. У демона с фоновыми
    задачами это перестанет быть верным, и пул придётся заводить вместе
    с ответом на вопрос, чья транзакция чья (долг 3c)."""

    name = "pg"

    def __init__(self, dsn=None, *, test: bool = False):
        import store_pg

        self._pg = store_pg
        self.conn = store_pg.connect(dsn, test=test)
        self._ensure_agent()

    def _ensure_agent(self) -> None:
        """Долить дефолты объекта №0. ЕДИНСТВЕННОЕ место досева в проекте.

        Раньше их было два — здесь и `store.ensure_self`, — и докстринг
        предписывал им совпадать. Предписание не проверялось ничем и не
        выполнялось: тут доливалось только место, а имя и черты
        оставлялись на умолчания схемы. Умолчание `traits` — пустой
        массив, и на чистом старте персонаж выходил **без единой черты**:
        строка промпта читалась «Твои черты характера: .» — с висящей
        точкой и без характера, тогда как JSON-движок засевал
        «любопытный».

        Расхождение прожило до первого живого запуска, потому что
        сценарий `empty` обходил движки стороной. Чистый старт — путь,
        которым проект проходит ровно один раз, и именно поэтому он под
        сценарием: второго шанса заметить не будет. С Шага 26 обязанность
        «совпадать» снята вместе со вторым концом.
        """
        row = self.conn.execute(
            "SELECT name, traits, place_label FROM agent WHERE id = 1"
        ).fetchone() or {}

        # Досев — одна единица: персонаж без черт, но уже с объектом №0 —
        # то самое полусостояние, из-за которого промпт выходил с висящей
        # точкой вместо характера. Под autocommit без границы оно стало бы
        # достижимым не только на падении, но и на Ctrl+C.
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

    # --- жизненный цикл ---------------------------------------------------
    @contextmanager
    def unit(self):
        """Единица записи — транзакция. Соединение живёт в `autocommit`.

        Внутрь не должен попадать сетевой вызов: транзакция держит строки
        заблокированными, а ответ LLM идёт секунды. Это то же правило, из-за
        которого на Шаге 22 отклонили `claimed_at`, — и оно же переставило
        тело цикла в `main`: сначала все три прохода, потом запись.
        """
        with self.conn.transaction():
            yield self

    def close(self) -> None:
        # Коммита тут больше нет: под autocommit фиксировать нечего — всё,
        # что записано, записано своей единицей.
        self.conn.close()

    # --- чтение -----------------------------------------------------------
    def snapshot(self, now: datetime) -> Turn:
        return self._pg.build_snapshot(self.conn, now)

    def place(self) -> dict:
        # Запрос уехал в `store_pg` на Шаге 32: у места стало два читателя,
        # и второй (инспектор) до фасада не дотягивается по устройству.
        return self._pg.place(self.conn)

    def last_exchange(self):
        return self._pg.last_exchange(self.conn)

    def session_stale(self, now: datetime) -> bool:
        return self._pg.session_stale(self.conn, now)

    def summary_buffer(self) -> dict:
        return self._pg.summary_buffer(self.conn)

    def working_memory(self) -> list[dict]:
        return self._pg.working_memory(self.conn)

    def pending(self) -> list[dict]:
        """Непомеченная пачка входящих. Третий читающий контракт фасада.

        Отличие от двух прежних названо, потому что все три легко спутать:
        снимок — память о разговорах, рабочая память — сам разговор,
        **очередь — то, что ещё не стало ни тем, ни другим**. Первый уезжает
        в `mind`, вторая в модель, третья не уезжает никуда: из неё делают
        реплику хода.
        """
        return self._pg.pending(self.conn)

    # --- запись -----------------------------------------------------------
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
        """Обмен двумя строками. Возвращает id строки `assistant` (Шаг 28)."""
        return self._pg.append_exchange(self.conn, user_text, answer, now, arrived_at)

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


def open_engine(**kwargs):
    """Поднять движок. Выбора больше нет, и это Шаг 26.

    До него выбор шёл по наличию `DATABASE_URL`: есть — Postgres, нет —
    файл на диске. Ветка была честной ровно до тех пор, пока JSON-движок
    оставался рабочим путём; с уходом второго писателя в план держать два
    пути записи стало не страховкой, а вторым местом, где надо помнить
    про транзакции.

    Фабрика при этом остаётся: третий движок в проекте уже был (JSON
    пришёл на смену наброску), и придёт ещё. Пусть точка подъёма будет
    одна и известная — тогда следующий переезд правит этот файл, а не
    все вызовы.

    Базы нет — `store_pg.connect` роняет вызов внятным `RuntimeError` из
    `config.require_dsn`. Молчаливого отката на файл больше не будет: он
    выглядел бы как работающая программа с пустой памятью.
    """
    return PgEngine(**kwargs)
