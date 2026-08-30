"""Ход — функция, а не тело цикла (Шаг 27)."""

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
RETRIEVER_COOLDOWN_HOURS = 1.0


@dataclass(frozen=True)
class Edges:
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
    answer: str | None
    error: str | None = None
    superseded: bool = False


class _Superseded(Exception):
    """Пачку забрали. Роняет T1, чтобы откатить обмен."""


def open_edges() -> Edges:
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
            eng.enqueue_digest(reply_id, findings)
    except _Superseded:
        logging.warning(
            "пачку %s забрал другой потребитель — ответ выброшен, T1 откачен",
            list(inbox_ids),
        )
        return Outcome(answer=None, superseded=True)

    if announce is not None:
        announce(answer)
    return Outcome(answer=answer)


def digest_one(eng, edges: Edges, now: datetime) -> bool:
    """Один отложенный T2. False — работы нет или сеть упала, надо подождать."""
    job = eng.next_digest()
    if not job:
        return False
    try:
        pair = eng.exchange_by_reply(job["reply_id"])
        if not pair:
            with eng.unit():
                eng.mark_digest_done(job["id"], now)
            return True
        turn = eng.snapshot(now)
        findings = job["findings"]
        t = time.monotonic()
        new_mood = reflect_mood(turn, pair["user_text"], pair["answer"], edges.llm)
        logging.info("digest reflect_mood: %.1fs", time.monotonic() - t)
        new_assertions = reflect_self(
            turn, pair["user_text"], pair["answer"], edges.llm)
        logging.info("digest reflect_self: %.1fs", time.monotonic() - t)
        candidates = list(extract_objects(
            turn, pair["user_text"], pair["answer"], edges.llm, findings))
        logging.info("digest extract_objects: %.1fs", time.monotonic() - t)
        with eng.unit():
            if new_mood:
                eng.set_mood(new_mood)
            if new_assertions:
                eng.merge_self_assertions(new_assertions, now)
            for cand in candidates:
                eng.upsert_object(cand, now)
            eng.mark_digest_done(job["id"], now)
        return True
    except Exception as err:
        logging.warning("digest_one: %s — повторю на следующем круге", err)
        return False