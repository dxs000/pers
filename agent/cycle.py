"""Ход — функция, а не тело цикла (Шаг 27).

Шаг 24 объявил, что «у хода не осталось состояния между итерациями, и
`agent.py` сможет звать его функцией». Буквально это было неверно: в цикле
`main` продолжали жить три вещи, и каждая пережила бы расщепление молча.

- `previous` (`eng.last_exchange()`) читался ОДИН РАЗ до цикла. В REPL это
  почти незаметно; у демона процесс живёт неделями, и строка «Прошлый
  разговор был N назад» считалась бы от подъёма юнита до конца времён.
  Теперь читается перед T1, каждый ход. **Последствие названо и принято:**
  молчание ДЛИННЕЕ `SILENCE_MIN_HOURS` (1 ч), но короче `SESSION_GAP_HOURS`
  (3 ч) начинает рендериться внутри живой сессии. Человек отошёл на два
  часа — персонаж вправе это заметить; раньше он не мог заметить в принципе.
- Клиенты сети и модели жили модульными глобалами (`_http`, `_search_http`).
  Теперь это `Edges` — один объект, собираемый на входе в процесс и
  приезжающий параметром. Глобалов в модуле нет ни одного.

Заслонка ретривера больше не считает ходы: у демона «три хода» перестают
быть длительностью. Порог живёт в `agent.last_search_ts`.

**Печать сюда не заходит.** Это та же проверка, что «`mind` не получает
`state`»: ход физически не может обзавестись зависимостью от TTY, а не
обещает этого. Ответ отдаётся наружу колбэком `announce` — и отдаётся
внутри хода, сразу после T1, потому что порядок «записать раньше, чем
показать» есть правило ХОДА, а не оболочки.

`announce` же — это и есть будущий провод Фазы 4b: демон отдаст ответ не
`print`, а `NOTIFY`, и ход об этой разнице знать не будет.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import config
import outside
import sky as sky_mod
import web
from mind import (build_system_prompt, decide_query, extract_objects,
                  reflect_mood, reflect_self, weather_family)
from snapshot import SESSION_GAP_HOURS
from openai import OpenAI, OpenAIError

NET_TIMEOUT = 10.0
SEARCH_TIMEOUT = 8.0

# Сколько часов после состоявшегося поиска ретривер молчит.
# Смысл не в экономии вызовов — сам проход дёшев, — а в том, чтобы блок
# найденного не стоял в промпте снова, пока выдача ещё та же.
RETRIEVER_COOLDOWN_HOURS = 1.0


@dataclass(frozen=True)
class Edges:
    """Края хода: модель, сеть среды, сеть поиска и ключ к ней."""

    llm: Any
    http: Any = None
    search: Any = None
    search_key: str = ""

    def close(self) -> None:
        for client in (self.http, self.search):
            if client is not None:
                client.close()


@dataclass(frozen=True)
class Outcome:
    """Что ход отдаёт наружу. Ни одно поле не про печать.

    `superseded` (Шаг 28) — ход не состоялся, потому что пачку успел забрать
    другой потребитель. Отдельным полем, а не строкой в `error`: ошибки
    показывают человеку, а тут показывать нечего.
    """

    answer: str | None
    error: str | None = None
    superseded: bool = False


class _Superseded(Exception):
    """Внутренний сигнал: пачку забрали. Роняет T1, чтобы откатить обмен."""


def open_edges() -> Edges:
    """Поднять края процесса. Одна точка на процесс — и у REPL, и у демона."""
    return Edges(
        llm=OpenAI(
            api_key=config.require_api_key(),
            base_url=config.DEEPSEEK_BASE_URL,
            http_client=config.get_sync_client(),
            max_retries=config.MAX_RETRIES,
        ),
        http=config.get_sync_client(timeout=NET_TIMEOUT),
        search=web.build_search_client(SEARCH_TIMEOUT),
        search_key=config.TAVILY_API_KEY,
    )


def weather_snapshot(place: dict, edges: Edges, now_dt: datetime) -> dict | None:
    """Погода в месте персонажа или `None`. Кэш и режимы отказа — в `outside`."""
    pair = sky_mod.coords(place.get("lat"), place.get("lon"))
    if pair is None:
        return None

    try:
        return outside.weather(pair[0], pair[1], now_dt, edges.http)
    except Exception as err:
        logging.warning("weather: слепок не собрался: %s", err)
        return None


def prompt_and_latch(eng, edges: Edges, now_dt: datetime, previous=None,
                     findings: list[dict] | None = None, tz=None) -> str:
    """Системный промпт на момент `now_dt` — и запись латча среды."""
    tz = tz or config.TZ
    place = eng.place()
    snap = sky_mod.local_snapshot(place.get("lat"), place.get("lon"), now_dt, tz)
    wx = weather_snapshot(place, edges, now_dt)

    prompt = build_system_prompt(
        eng.snapshot(now_dt),
        now_dt.astimezone(tz),
        snap,
        wx,
        previous,
        findings,
    )
    with eng.unit():
        eng.remember_outside(snap, wx, now_dt, weather_family(wx))
    return prompt


def look_outward(eng, user_text: str, objects: list[dict], edges: Edges,
                 now: datetime) -> list[dict] | None:
    """Решить, нужен ли веб, и — если заслонка позволяет — сходить.

    Возвращает findings. `None` — в промпте блока не будет; пустой список —
    «искали, не нашли».

    Заслонка отсекает поиск, а не решение. `decide_query` зовётся всегда.
    """
    query = decide_query(user_text, objects, edges.llm)
    if not query:
        return None

    last = eng.last_search_ts()
    if last is not None:
        hours = (now - last).total_seconds() / 3600.0
        if 0.0 <= hours < RETRIEVER_COOLDOWN_HOURS:
            logging.info(
                "поиск подавлен заслонкой (%.1f ч из %.1f): «%s»",
                hours, RETRIEVER_COOLDOWN_HOURS, query,
            )
            return None

    results = web.search(query, edges.search, edges.search_key, now=now)
    if results is None:
        return None

    with eng.unit():
        eng.mark_search(now)
    for item in results:
        logging.info("нашлось: %s | %s", item.get("title"), item.get("url"))
    return results


@dataclass(frozen=True)
class Batch:
    """Пачка входящих, разрезанная по границе разговора."""

    live: list[dict]
    stale: list[dict]

    @property
    def text(self) -> str:
        return "\n".join((r["text"] or "").strip() for r in self.live if r["text"])

    @property
    def arrived_at(self) -> datetime | None:
        return self.live[-1]["ts"] if self.live else None

    @property
    def ids(self) -> list[int]:
        return [r["id"] for r in self.live]


def split_batch(rows: list[dict], gap_hours: float = SESSION_GAP_HOURS) -> Batch:
    """Разрезать пачку там, где между репликами прошёл разрыв сессии."""
    if not rows:
        return Batch(live=[], stale=[])

    cut = 0
    for i in range(len(rows) - 1, 0, -1):
        span = (rows[i]["ts"] - rows[i - 1]["ts"]).total_seconds() / 3600.0
        if span >= gap_hours:
            cut = i
            break
    return Batch(live=rows[cut:], stale=rows[:cut])


def handle_pending(eng, edges: Edges, now: datetime, *,
                   announce: Callable[[str], None] | None = None,
                   close_session: Callable[[], object] | None = None,
                   tz=None) -> Outcome | None:
    """Разобрать очередь и сделать ход. Пусто — `None`, и это не ошибка."""
    rows = eng.pending()
    if not rows:
        return None

    batch = split_batch(rows)
    if batch.stale:
        logging.info(
            "реплик по ту сторону разрыва: %s — текст выброшен, счёт учтён",
            len(batch.stale),
        )
        with eng.unit():
            eng.mark_handled([r["id"] for r in batch.stale], now)
            counted = eng.bump_dropped(len(batch.stale))
        if not counted:
            logging.warning(
                "открытой сессии нет — %s отброшенных реплик не попадут в "
                "счёт эпизода; текст остаётся в inbox", len(batch.stale))
        if close_session is not None:
            close_session()

    return handle_turn(
        eng, edges, batch.text, now,
        announce=announce, tz=tz,
        arrived_at=batch.arrived_at, inbox_ids=batch.ids,
    )


def handle_turn(eng, edges: Edges, text: str, now: datetime, *,
                announce: Callable[[str], None] | None = None,
                arrived_at: datetime | None = None,
                inbox_ids: list[int] | tuple = (),
                tz=None) -> Outcome:
    """Один ход целиком: от реплики до записанных выводов."""
    turn = eng.snapshot(now)

    findings = look_outward(eng, text, turn.objects, edges, now)

    previous = eng.last_exchange()

    try:
        messages = (
            [{"role": "system",
              "content": prompt_and_latch(eng, edges, now, previous, findings, tz)}]
            + eng.working_memory()
            + [{"role": "user", "content": text}]
        )
        response = edges.llm.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=messages,
        )
    except OpenAIError as err:
        return Outcome(answer=None, error=str(err))

    answer = response.choices[0].message.content

    try:
        with eng.unit():
            eng.touch_exchange(now)
            reply_id = eng.append_exchange(text, answer, now, arrived_at)
            if inbox_ids:
                claimed = eng.mark_handled(inbox_ids, now, reply_id)
                if len(claimed) != len(inbox_ids):
                    raise _Superseded
    except _Superseded:
        logging.warning(
            "пачку %s забрал другой потребитель — ответ выброшен, T1 откачен",
            list(inbox_ids),
        )
        return Outcome(answer=None, superseded=True)

    if announce is not None:
        announce(answer)

    t = time.monotonic()
    turn = eng.snapshot(now)

    new_mood = reflect_mood(turn, text, answer, edges.llm)
    logging.info("reflect_mood: %.1fs", time.monotonic() - t)

    new_assertions = reflect_self(turn, text, answer, edges.llm)
    logging.info("reflect_self: %.1fs", time.monotonic() - t)

    candidates = list(extract_objects(turn, text, answer, edges.llm, findings))
    logging.info("extract_objects: %.1fs", time.monotonic() - t)

    with eng.unit():
        if new_mood:
            eng.set_mood(new_mood)
        if new_assertions:
            eng.merge_self_assertions(new_assertions, now)
        for cand in candidates:
            eng.upsert_object(cand, now)

    logging.info("complete_cycle: %.1fs", time.monotonic() - t)
    return Outcome(answer=answer)