"""Ход — функция, а не тело цикла (Шаг 27)."""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import config
import genesis
import outside
import sky as sky_mod
import timeutil
import web
from mind import (VERDICT_WRITE, build_system_prompt, check_memory,
                  decide_query, dream, dream_subject, extract_memories,
                  extract_objects, propose_birthplaces, propose_names,
                  reflect_mood, reflect_self, speak_first, weather_family)
from snapshot import SESSION_GAP_HOURS, iso
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
    turn = eng.snapshot(now_dt)
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
        remembered = _biograph(eng, edges, turn, pair, now)
        logging.info("digest биограф: %.1fs", time.monotonic() - t)
        with eng.unit():
            if new_mood:
                eng.set_mood(new_mood)
            if new_assertions:
                eng.merge_self_assertions(new_assertions, now)
            for cand in candidates:
                eng.upsert_object(cand, now)
            for happened_at, precision, text in remembered:
                eng.add_memory(happened_at, precision, text, "told")
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
        eng.add_memory(now, "day", seen["dream"], "dream", DREAM_WEIGHT)
        if recalled and verdict == VERDICT_WRITE:
            eng.add_memory(happened_at, recalled["precision"],
                           recalled["text"], "inferred")
        eng.record_urge("dream", dream_subject(seen["dream"]), DREAM_URGE, now,
                        now + timedelta(hours=DREAM_TTL_HOURS))

    logging.info("приснилось: %s", seen["dream"][:80])
    return seen["dream"]


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