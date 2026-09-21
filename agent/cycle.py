"""Ход — функция, а не тело цикла (Шаг 27)."""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import config
import genesis
import library
import outside
import sky as sky_mod
import embed as embed_mod
# Выбор книги живёт в `shelf_choice` (Шаг 49.1). До Шага 56 он подменялся
# здесь из `agent.py` присваиванием `cycle.choose_book = ...`, и сбруя, которая
# `agent.py` не импортирует, проверяла копию из `mind`, а демон жил другой.
from shelf_choice import choose_book
import timeutil
import web
from mind import (CONSPECTUS_LIMIT, VERDICT_WRITE, build_system_prompt,
                  check_memory, decide_query, dream,
                  dream_subject, extract_memories, extract_objects, linger,
                  notice_promise, propose_birthplaces, propose_names,
                  read_portion, reflect_mood, reflect_self, reflect_traits,
                  day, say_promise, speak_first, weather_family)
from snapshot import SESSION_GAP_HOURS, clip_text, iso
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
    # Ключ AI Studio (Шаг 58). Пустой — векторов нет, память работает по весу.
    # Отдельно от `search_key`, потому что сбруя заводит поиск во всех
    # сценариях, а векторы — только в своём: иначе каждый прежний эталон
    # поменялся бы от того, что появился новый край.
    ai_key: str = ""

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
        ai_key=config.YANDEX_API_KEY,
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
                     findings: list[dict] | None = None, tz=None,
                     about: list[float] | None = None) -> str:
    tz = tz or config.TZ
    place = eng.place()
    snap = sky_mod.local_snapshot(place.get("lat"), place.get("lon"), now_dt, tz)
    wx = weather_snapshot(place, edges, now_dt)
    # `about` — вектор реплики собеседника (Шаг 58). К воспоминаниям по весу
    # добавляются близкие по смыслу, и они отмечаются вспомненными ниже тем же
    # `touch_recall`: вспомнить к слову — тоже вспомнить.
    turn = eng.snapshot(now_dt, about=about)
    prompt = build_system_prompt(
        turn,
        now_dt.astimezone(tz),
        snap,
        wx,
        previous,
        findings,
    )
    # Вспоминание отмечается ЗДЕСЬ, потому что здесь снимок стал промптом
    # (Шаг 40). Единица уже открыта под латч среды, и второй не заводится:
    # обе записи — след одного и того же прочтения памяти.
    with eng.unit():
        eng.remember_outside(snap, wx, now_dt, weather_family(wx))
        eng.touch_recall(turn.memories, now_dt)
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
    about = embed_mod.embed(text, "query", edges)
    try:
        messages = (
            [{"role": "system",
              "content": prompt_and_latch(eng, edges, now, previous, findings, tz,
                                          about=about)}]
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
        new_mood = reflect_mood(turn, pair["user_text"], pair["answer"],
                                edges.llm, now=now)
        logging.info("digest reflect_mood: %.1fs", time.monotonic() - t)
        new_assertions = reflect_self(
            turn, pair["user_text"], pair["answer"], edges.llm)
        logging.info("digest reflect_self: %.1fs", time.monotonic() - t)
        candidates = list(extract_objects(
            turn, pair["user_text"], pair["answer"], edges.llm, findings))
        logging.info("digest extract_objects: %.1fs", time.monotonic() - t)
        remembered = _biograph(eng, edges, turn, pair, now)
        logging.info("digest биограф: %.1fs", time.monotonic() - t)
        # Обещание (Шаг 43). Отсчёт идёт от `asked_at` — момента, когда
        # просили, — а не от `now`: T2 отстаёт от разговора на минуты, а после
        # простоя демона может отстать на часы, и «через пять часов»,
        # посчитанное от разбора, сдвинулось бы вместе с очередью.
        asked_at = pair.get("asked_at") or now
        promised = notice_promise(
            turn, pair["user_text"], pair["answer"], edges.llm,
            asked_at=asked_at, tz=config.TZ)
        logging.info("digest обещание: %.1fs", time.monotonic() - t)
        with eng.unit():
            # `None` — настроение не менялось, и это обычный исход (Шаг 44).
            # Метка `mood_since` при этом не двигается: в том и инерция.
            if new_mood:
                eng.set_mood(new_mood[0], new_mood[1], now)
            if new_assertions:
                eng.merge_self_assertions(new_assertions, now)
            for cand in candidates:
                eng.upsert_object(cand, now)
            for happened_at, precision, text in remembered:
                eng.add_memory(happened_at, precision, text, "told", now=now)
            if promised:
                due_at, what = promised
                eng.add_promise(due_at, what, pair.get("asked_id"),
                                now=asked_at)
                logging.info("обещание записано: %s -> %s", iso(due_at), what)
            eng.mark_digest_done(job["id"], now)
        return True
    except Exception as err:
        logging.warning("digest_one: %s — повторю на следующем круге", err)
        return False


def _biograph(eng, edges: Edges, turn, pair: dict, now: datetime) -> list[tuple]:
    """Биограф плюс сверка. Возвращает готовое к записи, ничего не пишет сам.

    Не пишет намеренно: запись T2 идёт одной транзакцией в `digest_one`, и
    воспоминание обязано лечь вместе с настроением и объектами, а не отдельно.
    Иначе при падении между ними разговор оставил бы след в биографии, но не
    в памяти об объектах — то есть состояния, которого не бывает после
    успешного хода.

    Возраст в дату переводит КОД, а не модель. Модель называет, сколько ему
    было лет, — так человек и помнит, — а календарную метку считает тот, у
    кого есть `born_at`. Спроси мы у модели дату, она выдала бы правдоподобное
    число дня и месяца для события, которое датируется в лучшем случае годом,
    и `precision` осталось бы стоять при фальшивой точности.
    """
    born = timeutil.parse_ts(turn.born_at or "")
    age_now = timeutil.age_years(born, now)
    if born is None or age_now is None:
        return []

    canon = eng.all_memories()
    found = extract_memories(turn, pair["user_text"], pair["answer"],
                             edges.llm, canon, born, age_now)
    out = []
    for cand in found:
        happened_at = born + timedelta(days=cand["age"] * timeutil.DAYS_IN_YEAR)
        verdict = check_memory(
            {**cand, "happened_at": iso(happened_at)}, canon, born, edges.llm)
        logging.info("сверка (%s лет, %s): %s — %s",
                     cand["age"], cand["precision"], verdict, cand["text"][:60])
        if verdict == VERDICT_WRITE:
            out.append((happened_at, cand["precision"], cand["text"]))
    return out

# =============================================================================
# Фоновый ход (Шаг 35): персонаж заговаривает сам
# =============================================================================
# Пороги инициативы. Живут ЗДЕСЬ, а не в хранилище: база копит побуждение,
# решает цикл. Затухание, наоборот, в `store_pg` — оно считается внутри SQL.
#
# Числа выбраны на глаз и правятся на живом, как `RETRIEVER_COOLDOWN_HOURS`.
# Смысл у них разный, и потому их три, а не одно:
IMPULSE_FLOOR = 1.0          # ниже — повод есть, но говорить не о чем
UTTERANCE_COOLDOWN_HOURS = 2.0   # не чаще, чем раз в столько, что бы ни копилось
UTTERANCES_PER_DAY = 6       # потолок суток; молчаливость дешевле навязчивости
IMPULSE_DAMP = 0.3           # во сколько глушатся прочие поводы после реплики

# Побуждение от тишины. Не «сколько часов молчим», а сколько это в сутках:
# так порог не придётся пересчитывать, если сутки перестанут быть мерой.
SILENCE_URGE_PER_DAY = 1.4
SILENCE_START_HOURS = 6.0    # раньше — не молчание, а пауза в разговоре

# Побуждение от перемены за окном. Разовое и крупное: смена семейства —
# новость, и если о ней не сказать сейчас, говорить будет не о чем.
WEATHER_URGE = 1.2
WEATHER_TTL_HOURS = 4.0      # дождь, о котором вспомнили к ночи, — не новость


@dataclass(frozen=True)
class Urge:
    """Событийное побуждение, замеченное на одном заходе.

    **Поля `mode` здесь больше нет (Шаг 37).** Оно различало событие и
    состояние, и различение было верным, а вывод из него — нет: состояние
    писалось в ту же таблицу, что и событие, и потому требовало переписывания
    каждый заход, чтобы не устареть. Из этого следовал фоновый писатель,
    правивший строку `silence` каждые пять секунд, — 17 280 записей в сутки
    ради величины, которая до порога доползает раз в сутки.

    Правильный вывод из того же различения — состояние в таблице вообще не
    хранить. Оно вычисляется из уже записанного (`last_exchange`,
    `last_utterance`), а хранить производное от записанного значит заводить
    второй факт об одном и том же. Отсюда `silence_urge` рядом, и отсюда же
    в `impulses` остались только события.
    """

    kind: str
    subject: str | None
    amount: float
    expires_at: datetime | None = None


def silence_urge(eng, now: datetime) -> float:
    """Сила побуждения нарушить тишину. ВЫЧИСЛЯЕТСЯ, не хранится.

    Ноль — говорить не о чем: либо разговаривали недавно, либо разговора не
    было вовсе (персонажу нечего нарушать).

    Меряется от последнего сказанного ЗДЕСЬ — чужого или своего. Первый
    набросок смотрел только на `last_exchange`, то есть на разговор, и это
    давало навязчивость с гарантией: персонаж говорил в пустоту, ему не
    отвечали, метка не двигалась — и на следующем заходе сила тишины
    пересчитывалась ещё БОЛЬШЕЙ. Проверено внесением: 1.76 -> 1.94 за три
    часа. Собственная реплика разговора не создаёт, но ПОВОД исчерпывает:
    сказать снова то же самое в ту же пустоту нечего.

    Самозатухание — главная выгода от того, что величина вычисляемая. Стоит
    персонажу заговорить, `last_utterance` сдвигается, и побуждение падает в
    ноль само, без пометок и приглушений. Хранимому пришлось бы делать это
    записью, а записи можно не сделать.

    `agent.last_exchange_ts` при этом собственной репликой не двигается
    (`append_utterance`), и это не противоречие, а разделение: метка отвечает
    на «когда мы разговаривали» и уезжает в промпт словами «прошлый разговор
    был давно» — там собственная реплика была бы ложью.
    """
    stamps = [t for t in (eng.last_exchange(), eng.last_utterance()) if t]
    if not stamps:
        return 0.0
    hours = (now - max(stamps)).total_seconds() / 3600.0
    if hours < SILENCE_START_HOURS:
        return 0.0
    return SILENCE_URGE_PER_DAY * hours / 24.0


def sense_impulses(eng, now: datetime, wx) -> list[Urge]:
    """Какие СОБЫТИЯ произошли с прошлого захода. Чувствует, но НЕ пишет.

    Разделено сознательно: список поводов — ровно то, что сбруя обязана уметь
    проверить без базы и без модели. Смешай сюда запись, и проверять пришлось
    бы через хранилище, то есть через два слоя вместо нуля.

    Тишины здесь нет: она состояние, а не событие, и живёт в `silence_urge`.

    Сети здесь нет: `wx` приезжает готовым от вызывающего, как и в
    `prompt_and_latch`. Нечистое живёт на краю.
    """
    out: list[Urge] = []

    family = weather_family(wx)
    if family:
        # Латч КОЛОНКОЙ, а не снимком (Шаг 37): фоновому заходу нужно одно
        # поле, а `eng.snapshot` собирал ради него всю память шестью
        # запросами — 79% стоимости захода уходило на это.
        latch = eng.outside_latch()
        if latch.get("weather") and latch["weather"] != family:
            out.append(Urge("weather", family, WEATHER_URGE,
                            expires_at=now + timedelta(hours=WEATHER_TTL_HOURS)))

    return out


# =============================================================================
# День (Шаг 46): что было, пока никто не смотрел
# =============================================================================
# **Отдельный заход, а не ветка сна**, хотя оба пишут без собеседника и оба
# ходят к модели. Дела разные и, главное, времена разные: сон случается ночью,
# день подводится вечером, и слитые в один они дрались бы за один заход.
#
# **Вечер считается по ЧАСАМ, а не по свету** — и это единственное осознанное
# расхождение с заслонками сна. У сна свет верен по существу: сон про темноту,
# и в белые ночи ему честнее не сниться вовсе. Конец дня темнотой не
# определяется: в июне в высоких широтах вечера в смысле света нет, а день
# всё равно кончается. Часы работают везде одинаково, и это тот случай, когда
# простое правило вернее точного.
#
# Заслонок три, и они те же по природе, что у сна:
#   вечер       — день подводят, когда он кончился. Днём проход писал бы
#                 утро как целые сутки;
#   тишина      — если собеседник рядом, день ещё не кончился. Порог тот же
#                 час, что у сна: час без реплик — уже не разговор;
#   раз в сутки — запись необратима. Два вечера за один вечер не «подробный
#                 день», а вдвое быстрее исписанная жизнь.
# Нити (Шаг 47). Через сколько брошенное закрывается само. Три недели — срок,
# за который человек либо возвращается к начатому, либо уже не вернётся; он же
# достаточно велик, чтобы отпуск или болезнь не стёрли всё разом.
THREAD_FORGET_DAYS = 21.0
THREAD_DONE = "кончилось"
THREAD_FORGOTTEN = "забылось"

# Сколько общих значимых слов считаем упоминанием. Два: одно даёт ложные
# срабатывания на любом «работа» или «письмо», три — почти не срабатывает на
# фразах в полдюжины слов.
THREAD_MENTION_WORDS = 2


def _mentions(thread_text: str, scenes: list[str]) -> bool:
    """Упоминают ли сцены эту нить. Грубо, по общим словам, и НАМЕРЕННО грубо.

    Точный ответ дала бы модель, но это лишний вызов каждый вечер ради
    величины, ошибка в которой стоит недорого: не заметили возвращение — нить
    остынет на день раньше; заметили лишнее — проживёт на день дольше. Ни то
    ни другое не попадает в канон и ничего не удаляет.
    """
    def significant(text: str) -> set[str]:
        return {w for w in "".join(
            c.lower() if c.isalnum() else " " for c in text).split()
            if len(w) > 4}

    want = significant(thread_text)
    if not want:
        return False
    return any(len(want & significant(scene)) >= THREAD_MENTION_WORDS
               for scene in scenes)


DAY_HOUR_FROM = 20          # с восьми вечера по его месту
DAY_HOUR_TO = 24            # до полуночи; после — уже ночь и уже сон
DAY_QUIET_HOURS = 1.0       # столько никто не пишет — считаем, что он один
DAY_INTERVAL_HOURS = 20.0   # не чаще; сутки минус запас на сдвиг вечера


def day_tick(eng, edges: Edges, now: datetime, *, tz=None) -> list[str] | None:
    """Один вечерний заход. `None` — не время; `[]` — день был пустым.

    Различие между `None` и `[]` здесь значимое, в отличие от большинства
    проходов: пустой день — это состоявшаяся работа (модель спрошена, ответ
    «ничего»), и заслонка «раз в сутки» обязана его учесть. Слей мы их, и
    пустой вечер приводил бы к повторному вызову модели каждые пять секунд до
    полуночи.
    """
    tz = tz or config.TZ
    local = now.astimezone(tz)
    if not (DAY_HOUR_FROM <= local.hour < DAY_HOUR_TO):
        return None

    stamps = [t for t in (eng.last_exchange(), eng.last_utterance()) if t]
    if stamps:
        idle = (now - max(stamps)).total_seconds() / 3600.0
        if 0.0 <= idle < DAY_QUIET_HOURS:
            return None

    # Заслонка стоит на МЕТКЕ, а не на последней записи: пустой день записи не
    # оставляет, и держать на ней суточный интервал значило бы звать модель
    # каждые несколько секунд до полуночи - ровно в тот вечер, когда персонажу
    # нечего сказать.
    settled = eng.day_at()
    if settled is not None:
        hours = (now - settled).total_seconds() / 3600.0
        if 0.0 <= hours < DAY_INTERVAL_HOURS:
            return None

    previous = eng.last_lived()

    # Снимок и канон - после заслонок, как у сна и у модели в фоновом заходе.
    turn = eng.snapshot(now)
    born = timeutil.parse_ts(turn.born_at or "")
    age_now = timeutil.age_years(born, now)
    if born is None or age_now is None:
        return None

    # Погода берётся из ЛАТЧА, а не из сети. Вечерний заход не должен зависеть
    # от чужого сервиса: не ответил - день всё равно был. Латч при этом пишет
    # фоновый заход, который ходит в сеть по своему расписанию, так что свежее
    # значение тут почти всегда есть, а почти всегда - достаточно для антуража.
    weather = (eng.outside_latch() or {}).get("weather")

    # Забывание — до прохода и без модели (Шаг 47). Три недели тишины это
    # арифметика, а не суждение, и брошенная линия не должна ехать в промпт
    # как живая: спрошенный про неё персонаж принялся бы её продолжать.
    with eng.unit():
        forgotten = eng.forget_threads(now, THREAD_FORGET_DAYS, THREAD_FORGOTTEN)
    for t in forgotten:
        logging.info("нить забылась: %s", t["text"][:60])

    threads = eng.open_threads("self")
    canon = eng.all_memories()

    # Сделанное за окно (Шаг 50). Окно — от прошлой метки до сейчас, а не
    # «сутки назад»: пропусти проход вечер (гости, сеть лежала, демон не
    # работал), и прочитанное в тот вечер не должно пропасть из жизни только
    # потому, что подвести день не успели. Без метки — сутки, потому что
    # первому вечеру не от чего отсчитывать.
    #
    # Спрашивается ПОСЛЕ заслонок, вместе с каноном, и по той же причине: до
    # этого места доходит один заход из многих сотен.
    since = settled or (now - timedelta(days=1))
    deeds = eng.deeds_between(since, now)

    got = day(turn, canon, born, age_now, edges.llm,
              now=local, weather=weather,
              yesterday=previous["text"] if previous else None,
              threads=threads, deeds=deeds)
    scenes = got["scenes"]

    # Ворота сверки - те же, что у биографа и у вспомненного во сне. Прожитое
    # МОЖЕТ противоречить канону (в отличие от приснившегося), и ложится оно
    # навсегда.
    written = []
    for scene in scenes:
        verdict = check_memory(
            {"text": scene, "precision": "day", "happened_at": iso(now)},
            canon, born, edges.llm)
        logging.info("день, сверка: %s — %s", verdict, scene[:60])
        if verdict == VERDICT_WRITE:
            written.append(scene)

    # Нити, к которым сегодня возвращались, узнаются по СЦЕНАМ, а не по
    # отдельному полю в ответе. Поле пришлось бы держать в согласии со
    # сценами — то есть спрашивать модель дважды об одном, — а сцена,
    # упоминающая дело, и есть возвращение к нему.
    touched = [t["id"] for t in threads
               if written and _mentions(t["text"], written)]

    with eng.unit():
        for scene in written:
            eng.add_memory(now, "day", scene, "lived", now=now)
        for text in got["opened"]:
            eng.open_thread("self", text, now)
        if touched:
            eng.touch_threads(touched, now)
        if got["closed"]:
            eng.close_threads(got["closed"], now, THREAD_DONE)
        # Метка двигается ВСЕГДА, в том числе после пустого дня и после сцен,
        # отвергнутых сверкой. Тот же приём, что у `traits_at` (Шаг 42):
        # неудача - не повод повторять её через пять секунд на тех же входах.
        eng.set_day_at(now)
    if not written:
        # Пустой день при ПУСТОМ окне — обычное дело. Пустой день при
        # сделанном — другое: он что-то делал, и в биографии этого не будет.
        # Не ошибка (сверка могла отвергнуть сцену, модель могла промолчать),
        # но и не рядовой исход, и видеть его надо отдельно от первого.
        if any(deeds.values()):
            logging.warning(
                "день: записывать нечего, хотя сделанное было — %s",
                ", ".join(f"{k}: {len(v)}" for k, v in deeds.items() if v))
        else:
            logging.info("день: записывать нечего")

    for scene in written:
        logging.info("прожито: %s", scene[:80])
    return written


# =============================================================================
# Годовщины (Шаг 45): повод, который приносит календарь
# =============================================================================
# `impulses.kind = 'anniversary'` назван в `0002_initiative.sql` и с тех пор
# пустовал дольше всех — `curiosity` заполнился на Шаге 41, `dream` на Шаге 40.
# Ждал он не очереди, а оси жизни: до Шага 36 у персонажа не было даты
# рождения, а до Шага 38 — датированных воспоминаний, то есть годовщине нечему
# было быть годовщиной.
#
# **Единственный повод в проекте, который не стоит НИ ОДНОГО вызова модели и
# ни одного обращения к сети.** День рождения считается вычитанием из
# `born_at`, остальное — одним узким запросом к канону. Это делает его самым
# дешёвым источником инициативы и, что важнее, единственным, который работает
# при лежащей сети.
#
# **Сегодня — по ЕГО месту**, как ночь у сна. Календарь — вещь местная, и
# годовщина, наступившая по часам сервера, наступила не у него.

# День рождения весомее прочих годовщин, и разрыв намеренно велик. Прочие —
# повод вспомнить; свой день рождения человек либо отмечает, либо нарочито не
# отмечает, но мимо не проходит.
BIRTHDAY_URGE = 1.8
ANNIVERSARY_URGE = 1.3

# Столько не заводим повторно. Двадцать часов, а не двадцать четыре: сутки
# ровно означали бы, что на следующий день повод не заведётся, если заход
# случится на минуту раньше вчерашнего.
ANNIVERSARY_ONCE_HOURS = 20.0

# Обрезка предмета. Короче, чем у сна: там `subject` — выжимка текста, который
# целиком лежит в `memories`, а здесь в предмет уезжает ещё и «сколько лет
# назад», и длинный хвост вытолкнул бы его из ремарки.
ANNIVERSARY_SUBJECT_LIMIT = 90


def _end_of_day(now: datetime, tz) -> datetime:
    """Полночь следующих суток по месту. Это и есть срок годности годовщины.

    Не «плюс двадцать часов»: годовщина перестаёт быть сегодняшней ровно в
    полночь, и повод, доживший до завтра, говорил бы «сегодня» про вчера.
    """
    local = now.astimezone(tz)
    midnight = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


def sense_anniversaries(eng, now: datetime, tz) -> list[Urge]:
    """Что сегодня за день. Чувствует, но НЕ пишет — как `sense_impulses`.

    Отдельной функцией, а не веткой `sense_impulses`, по двум причинам.
    Первая: писатель другой (`note_anniversary` вместо `record_urge`), и
    смешанный список пришлось бы разбирать по роду на записи. Вторая: здесь
    нужен пояс, а `sense_impulses` его не знает и знать не должен — погода с
    тишиной календаря не касаются.
    """
    out: list[Urge] = []
    today = now.astimezone(tz).date()
    expires = _end_of_day(now, tz)

    born = eng.born_at()
    if born is not None:
        years = today.year - born.year
        if (born.month, born.day) == (today.month, today.day) and years >= 1:
            out.append(Urge(
                "anniversary",
                f"тебе сегодня {years} {timeutil.years_word(years)}",
                BIRTHDAY_URGE, expires_at=expires))

    for m in eng.memories_on(today.month, today.day):
        years = today.year - m["happened_at"].year
        if years < 1:
            continue
        subject = (f"ровно {years} {timeutil.years_word(years)} назад: "
                   f"{clip_text(m['text'], ANNIVERSARY_SUBJECT_LIMIT)}")
        out.append(Urge("anniversary", subject, ANNIVERSARY_URGE,
                        expires_at=expires))

    return out


def _pick_impulse(eng, now: datetime) -> dict | None:
    """Самый сильный повод выше порога: вычисляемый или хранимый.

    Возвращает словарь той же формы, что и строка `impulses`, но `id` может
    быть `None` — у вычисленного повода строки нет. Это единственное отличие,
    и оно намеренно не спрятано: тому, кто будет помечать повод сказанным,
    нужно знать, есть ли что помечать.
    """
    candidates: list[dict] = []

    quiet = silence_urge(eng, now)
    if quiet >= IMPULSE_FLOOR:
        candidates.append({"id": None, "kind": "silence", "subject": None,
                           "urge": quiet})

    stored = eng.strongest_impulse(now, IMPULSE_FLOOR)
    if stored:
        candidates.append({"id": stored["id"], "kind": stored["kind"],
                           "subject": stored["subject"],
                           "urge": float(stored["urge"])})

    if not candidates:
        return None
    return max(candidates, key=lambda c: c["urge"])


# =============================================================================
# Обещания (Шаг 43): сначала долг, потом желание
# =============================================================================
# **Отдельный заход, а не ветка `background_tick`** — по тому же доводу, по
# которому отдельным сделан сон: дела разные. Фоновый заход решает «говорить
# или молчать» и имеет право ответить «молчать» всегда. Здесь решать нечего:
# срок пришёл, значит сказать надо.
#
# Отсюда и место в `agent.idle_tick` — ДО фонового захода. Порядок не
# косметический: заслонки инициативы (порог, пауза, бюджет суток) устроены
# так, чтобы персонаж не был навязчивым, и пропустить сквозь них долг
# невозможно — исчерпанный бюджет проглотил бы напоминание молча, и в логе
# это выглядело бы штатной работой.
#
# Специальной координации между заходами не понадобилось. Напомнив, персонаж
# зовёт `append_utterance`; `background_tick` на том же круге увидит
# `last_utterance` нулевой давности и промолчит по собственной паузе. Заслонка
# сработала ровно так, как задумана: человек услышал реплику, и вторая подряд
# ему не нужна.

# Через сколько напомнить второй раз, если ответа не было. Полчаса: меньше —
# и это уже понукание, больше — и напоминание опоздает к делу, ради которого
# заводилось.
PROMISE_REPEAT_HOURS = 0.5

# Сколько раз всего. Два: сказать и повторить. Правило то же, что у нитей и у
# любопытства — спросить один раз забота, три надзор. Молчание в ответ на
# второй раз означает, что человек увидел и не хочет отвечать, и третий раз
# спорил бы с его решением.
PROMISE_ATTEMPTS = 2


def promise_tick(eng, edges: Edges, now: datetime, *,
                 announce: Callable[[str], None] | None = None,
                 tz=None) -> str | None:
    """Один заход по обещаниям: закрыть отвеченные, напомнить о созревшем.

    Возвращает сказанное или `None`. В отличие от фонового захода, `None`
    здесь означает «нечего напоминать», а не «решил промолчать».

    Заслонок нет ни одной, и это осознанно. Единственное ограничение —
    `PROMISE_ATTEMPTS`, и оно не заслонка от навязчивости, а признание того,
    что после второго молчания напоминать больше нечего.
    """
    # Закрытие отвеченных идёт первым и в своей транзакции: это уборка, она
    # не зависит от того, найдётся ли созревшее, и терять её из-за упавшего
    # ниже вызова модели незачем.
    spoke_at = eng.last_exchange_ts()
    if spoke_at is not None:
        with eng.unit():
            closed = eng.close_acknowledged_promises(spoke_at)
        if closed:
            logging.info("обещания: закрыто по ответу собеседника: %d", closed)

    row = eng.due_promise(now, PROMISE_REPEAT_HOURS)
    if not row:
        return None

    promise = dict(row)
    attempt = (promise["repeats"] or 0) + 1
    # Опоздание считается ТОЛЬКО для первой попытки. На повторе `due_at`
    # отстоит на `PROMISE_REPEAT_HOURS` по устройству, и та же арифметика
    # выдавала бы «ты опоздал на час» там, где персонаж напомнил вовремя и
    # повторяет по плану. Извиняться за это — врать в другую сторону.
    late_hours = (max((now - promise["due_at"]).total_seconds() / 3600.0, 0.0)
                  if attempt == 1 else 0.0)

    tz = tz or config.TZ
    place = eng.place()
    snap = sky_mod.local_snapshot(place.get("lat"), place.get("lon"), now, tz)

    # Погоды здесь нет намеренно, в отличие от `background_tick`. Там она
    # нужна, чтобы ЗАМЕТИТЬ событие — пропустишь, и оно потеряно. Здесь
    # событие уже известно, а лезть в сеть ради антуража одной фразы значило
    # бы поставить напоминание в зависимость от чужого сервиса.
    turn = eng.snapshot(now)
    text = say_promise(
        turn, promise, edges.llm,
        memory=eng.working_memory(),
        now=now.astimezone(tz), sky=snap, weather=None,
        last_exchange=eng.last_exchange(),
        late_hours=late_hours,
    )

    with eng.unit():
        eng.append_utterance(text, now)
        eng.mark_promise_said(promise["id"], now,
                              close=attempt >= PROMISE_ATTEMPTS)
        # Соседние побуждения глушатся так же, как после любой реплики:
        # человек услышал персонажа, и заговорить снова через паузу — то же
        # самое, от чего заслонка инициативы и защищает.
        eng.damp_impulses(IMPULSE_DAMP)

    logging.info("напомнил (обещание %d, попытка %d, опоздание %.1f ч): %s",
                 promise["id"], attempt, late_hours, text[:60])
    if announce is not None:
        announce(text)
    return text


def background_tick(eng, edges: Edges, now: datetime, *,
                    announce: Callable[[str], None] | None = None,
                    tz=None) -> str | None:
    """Один фоновый заход: заметить события и, может быть, заговорить.

    Возвращает сказанное или `None`. `None` — обычный исход: молчание здесь
    не отказ, а норма, и подавляющее большинство заходов кончаются им.

    **Заслонок три, и каждая отвечает своему провалу.** Порог отсекает «повод
    есть, но говорить не о чем»; пауза — «повод сильный, но мы только что
    разговаривали»; бюджет суток — «поводов много, и каждый по-своему прав».
    Одной величиной их не выразить: убери бюджет, и день с меняющейся погодой
    сделает персонажа невыносимым, хотя каждая отдельная реплика будет
    уместной.

    **Погода спрашивается до заслонок, а модель — после.** Порядок не
    случайный: погода дешёвая (кэш на 20 минут, 72 запроса в сутки) и нужна,
    чтобы событие вообще заметить — пропусти его, и оно потеряно, потому что
    второй раз не случится. Модель дорогая, и до неё доходит только заход,
    прошедший все три заслонки, то есть не чаще `UTTERANCES_PER_DAY` раз в
    сутки.
    """
    tz = tz or config.TZ
    place = eng.place()
    snap = sky_mod.local_snapshot(place.get("lat"), place.get("lon"), now, tz)
    wx = weather_snapshot(place, edges, now)

    urges = sense_impulses(eng, now, wx)
    if urges:
        with eng.unit():
            for u in urges:
                eng.record_urge(u.kind, u.subject, u.amount, now, u.expires_at)

    # Годовщины пишутся СВОИМ писателем (Шаг 45): накопление им противопоказано,
    # потому что повод держится весь день, а заходов за день тысячи. Стоят они
    # тут же, до заслонок, по тому же доводу, что и погода: замеченное надо
    # записать, даже если говорить сегодня уже не придётся.
    for u in sense_anniversaries(eng, now, tz):
        with eng.unit():
            if eng.note_anniversary(u.subject, u.amount, now, u.expires_at,
                                    ANNIVERSARY_ONCE_HOURS):
                logging.info("годовщина: %s", u.subject)

    # Пауза и бюджет спрашиваются ДО выбора повода: ни та ни другой не
    # зависят от того, какой повод победит, и спросить их первыми дешевле
    # ровно на один запрос к самой длинной таблице.
    said_last = eng.last_utterance()
    if said_last is not None:
        idle = (now - said_last).total_seconds() / 3600.0
        if 0.0 <= idle < UTTERANCE_COOLDOWN_HOURS:
            return None
    if eng.utterances_since(now - timedelta(days=1)) >= UTTERANCES_PER_DAY:
        logging.info("инициатива: бюджет суток исчерпан (%s)", UTTERANCES_PER_DAY)
        return None

    impulse = _pick_impulse(eng, now)
    if not impulse:
        return None

    turn = eng.snapshot(now)
    text = speak_first(
        turn, impulse, edges.llm,
        memory=eng.working_memory(),
        now=now.astimezone(tz), sky=snap, weather=wx,
        last_exchange=eng.last_exchange(),
    )
    if not text:
        # Повод НЕ гасится: модель промолчала, а побуждение никуда не делось.
        # Пометь его сказанным — и персонаж потерял бы то, о чём хотел
        # сказать, из-за одной неудачной попытки.
        return None

    with eng.unit():
        eng.append_utterance(text, now)
        if impulse["id"] is not None:
            eng.mark_spoken(impulse["id"], now)
        # Приглушается всегда, в том числе после вычисленного повода: человек
        # услышал одну реплику, а не реплику про погоду. Тишина гасится сама —
        # `append_utterance` сдвинул `last_utterance`.
        eng.damp_impulses(IMPULSE_DAMP)
        # Сказал про книгу — нерассказанное о ней перестало быть
        # нерассказанным (Шаг 49). Помечаются ВСЕ нерассказанные, а не одна
        # «та самая»: связи «повод — заметка» в базе нет, и заводить колонку
        # под один род повода значило бы угадывать схему (`0011_reading.sql`
        # отклонил связь книги с нитью по тому же доводу). Заметок между
        # двумя репликами накапливается одна-две, и цена ошибки — мысль,
        # которую он не проговорил вслух, но которая осталась в базе и в
        # промпте.
        if impulse["kind"] == "reading":
            eng.mark_notes_told([n["id"] for n in eng.untold_notes()], now)
        eng.remember_outside(snap, wx, now, weather_family(wx))
        # Вспомненное отмечается только когда персонаж ЗАГОВОРИЛ, а не когда
        # промпт собрался. Промолчавшая модель прочла память и ничего с ней
        # не сделала — считать это вспоминанием значило бы, что вес растёт от
        # неудачных попыток. Расхождение с `prompt_and_latch` намеренное: там
        # реплика уже отдана к моменту записи, здесь ещё нет.
        eng.touch_recall(turn.memories, now)

    logging.info("заговорил сам (%s, urge %.2f): %s",
                 impulse["kind"], impulse["urge"], text[:60])
    if announce is not None:
        announce(text)
    return text


# =============================================================================
# Сон (Шаг 40): биография растёт без собеседника
# =============================================================================
# **Отдельный заход, а не ветка `background_tick`.** Дела разные: фоновый
# заход решает «говорить или молчать», сон решает «записать». Слитые в один,
# они дали бы молчаливому заходу цену вызова модели — а молчаливых заходов
# подавляющее большинство, и весь Шаг 37 был про то, чтобы они были дешёвыми.
#
# **Ночь считается по ЕГО месту, а не по часам сервера и не по времени
# собеседника.** Персонаж живёт там, где живёт (`place_lat`/`place_lon`), и
# небо ему считает та же офлайновая арифметика, что рисует «за окном». Без
# места ночи нет — сон выключается, как выключается блок среды. Деградация,
# а не отказ: то же правило, что у `sky` и `web`.
#
# Заслонок три, и они не те же, что у инициативы, хотя выглядят похоже:
#   ночь        — сон не бывает днём, и это не эстетика: днём он превратился
#                 бы в фоновый генератор биографии, работающий, пока человек
#                 разговаривает;
#   тишина      — если собеседник рядом, персонаж не спит. Порог короткий:
#                 час без реплик — уже не разговор;
#   раз в сутки — сон необратим и пишется в таблицу, которая не
#                 переписывается. Две ночи подряд за одну ночь — не «много
#                 снов», а вдвое быстрее исписанное детство.
NIGHT_LIGHTS = frozenset({sky_mod.LIGHT_NAUTICAL, sky_mod.LIGHT_ASTRONOMICAL,
                          sky_mod.LIGHT_NIGHT})

# Гражданские сумерки в NIGHT_LIGHTS НЕ входят намеренно. В высоких широтах
# летом темнее гражданских не становится вовсе, и включи мы их — сон пришёлся
# бы на светлый вечер. Пусть лучше в белые ночи ему не снится ничего: пропуск
# заметен и честен, а сон в девять вечера выглядит поломкой.

DREAM_QUIET_HOURS = 1.0      # столько никто не пишет — считаем, что он один
DREAM_INTERVAL_HOURS = 20.0  # не чаще; сутки минус запас на сдвиг ночи
DREAM_WEIGHT = 1.6           # свежий сон всплывает в промпте сам, без правок
DREAM_URGE = 1.5             # выше порога сразу: сон — крупный повод
DREAM_TTL_HOURS = 14.0       # к вечеру рассказывать уже нечего


def dream_tick(eng, edges: Edges, now: datetime, *, tz=None) -> str | None:
    """Одна ночь: увидеть сон и записать его. Возвращает сон или `None`.

    `None` — обычный исход, как и у фонового захода: почти всегда сейчас не
    ночь, или он не один, или уже снилось.

    **Пишет, но не говорит.** Заведённый импульс подхватит `background_tick`
    своим чередом — может быть, через час, может быть, утром. Сказать сразу
    было бы соблазнительно и неверно: тогда сон существовал бы ради реплики,
    а он существует сам по себе. Ночная реплика при этом возможна, и это
    оставлено сознательно — человек спит и увидит её утром, а от навязчивости
    держат пауза и бюджет суток, те же, что у всех прочих поводов.
    """
    tz = tz or config.TZ
    place = eng.place()
    snap = sky_mod.local_snapshot(place.get("lat"), place.get("lon"), now, tz)
    if snap is None or snap.get("light") not in NIGHT_LIGHTS:
        return None

    stamps = [t for t in (eng.last_exchange(), eng.last_utterance()) if t]
    if stamps:
        idle = (now - max(stamps)).total_seconds() / 3600.0
        if 0.0 <= idle < DREAM_QUIET_HOURS:
            return None

    slept = eng.last_dream_at()
    if slept is not None:
        hours = (now - slept).total_seconds() / 3600.0
        if 0.0 <= hours < DREAM_INTERVAL_HOURS:
            return None

    # Снимок и канон спрашиваются ПОСЛЕ заслонок, как модель в
    # `background_tick`: до них доходит один заход из многих сотен.
    turn = eng.snapshot(now)
    born = timeutil.parse_ts(turn.born_at or "")
    age_now = timeutil.age_years(born, now)
    if born is None or age_now is None:
        return None

    canon = eng.all_memories()
    seen = dream(turn, canon, born, age_now, edges.llm, now=now.astimezone(tz))
    if not seen:
        return None

    # Вспомненное проходит ТЕ ЖЕ ворота, что кандидат биографа. Ворота одни на
    # обоих писателей — два разных правила «что считать противоречием»
    # разъехались бы, и разъехались бы молча.
    recalled = seen.get("recalled")
    verdict = None
    happened_at = None
    if recalled:
        happened_at = born + timedelta(
            days=recalled["age"] * timeutil.DAYS_IN_YEAR)
        verdict = check_memory(
            {**recalled, "happened_at": iso(happened_at)}, canon, born,
            edges.llm)
        logging.info("сон, сверка (%s лет, %s): %s — %s",
                     recalled["age"], recalled["precision"], verdict,
                     recalled["text"][:60])

    with eng.unit():
        # Сон датируется сегодняшней ночью и точностью до дня: он и правда
        # случился в этот день, и это единственное воспоминание, у которого
        # дата известна безусловно.
        eng.add_memory(now, "day", seen["dream"], "dream", DREAM_WEIGHT,
                       now=now)
        if recalled and verdict == VERDICT_WRITE:
            eng.add_memory(happened_at, recalled["precision"],
                           recalled["text"], "inferred", now=now)
        eng.record_urge("dream", dream_subject(seen["dream"]), DREAM_URGE, now,
                        now + timedelta(hours=DREAM_TTL_HOURS))

    logging.info("приснилось: %s", seen["dream"][:80])
    return seen["dream"]


# =============================================================================
# Чтение (Шаг 49): дело, которого нельзя выдумать
# =============================================================================
# **Первый проход, у которого результат лежит вне базы.** Всё, что персонаж
# «делал» до сих пор, сочинял вечерний проход из его же канона: текст о тексте,
# и спорить с ним нечем. Прочитанная страница либо прочитана, либо нет, и
# позиция в файле знает об этом больше, чем модель.
#
# **Заход делает ОДНО из двух: берёт книгу или читает.** Не оба подряд: взяв
# книгу, персонаж не садится читать её в ту же минуту — он взял её и пошёл
# дальше, а вечер чтения придёт своим чередом. Слитые вместе, они стоили бы
# двух вызовов модели на одном круге, и первый вечер новой книги отличался бы
# от прочих вдвое.
#
# **Заслонок три, и они те же по природе, что у сна и дня:**
#   часы        — читает он не ночью и не в пять утра. Окно широкое (день и
#                 вечер), потому что чтение, в отличие от сна и итога дня, к
#                 часу не привязано: человек читает, когда выдалось время;
#   тишина      — если собеседник рядом, персонаж не читает. Порог тот же час,
#                 что у сна и дня;
#   интервал    — не чаще раза в шесть часов, а у подхода к полке — раза в
#                 сутки. Числа выбраны от жизни, как `PORTION_CHARS`: два-три
#                 захода в сутки — это человек, который много читает, но не
#                 машина, глотающая роман за день; к полке же подходят реже,
#                 чем садятся за начатую книгу. Почему интервала два, а не
#                 один, сказано у `CHOICE_INTERVAL_HOURS`.
#
# Метка интервала — `agent.read_at`, и двигается она ВСЕГДА, в том числе когда
# персонаж отказался брать книгу и когда модель не ответила. Почему её
# пришлось завести, хотя `0011_reading.sql` объявил её ненужной, разобрано в
# `0012_reading_pass.sql`.

READING_HOUR_FROM = 9        # раньше — он ещё не проснулся
READING_HOUR_TO = 24         # позже — ночь, и это время сна, а не книги
READING_QUIET_HOURS = 1.0    # столько никто не пишет — считаем, что он один
READING_INTERVAL_HOURS = 6.0 # не чаще; два-три захода в сутки

# Подход к полке — дело РЕЖЕ чтения, и разведены они после первого живого
# прогона (Шаг 49.2). Причина не в цене вызова, а в том, что отказ не
# оставляет следа и не может оставить: мир между заходами не меняется — та же
# полка, те же нити, то же настроение, — и персонаж, сказавший «не сегодня»,
# скажет это же через шесть часов теми же словами. Четыре одинаковых вызова
# в сутки ради ответа, известного заранее.
#
# Соблазн — помнить отказы («ты уже трижды проходил мимо»). Отвергнут: это
# полка «хочу прочитать» с другой стороны, то есть список, который никто не
# сокращает, и `0011_reading.sql` отклонил его поимённо. Сутки — не память, а
# такт: человек и правда подходит к полке примерно раз в день, а садится
# читать чаще, потому что книга уже у него в руках.
CHOICE_INTERVAL_HOURS = 24.0

# Повод рассказать о прочитанном. Ниже сна (1.5) и вровень с погодой (1.2):
# мысль на полях — не новость и не событие, но и не пустяк. Порядок между
# поводами задаётся этими числами и больше ничем.
READING_URGE = 1.2

# Двадцать часов. Дольше сна (14), потому что прочитанное вчера остаётся
# уместным дольше, чем приснившееся; короче любопытства (72), потому что
# мысль о книге, высказанная через три дня, — уже не мысль, а рецензия.
READING_TTL_HOURS = 20.0

# Обрезка предмета повода. Заметка целиком (300) вытолкнула бы из ремарки всё
# остальное; здесь нужен не пересказ мысли, а напоминание о ней.
READING_SUBJECT_LIMIT = 140

# Слова закрытия книги. Разницу несёт именно слово (`0011_reading.sql`), и
# причина брошенного дописывается к нему: «бросил» без «почему» через месяц
# ничего не объясняет.
BOOK_DONE = "дочитал"
BOOK_QUIT = "бросил"


def reading_tick(eng, edges: Edges, now: datetime, *, tz=None,
                 force: bool = False) -> str | None:
    """Один заход чтения. `None` — не время, не с чем или не вышло.

    `force` снимает ТОЛЬКО заслонки времени и оставляет всё остальное. Он для
    `agent.py --read`, то есть для человека, который хочет посмотреть на проход
    сейчас, а не ждать шести часов. Запись при этом настоящая: порция читается
    по-настоящему, позиция двигается по-настоящему, и «прогона вхолостую» тут
    нет и быть не может — прочитанное необратимо.
    """
    tz = tz or config.TZ
    if not force:
        local = now.astimezone(tz)
        if not (READING_HOUR_FROM <= local.hour < READING_HOUR_TO):
            return None

        stamps = [t for t in (eng.last_exchange(), eng.last_utterance()) if t]
        if stamps:
            idle = (now - max(stamps)).total_seconds() / 3600.0
            if 0.0 <= idle < READING_QUIET_HOURS:
                return None

        settled = eng.read_at()
        if settled is not None:
            hours = (now - settled).total_seconds() / 3600.0
            if 0.0 <= hours < READING_INTERVAL_HOURS:
                return None
            # Длинный интервал спрашивается ТОЛЬКО у того, у кого книги на
            # руках нет, и спрашивается после короткого. Порядок экономит не
            # красоту: `current_book` — лишний запрос, и стой он выше, он
            # платился бы каждую минуту дежурства. Здесь он платится в окне
            # между шестью и двадцатью четырьмя часами после прошлого захода,
            # то есть несколько раз в сутки.
            if hours < CHOICE_INTERVAL_HOURS and eng.current_book() is None:
                return None

    book = eng.current_book()
    if book is None:
        return _take_book(eng, edges, now)
    return _read_book(eng, edges, book, now)


def _take_book(eng, edges: Edges, now: datetime) -> str | None:
    """Подойти к полке. `None` — не взял, и это обычный исход.

    **Пустая полка метку НЕ двигает.** Отказ персонажа и отсутствие книг —
    разные вещи: первое его решение, второе обстоятельство. Двинь мы метку на
    пустой полке, положенная в неё книга ждала бы до шести часов, а стоит
    проверка одну короткую выборку — дешевле, чем заслонка, которая её
    экономит.
    """
    shelf = eng.shelf_state()
    if not shelf.get("free"):
        return None

    turn = eng.snapshot(now)
    got = choose_book(turn, shelf, edges.llm)

    # Метка и взятие — ОДНОЙ единицей. Иначе возможно состояние «книга взята,
    # метка не двинута», и следующий круг через минуту застал бы персонажа с
    # книгой в руках и позвал бы читать: первый вечер новой книги стал бы
    # двойным.
    with eng.unit():
        eng.set_read_at(now)
        row = eng.pick_book(got["text_path"], got["why"], now) if got else None

    if got is None:
        # Почему не взял, сказал разбор (`_parse_choice`): отказ или
        # непонятый ответ, и словами самого персонажа. Здесь — только то,
        # чего разбор не знает: сколько книг перед ним лежало.
        logging.info("полка: %s свободных книг, ни одной не взял",
                     len(shelf["free"]))
        return None
    if row is None:
        # Условный UPDATE не сработал: книгу взяли в другом месте или она уже
        # открыта. Не ошибка — гонка, и правильный исход у неё именно такой.
        logging.warning("книгу взять не вышло: %s", got["text_path"])
        return None

    logging.info("взял книгу: %s — %s", row["title"], got["why"])
    return f"взял «{row['title']}»"


def _read_book(eng, edges: Edges, book, now: datetime) -> str | None:
    """Прочитать одну порцию. `None` — не прочитал.

    **Позиция двигается только вместе с записанным конспектом, и только если
    она не уехала.** Обе половины этого правила живут в `store_pg`
    (`advance_reading`), а здесь — его следствие: неудачный вызов модели НЕ
    двигает позицию, потому что кусок, засчитанный прочитанным без чтения,
    пропадает из книги навсегда.
    """
    position = int(book["position"])
    if position >= int(book["length"]):
        # Позиция дошла до конца, а книга не закрыта: так выглядит прошлый
        # заход, упавший между сдвигом и закрытием. Чинится здесь и молча —
        # читать нечего, значит дочитал.
        with eng.unit():
            eng.set_read_at(now)
            eng.close_book(book["id"], now, BOOK_DONE)
        logging.info("дочитал: %s", book["title"])
        return f"дочитал «{book['title']}»"

    got = library.portion(book["text_path"], position)
    if got is None:
        # Файл пропал или не читается. Метка двигается, чтобы не молотить
        # диск и лог каждую минуту; о пропаже громко говорит `sync_shelf` при
        # подъёме, и чинится это руками — книгу вернуть на полку.
        with eng.unit():
            eng.set_read_at(now)
        logging.warning("читать нечем, книга недоступна: %s", book["text_path"])
        return None

    turn = eng.snapshot(now)
    conspectus = eng.conspectus_so_far(book["id"], CONSPECTUS_LIMIT)
    out = read_portion(turn, book, got, conspectus, edges.llm)
    if out is None:
        # Метка двигается и при неудаче — прецедент `traits_at`: повторять
        # тот же промах через минуту на тех же входах незачем. Позиция при
        # этом стоит, и кусок будет прочитан следующим заходом.
        with eng.unit():
            eng.set_read_at(now)
        return None

    with eng.unit():
        eng.set_read_at(now)
        reading_id = eng.advance_reading(
            book["id"], got["from_pos"], got["to_pos"], out["conspectus"], now)
        if reading_id is None:
            # Позиция уехала, пока ходили в модель. Всё или ничего: конспект
            # лёг бы не к тому куску.
            logging.warning("позиция уехала, пока он читал: %s",
                            book["title"])
            return None
        if out["note"]:
            eng.add_note(book["id"], reading_id, out["note"], now,
                         got["from_pos"])
            eng.record_urge("reading",
                            clip_text(out["note"], READING_SUBJECT_LIMIT),
                            READING_URGE, now,
                            now + timedelta(hours=READING_TTL_HOURS))
        # Бросить можно и на последней порции, и тогда это «бросил», а не
        # «дочитал»: слово должно говорить о человеке, а не о позиции.
        if out["quit"]:
            eng.close_book(book["id"], now, f"{BOOK_QUIT}: {out['quit']}")
        elif got["done"]:
            eng.close_book(book["id"], now, BOOK_DONE)

    read = got["to_pos"] - got["from_pos"]
    logging.info("прочитал %s знаков, %s: %s (%.0f%%)",
                 read, book["title"],
                 (out["conspectus"] or "конспекта нет")[:60],
                 100.0 * got["progress"])
    if out["note"]:
        logging.info("мысль на полях: %s", out["note"][:80])
    if out["quit"]:
        logging.info("бросил: %s — %s", book["title"], out["quit"])
        return f"бросил «{book['title']}»"
    if got["done"]:
        logging.info("дочитал: %s", book["title"])
        return f"дочитал «{book['title']}»"
    return f"прочитал {read} знаков «{book['title']}»"


# =============================================================================
# Черты (Шаг 42): характер пересматривается, когда биография выросла
# =============================================================================
# **Порог по накопленному, а не по времени, и это несущее решение.** Ночной
# пересчёт «раз в сутки» дал бы черты, следящие за последним разговором:
# сутки без событий — и модель всё равно перебирает список, находя, что
# поправить. Характер обязан отставать от событий, иначе он не характер, а
# настроение, у которого уже есть своя колонка.
#
# Шаг в пять воспоминаний выбран на глаз и правится на живом, как
# `RETRIEVER_COOLDOWN_HOURS`. Порядок величины при этом не произволен: сон
# пишет одно-два за ночь, разговор про жизнь — одно, значит пересчёт выходит
# раз в двое-трое суток живой жизни и заметно реже у молчащего персонажа.
#
# Нижний порог по канону отдельный от шага, хотя на чистом старте они
# совпадают. Отвечают они разному: шаг говорит «накопилось достаточно
# НОВОГО», пол — «есть из чего вообще выводить». Совпади они навсегда,
# первый же пересчёт съел бы весь запас, и второй пришёл бы вдвое позже, чем
# нужно.
TRAITS_STEP = 5
TRAITS_FLOOR = 4


def reconsider_traits(eng, edges: Edges, now: datetime) -> list[str] | None:
    """Пересмотреть черты по канону. `None` — не время или не вышло.

    Заслонки дешёвые и стоят до модели: две счётные строки против вызова,
    который в этом проходе самый дорогой после сна.

    **Метка двигается и при неудаче, а список — нет.** Пересчёт, вернувший
    меньше `TRAITS_MIN`, означает неудачный вызов, а не обеднившегося
    человека; записать такое значило бы стереть характер из-за сетевого
    сбоя. Но и повторять ту же неудачу следующей ночью на том же каноне
    незачем — поэтому знак сдвигается, и проход вернётся, когда накопится
    новый шаг.
    """
    turn = eng.snapshot(now)
    born = timeutil.parse_ts(turn.born_at or "")
    age_now = timeutil.age_years(born, now)
    if born is None or age_now is None:
        return None

    since = eng.traits_at()
    if eng.memories_since(since) < TRAITS_STEP:
        return None

    canon = eng.all_memories()
    if len(canon) < TRAITS_FLOOR:
        return None

    found = reflect_traits(turn, canon, born, age_now, edges.llm)
    traits = [item["name"] for item in found]
    with eng.unit():
        # Пустое НЕ пишется поверх нажитого: `set_traits` кладёт то, что
        # дали, и решение «не трогать список» принимается здесь, повтором
        # прежнего значения. Метка при этом уезжает новая — в том и смысл.
        eng.set_traits(traits or list(turn.traits), now)
        # История (Шаг 56) пишется той же единицей, что и список: черта,
        # попавшая в `agent.traits` без строки в истории, — ровно тот разъезд,
        # от которого `set_traits` держит список и метку одним `UPDATE`.
        # Пустой пересчёт историю не трогает, как не трогает и список.
        if traits:
            eng.record_traits(found, now)
    if not traits:
        return None

    for item in found:
        logging.info("черта: %s — %s", item["name"], item["reason"])
    logging.info("черты пересмотрены: %s -> %s",
                 turn.traits_line or "(не было)", ", ".join(traits))
    return traits


# =============================================================================
# Любопытство (Шаг 41): повод, возникающий при закрытии разговора
# =============================================================================
# Величины те же по устройству, что у погоды и сна, и разведены по той же
# логике: сила говорит «насколько тянет», срок — «когда перестанет быть
# новостью».
#
# Сила чуть выше порога, а не крупная. Любопытство — не новость: если за
# трое суток нашёлся повод сильнее, спросить можно и потом, а вот перебить им
# сон или перемену за окном было бы неверно. Порядок между поводами задаётся
# здесь и нигде больше.
CURIOSITY_URGE = 1.1
# Трое суток, а не часы. Погода протухает за часы, потому что перестаёт быть
# правдой; вопрос правдой быть не перестаёт — он перестаёт быть уместным, и
# происходит это медленно.
CURIOSITY_TTL_HOURS = 72.0


def record_curiosity(eng, edges: Edges, turn, buffer: dict,
                     now: datetime) -> str | None:
    """Вычитать из закрывшегося разговора незакрытый вопрос и завести повод.

    Зовётся из `agent.finish_session`, то есть один раз на сессию. Решение
    (пороги, срок) живёт здесь, а не в демоне: демон закрывает сессию, а
    что при этом становится поводом — вопрос хода.

    Пишет СВОЕЙ единицей, а не чужой. Соблазн — влезть в ту же транзакцию,
    что и `close_session`: закрытие и повод происходят в один момент. Но
    между ними стоит вызов модели, а транзакция не переживает вызов модели —
    то же правило, по которому отклонён `inbox.claimed_at` в `0001_base`.
    Цена названа: упади процесс между ними, сессия закроется без повода, и
    повторить его будет неоткуда. Терпимо ровно потому, что повод —
    единственное, что здесь теряется, и он не память.
    """
    open_subjects = [
        r["subject"] for r in eng.open_impulses()
        if r["kind"] == "curiosity" and r["subject"]
    ]
    subject = linger(turn, buffer, edges.llm, open_subjects)
    if not subject:
        return None
    with eng.unit():
        eng.record_urge("curiosity", subject, CURIOSITY_URGE, now,
                        now + timedelta(hours=CURIOSITY_TTL_HOURS))
    logging.info("осталось незакрытым: %s", subject)
    return subject


# =============================================================================
# Генезис (Шаг 36.3): рождение как ПЛАН, отдельно от записи
# =============================================================================
# Порядок здесь жёсткий и однонаправленный: тяга -> место -> имя.
#
# Место раньше имени, потому что мода имён когортная И местная: один и тот
# же город с разницей в двадцать лет даёт разные списки, а один и тот же год
# в двухстах километрах — другие. Спроси имя раньше разрешённого места, и
# получишь человека ниоткуда, то есть ровно ту усреднённость, от которой
# тяга и уводит.
#
# Модель зовётся ДВАЖДЫ и оба раза служебно: она перечисляет населённые
# пункты и имена — то, что знает как справочник. Ничего о характере, судьбе
# и занятиях у неё не спрашивается ни здесь, ни вообще: это обязано нарасти
# воспоминаниями, а не выпасть анкетой на первом вызове.

# Проверяется РАССТОЯНИЕ ОТ ДОМА, и только оно.
#
# До первого живого прогона сверялось удаление от целевой точки — то есть
# заодно и направление. Прогон показал, почему так нельзя: модель
# направление игнорирует. Просили 733 км к югу от Брянска — получили восемь
# посёлков Карачаево-Черкесии, это юго-восток и 1170 км. Фильтр по цели
# отсекал ВСЁ и сваливал генезис в `same_place` на каждом прогоне, а
# проверка, не пропускающая ничего, — не проверка, а выключатель.
#
# Расстояние модель при этом берёт: промах в 1.6 раза на плече в семьсот
# километров. Полоса поэтому задана в РАЗАХ, а не в километрах — ошибка у
# неё пропорциональная, а не абсолютная.
#
# Нижняя граница нужна не меньше верхней: без неё уцелел бы пригород, и
# «родился в другом месте» выродилось бы в «родился в соседнем районе».
#
# Направление остаётся в промпте подсказкой. Цена названа: место рождения
# ложится туда, куда модель охотнее вспоминает, и её предпочтения частично
# возвращаются. Разнообразие держат требование разброса в промпте и тяга
# среди уцелевших, а не проверка.
BIRTHPLACE_NEAR = 0.5
BIRTHPLACE_FAR = 2.5


@dataclass(frozen=True)
class Plan:
    """Что БЫЛО БЫ записано. Сам не пишет и записи не касается.

    План отделён от записи не ради сухого прогона — наоборот, сухой прогон
    возможен потому, что план отделён. Рождение необратимо, и посмотреть на
    него до записи надо иметь возможность без правки кода.

    Три последних поля к записи отношения не имеют: это то, что предлагали,
    что уцелело и по какой полосе. Они существуют для сухого прогона, и без
    них он показывал бы результат, не показывая, из чего тот получился, —
    то есть не давал бы поправить промпт, а только принять или отвергнуть
    человека.
    """

    birth: object                      # genesis.Birth
    birthplace: str | None
    birthplace_lat: float | None
    birthplace_lon: float | None
    name: str | None
    reason: str | None
    proposed: list[str] = field(default_factory=list)
    survived: list[tuple] = field(default_factory=list)
    band_km: tuple = (0.0, 0.0)
    names: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Есть ли что записывать. Без имени рождения не состоялось."""
        return bool(self.name)


def plan_genesis(place: dict, first_text: str, edges: Edges,
                 now: datetime) -> Plan:
    """Вытянуть рождение и достроить его моделью. НЕ ПИШЕТ НИЧЕГО.

    Сеть здесь есть, и это не нарушение правила «нечистое живёт на краю»:
    функция сама является краем — она зовётся один раз за жизнь персонажа и
    снаружи хода. Чистая половина вынесена в `genesis` и проверяется эталоном
    без сети и без модели.
    """
    birth = genesis.draw(place.get("label"), place.get("lat"),
                         place.get("lon"), first_text, now)
    digest = genesis.seed(place.get("label"), first_text, now)

    # Считается ДО цикла: полоса — свойство тяги, а не кандидата, и внутри
    # цикла пересчитывалась бы восемь раз, создавая впечатление, будто
    # зависит от того, что назвала модель.
    near = birth.distance_km * BIRTHPLACE_NEAR
    far = birth.distance_km * BIRTHPLACE_FAR

    proposed: list[str] = []
    survived: list[tuple] = []
    birthplace = place.get("label")
    bp_lat, bp_lon = place.get("lat"), place.get("lon")

    # Без координат дома сверять нечем: полоса меряется от него. Место
    # рождения тогда совпадает с местом жизни — та же деградация, что у
    # `sky` и `web`, а не отказ.
    can_check = place.get("lat") is not None and place.get("lon") is not None

    if not birth.same_place and can_check:
        proposed = propose_birthplaces(place.get("label") or "", birth, edges.llm)
        for label in proposed:
            try:
                found = outside.geocode(label, edges.http)
            except Exception as err:
                logging.warning("genesis: геокодер молчит на %r: %s", label, err)
                continue
            if not found:
                continue
            off = genesis.distance_km(place["lat"], place["lon"],
                                      found["lat"], found["lon"])
            if near <= off <= far:
                survived.append((found["label"], round(off, 1),
                                 found["lat"], found["lon"]))

        picked = genesis.choose(digest, 3, survived)
        if picked:
            birthplace, _, bp_lat, bp_lon = picked
        else:
            # Ни одно не уцелело: сеть молчит, модель насочиняла или назвала
            # не ту сторону света. Родился там же, где живёт, — законный
            # исход, а не отказ, и второго вызова модели он не стоит:
            # переспрашивание тянуло бы к тому же ответу, промпт тот же.
            logging.info("genesis: место не подтвердилось "
                         "(полоса %.0f–%.0f км) — родился там же", near, far)

    names = propose_names(birth, birthplace or "", edges.llm)
    chosen = genesis.choose(digest, 4, names)

    return Plan(
        birth=birth,
        birthplace=birthplace,
        birthplace_lat=bp_lat,
        birthplace_lon=bp_lon,
        name=chosen["name"] if chosen else None,
        reason=chosen["reason"] if chosen else None,
        proposed=proposed,
        survived=survived,
        band_km=(round(near, 1), round(far, 1)),
        names=names,
    )