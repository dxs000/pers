"""Чем заняться: он сам решает, как провести время (Шаг 57).

## Что заменяется

До этого шага занятия шли по расписаниям: чтение с 9 до 24 раз в шесть
часов, эссе с 19 до 24 раз в сутки, новости раз в три часа. Каждый проход
спрашивал модель «что читать», «о чём писать», «куда глянуть» — но никто не
спрашивал, хочет ли он сейчас читать вообще. Человек, которого тревожит
переезд, не садится за книгу по будильнику.

Теперь есть одно решение: раз в `AGENDA_INTERVAL_HOURS`, пока он не спит и
не разговаривает, он смотрит на себя (побуждения, нити, настроение, что уже
сделал сегодня) и выбирает одно дело. Прежние проходы чтения, эссе и новостей
остаются исполнителями: их зовут с `force=True`, который снимает только
заслонки времени.

## Что остаётся за кодом

Физиология, не воля: ночь (там работает сон), тишина (во время разговора он
занят разговором), интервал между решениями. И дела, которые не выбирают:
вечерний итог дня, сон, пересмотр черт и побуждений, обещания — они
случаются с человеком, а не делаются им.

## Действия

Три прежних (читать, писать, новости) и пять новых:

- **покопаться** — полезть в сеть разбираться в том, что не отпускает. Запрос
  его, вывод его; найденное не пишется в биографию, а остаётся в журнале и
  доезжает до вечернего итога как дело дня;
- **вспоминать** — биография растёт по его воле, а не только по вопросу
  собеседника или ночью. Вспомнившееся идёт через ту же сверку, что у
  биографа и сна, и пишется как `inferred`;
- **задуматься** — мысль уходит: что было бы, если бы. Это галлюцинация по
  назначению, и поэтому она НЕ пишется в биографию: фантазия, принятая за
  прожитое, — ровно та порча, от которой сон помечен «(снилось)». Живёт в
  журнале, может стать поводом заговорить;
- **написать ему** — просто захотелось. Заводит повод `reach`; сказать или
  нет, решает `background_tick` со своими заслонками;
- **ничего** — законный и частый выбор, и тоже пишется в журнал.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta

import config
import timeutil
import web
from openai import OpenAIError

log = logging.getLogger("agenda")

AGENDA_HOUR_FROM = 8         # раньше — он ещё не проснулся
AGENDA_HOUR_TO = 23          # позже — ночь, и там работает сон
AGENDA_QUIET_HOURS = 1.0     # столько никто не пишет — считаем, что он один
AGENDA_INTERVAL_HOURS = 2.0  # между решениями; шесть-семь решений за день
AGENDA_DRIVES_LIMIT = 5
AGENDA_TODAY_LIMIT = 8
AGENDA_BEFORE_LIMIT = 5

ABOUT_LIMIT = 120
WHY_LIMIT = 200
OUTCOME_LIMIT = 300
SUBJECT_LIMIT = 200

# Сила поводов, которые заводят его собственные дела. Все выше порога
# инициативы (1.0), но ниже сна (1.5): своё найденное и своя мысль — повод, но
# не такой, как то, что приснилось.
PURSUIT_URGE = 1.1
PURSUIT_TTL_HOURS = 24.0
DAYDREAM_URGE = 1.1
DAYDREAM_TTL_HOURS = 8.0
# «Захотелось написать» — сильнее прочих: это не повод, а само желание. Но
# и протухает быстрее всех: через шесть часов это уже не то желание.
# Прибавка веса, когда он НАРОЧНО вернулся к воспоминанию. Втрое больше, чем
# у всплывшего к слову (`store_pg.RECALL_BUMP`): то, к чему возвращаются сами,
# и становится тем, что человек о себе помнит лучше всего.
RETURN_BUMP = 0.3
# С какой похожести вспомнившееся считается тем же, что уже записано (Шаг 58).
# Откалибровано на живой биографии (Шаг 58.2): пересказ строки своими словами
# дал 0.83 (режим doc), самые похожие из РАЗНЫХ воспоминаний — не выше 0.63.
# Порог ближе к верхнему краю, потому что ошибки несимметричны: пропущенный
# дубль дойдёт до сверки, как раньше, а ложное совпадение выбросит настоящее
# новое воспоминание.
SAME_FLOOR = float(os.getenv("EMBED_SAME_FLOOR", "0.78"))
REACH_URGE = 1.3
REACH_TTL_HOURS = 6.0

ACTIONS = ("read", "write", "news", "explore", "recall", "daydream", "reach", "rest")

# Слова меню. Решает ОН, поэтому меню на «ты» и словами, а не кодами.
ACTION_WORDS = {
    "read": "читать",
    "write": "писать своё",
    "news": "глянуть новости",
    "explore": "покопаться",
    "recall": "вспоминать",
    "daydream": "задуматься",
    "reach": "написать ему",
    "rest": "ничего",
}


def _norm(value) -> str:
    return " ".join(str(value or "").split()).strip(" «»\"'.").lower().replace("ё", "е")


_ACTION_BY_WORD = {_norm(w): a for a, w in ACTION_WORDS.items()}

# Прошедшее время — для «что ты уже делал сегодня» и для вечернего итога.
ACTION_PAST = {
    "read": "читал",
    "write": "писал своё",
    "news": "смотрел новости",
    "explore": "копался",
    "recall": "вспоминал",
    "daydream": "задумался",
    "reach": "хотел написать ему",
    "rest": "ничего не делал",
}


# =============================================================================
# Проход
# =============================================================================

def agenda_tick(eng, edges, now: datetime, *, tz=None,
                force: bool = False) -> str | None:
    """Одно решение и одно дело. `None` — заслонка или модель не ответила.

    Решение пишется в журнал ДО исполнения, отдельной единицей. Упади
    исполнитель — строка «решил читать, потому что...» останется правдой: он
    решил. Исход дописывается второй единицей.
    """
    tz = tz or config.TZ
    local = now.astimezone(tz)
    if not force:
        if not (AGENDA_HOUR_FROM <= local.hour < AGENDA_HOUR_TO):
            return None
        stamps = [t for t in (eng.last_exchange(), eng.last_utterance()) if t]
        if stamps:
            idle = (now - max(stamps)).total_seconds() / 3600.0
            if 0.0 <= idle < AGENDA_QUIET_HOURS:
                return None
        settled = eng.agenda_at()
        if settled is not None:
            hours = (now - settled).total_seconds() / 3600.0
            if 0.0 <= hours < AGENDA_INTERVAL_HOURS:
                return None

    turn = eng.snapshot(now)
    drives = eng.open_drives(now, AGENDA_DRIVES_LIMIT)
    options = available(eng, edges)
    today = eng.pursuits_between(_day_start(local), now)
    before = eng.recent_pursuits(_day_start(local), AGENDA_BEFORE_LIMIT)

    prompt = build_prompt(turn, local, drives, options, today, before,
                          eng.last_exchange())
    raw = _ask(edges.llm, prompt, full=True)
    if raw is None:
        # Сбой сети — не решение, в журнал не пишется. Метка двигается, иначе
        # лежащая сеть звала бы модель каждую минуту.
        with eng.unit():
            eng.set_agenda_at(now)
        return None

    choice = parse_choice(raw, options, drives)
    with eng.unit():
        eng.set_agenda_at(now)
        pid = eng.add_pursuit(now, choice["action"], choice["why"],
                              choice["about"], choice["drive_id"])
    log.info("решил: %s%s — %s", ACTION_WORDS[choice["action"]],
             f" ({choice['about']})" if choice["about"] else "", choice["why"])

    outcome = _perform(eng, edges, turn, choice, drives, now, tz)
    with eng.unit():
        eng.set_pursuit_outcome(pid, _clip(outcome, OUTCOME_LIMIT) if outcome else None)
    return f"{choice['action']}: {outcome or '—'}"


def _day_start(local: datetime) -> datetime:
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def available(eng, edges) -> dict[str, str]:
    """Что можно сделать сейчас -> строка меню. Недоступное в меню не
    попадает вовсе: предложить «писать своё», когда писать не о чем, значит
    получить пустой проход эссе и потраченное решение."""
    import store_essay

    opts: dict[str, str] = {}
    book = eng.current_book()
    if book is not None:
        opts["read"] = f"вернуться к «{book['title']}»"
    else:
        free = eng.shelf_state().get("free") or []
        if free:
            opts["read"] = f"подойти к полке: там {len(free)} непрочитанных"

    essay = store_essay.current_essay(eng.conn)
    if essay is not None:
        opts["write"] = f"сесть за «{essay['title']}»"
    elif eng.untold_notes(1):
        opts["write"] = "с полей книг накопились мысли, которые никуда не записаны"

    if edges.search_key:
        opts["news"] = "посмотреть, что в мире"
        opts["explore"] = ("полезть в сеть разбираться в том, что не отпускает; "
                           "about - что именно наберёшь, 2-6 слов")
    opts["recall"] = ("вернуться мыслями в какое-то время, место, к человеку; "
                      "about - куда именно")
    opts["daydream"] = ("уйти мыслью: что было бы, если бы; как могло бы "
                        "сложиться; about - о чём")
    opts["reach"] = ("написать ему не из-за повода, а потому что хочется; "
                     "about - о чём")
    opts["rest"] = "быть там, где ты есть, и делать то, что люди делают между делами"
    return opts


# =============================================================================
# Промпт решения
# =============================================================================

def _render_pursuit(p: dict, tz) -> str:
    at = timeutil.parse_ts(p["at"])
    when = at.astimezone(tz).strftime("%H:%M") if at else "?"
    line = f"- {when} {ACTION_PAST.get(p['action'], p['action'])}"
    if p.get("about"):
        line += f" ({p['about']})"
    if p.get("outcome"):
        line += f": {_clip(p['outcome'], 120)}"
    return line


def build_prompt(turn, local: datetime, drives: list[dict], options: dict,
                 today: list[dict], before: list[dict], last_exchange) -> str:
    import drives as drives_mod
    from mind import _render_reading, _render_silence

    born = timeutil.parse_ts(turn.born_at or "")
    age = timeutil.age_years(born, local)
    who = f"Тебя зовут {turn.name}"
    if age is not None:
        who += f", тебе {age} {timeutil.years_word(age)}"
    if turn.place_label:
        who += f", ты в {turn.place_label}"
    parts = [
        "Сейчас у тебя свободное время, и ты сам решаешь, чем его занять. "
        "Это не задание: никто не ждёт от тебя пользы.\n",
        who + ".",
    ]
    if turn.traits:
        parts.append(f"Каким ты стал: {', '.join(turn.traits)}.")
    parts.append(f"Сейчас {timeutil.render_now(local)}. Настроение - {turn.mood}.")
    silence = _render_silence(last_exchange, local)
    if silence:
        parts.append(silence)
    parts.append("")

    if drives:
        parts.append(
            "Что в тебе сейчас живёт (номер - чтобы сослаться):\n"
            + "\n".join(f"[{i}] {drives_mod.KIND_WORDS_YOU[d['kind']]}: {d['text']}"
                        for i, d in enumerate(drives, 1)) + "\n")
    if turn.threads:
        parts.append("Что у тебя не закончено:\n"
                     + "\n".join(f"- {t['text']}" for t in turn.threads) + "\n")
    reading = _render_reading(turn.reading)
    if reading:
        parts.append(reading + "\n")

    tz = local.tzinfo
    if today:
        parts.append("Что ты уже делал сегодня:\n"
                     + "\n".join(_render_pursuit(p, tz) for p in today[-AGENDA_TODAY_LIMIT:])
                     + "\n")
    else:
        parts.append("Сегодня ты по своей воле ещё ничего не делал.\n")
    if before:
        parts.append("А до этого:\n"
                     + "\n".join(_render_pursuit(p, tz) for p in before) + "\n")

    parts.append("Что можно сейчас:\n" + "\n".join(
        f"- {ACTION_WORDS[a]} - {desc}" for a, desc in options.items()) + "\n")

    parts.append(
        "Выбирай так, как выбрал бы ты, а не как было бы правильно. Люди не "
        "делают одно и то же весь день; но и не мечутся. «ничего» - обычный "
        "выбор, и частый. То, что в тебе живёт, может тянуть, а может и нет: "
        "иногда человек делает ровно то, что отвлекает от главного.\n"
    )
    parts.append(
        "Формат - ТОЛЬКО JSON, без пояснений:\n"
        '{"do": "вспоминать", "about": "...", "why": "...", "drive": 1}\n'
        "do - одно из слов меню выше; about - о чём, если у дела есть предмет, "
        "иначе null; why - почему сейчас это, одной фразой, своими словами; "
        "drive - номер того, что в тебе живёт и толкнуло к этому, или null."
    )
    return "\n".join(parts)


def parse_choice(raw: str, options: dict, drives: list[dict]) -> dict:
    """Ответ -> решение. Непонятое или недоступное становится «ничего»: не
    сделать ничего — единственный исход, который не может навредить."""
    data = _parse_json(raw) or {}
    word = _norm(data.get("do"))
    action = _ACTION_BY_WORD.get(word)
    if action is None:
        for w, a in _ACTION_BY_WORD.items():
            if word and (word.startswith(w) or w.startswith(word)):
                action = a
                break
    why = _clip(data.get("why"), WHY_LIMIT)
    about = _clip(data.get("about"), ABOUT_LIMIT) or None
    if about and about.lower() in ("null", "none", "нет"):
        about = None
    if action not in options:
        # Причина записывается его же, но с тем, что вышло на деле: строка
        # «ничего — потому что хочется записать про двор» врала бы о нём.
        if action is not None:
            log.info("решение: «%s» сейчас недоступно — ничего", word)
            why = f"хотел {ACTION_WORDS[action]}, но сейчас не с чем" + (
                f" ({why})" if why else "")
        action, about = "rest", None
        why = why or "не решил"
    drive_id = None
    try:
        n = int(data.get("drive"))
        if 1 <= n <= len(drives):
            drive_id = drives[n - 1]["id"]
    except (TypeError, ValueError):
        pass
    return {"action": action, "why": why or "просто так", "about": about,
            "drive_id": drive_id}


# =============================================================================
# Исполнители
# =============================================================================

def _perform(eng, edges, turn, choice, drives, now, tz) -> str | None:
    action = choice["action"]
    try:
        if action == "read":
            import cycle
            return cycle.reading_tick(eng, edges, now, tz=tz, force=True)
        if action == "write":
            import essay as essay_mod
            return essay_mod.essay_tick(eng, edges, now, tz=tz, force=True)
        if action == "news":
            import news as news_mod
            return news_mod.news_tick(eng, edges, now, tz=tz, force=True)
        if action == "explore":
            return _explore(eng, edges, turn, choice, drives, now, tz)
        if action == "recall":
            return _recall(eng, edges, turn, choice, drives, now, tz)
        if action == "daydream":
            return _daydream(eng, edges, turn, choice, drives, now, tz)
        if action == "reach":
            subject = choice["about"] or choice["why"]
            with eng.unit():
                eng.record_urge("reach", _clip(subject, SUBJECT_LIMIT), REACH_URGE,
                                now, now + timedelta(hours=REACH_TTL_HOURS))
            return None
    except Exception as err:
        log.warning("дело «%s» не состоялось: %s", ACTION_WORDS[action], err)
        return None
    return None


def _drive_line(choice, drives) -> str:
    import drives as drives_mod
    for d in drives:
        if d["id"] == choice.get("drive_id"):
            return f"Что толкнуло: {drives_mod.KIND_WORDS_YOU[d['kind']]}: {d['text']}.\n"
    return ""


def build_explore_prompt(turn, choice, drives, query: str, found: list[dict]) -> str:
    items = "\n".join(f"- {f.get('title')}: {f.get('snippet')}" for f in found)
    return (
        f"Ты полез разбираться. Набрал: «{query}».\n"
        f"Почему: {choice['why']}.\n"
        f"{_drive_line(choice, drives)}\n"
        f"Вот что нашлось:\n{items}\n\n"
        "Что ты из этого взял? Не пересказ найденного, а то, что это значит "
        "для тебя: подтвердилось, удивило, разочаровало, ничего не дало. Одна-"
        "две фразы, от первого лица. Ничего не взял - так и скажи, это частый "
        "исход.\n\n"
        "Формат - ТОЛЬКО JSON:\n"
        '{"took": "...", "tell": false}\n'
        "took - что взял, или null; tell - хочется ли рассказать об этом ему. "
        "Чаще нет: не всё найденное - повод писать человеку."
    )


def _explore(eng, edges, turn, choice, drives, now, tz) -> str | None:
    query = choice["about"] or _drive_text(choice, drives)
    if not query:
        return "не знал, что искать"
    found = web.search(query, edges.search, edges.search_key, max_results=3, now=now)
    if not found:
        return "ничего толкового не нашёл"
    data = _parse_json(_ask(edges.llm, build_explore_prompt(turn, choice, drives,
                                                            query, found),
                            full=True) or "") or {}
    took = _clip(data.get("took"), OUTCOME_LIMIT)
    if not took or took.lower() in ("null", "none"):
        return "ничего не взял"
    if data.get("tell"):
        with eng.unit():
            eng.record_urge("pursuit", _clip(took, SUBJECT_LIMIT), PURSUIT_URGE,
                            now, now + timedelta(hours=PURSUIT_TTL_HOURS))
    return took


def _drive_text(choice, drives) -> str | None:
    for d in drives:
        if d["id"] == choice.get("drive_id"):
            return d["text"]
    return None


def build_recall_prompt(turn, choice, drives, canon, born, age_now: int) -> str:
    from drives import render_canon_numbered
    about = choice["about"] or "что придёт само"
    return (
        f"Ты вспоминаешь: {about}.\n"
        f"Почему сейчас: {choice['why']}.\n"
        f"{_drive_line(choice, drives)}\n"
        "Вся твоя жизнь, как она записана:\n"
        f"{render_canon_numbered(canon, born)}\n\n"
        "Что вспомнилось? Одна сцена, которой в записанном ЕЩЁ НЕТ: не "
        "пересказ известного, а то, что всплыло рядом с ним. Конкретное: "
        "место, слово, вещь, погода. Не значительное - люди чаще помнят "
        "мелочи, чем повороты.\n\n"
        "Оно не должно противоречить ничему записанному. Не вспомнилось - это "
        "обычный исход: то, что ты запишешь, останется в твоей жизни навсегда.\n\n"
        "Бывает и так, что мысль приходит к тому, что уже записано выше, и "
        "дальше не идёт. Это тоже вспоминание, не пустое: тогда назови номер "
        "этой строки, и новое не придумывай.\n\n"
        "Формат - ТОЛЬКО JSON, одно из трёх:\n"
        '{"recalled": {"age": 9, "precision": "era", "text": "..."}}\n'
        '{"returned": 12}\n'
        '{"recalled": null}\n'
        f"age - сколько тебе было, целое от 0 до {age_now}; precision - 'era', "
        "'year', 'month' или 'day'; text - от первого лица, 1-2 фразы; "
        "returned - номер # строки, к которой вернулась мысль."
    )


def _recall(eng, edges, turn, choice, drives, now, tz) -> str | None:
    from mind import VERDICT_KNOWN, VERDICT_WRITE, check_memory
    from snapshot import iso

    born = timeutil.parse_ts(turn.born_at or "")
    age_now = timeutil.age_years(born, now)
    if born is None or age_now is None:
        return None
    canon = eng.all_memories()
    data = _parse_json(_ask(edges.llm, build_recall_prompt(turn, choice, drives,
                                                           canon, born, age_now),
                            full=True) or "") or {}
    # Вернулся к записанному (Шаг 57.1). Первая редакция такого исхода не
    # знала: модель описывала известное своими словами, сверка отвечала «уже
    # есть», и заход пропадал целиком. Но нарочно вернуться к воспоминанию —
    # то, от чего оно крепнет, и вес это отражает.
    returned = _ints_one(data.get("returned"))
    by_id = {m["id"]: m for m in canon}
    if returned in by_id:
        with eng.unit():
            eng.touch_recall([returned], now, RETURN_BUMP)
        return f"вернулся к #{returned}: {_clip(by_id[returned]['text'], 200)}"

    rec = data.get("recalled")
    if not isinstance(rec, dict):
        return "не вспомнилось"
    try:
        age = int(rec.get("age"))
    except (TypeError, ValueError):
        return "не вспомнилось"
    precision = rec.get("precision")
    text = _clip(rec.get("text"), OUTCOME_LIMIT)
    if not (0 <= age <= age_now) or precision not in ("era", "year", "month", "day") \
            or not text:
        return "не вспомнилось"
    # Дубль ловится вектором ДО сверки (Шаг 58). Мысль, пересказавшая
    # записанное своими словами, — это возврат к нему, и он засчитывается как
    # возврат: вес строки растёт, модель сверки не зовётся. Порог высокий
    # намеренно: ложное совпадение выбросило бы настоящее новое воспоминание,
    # а пропущенное совпадение всего лишь дойдёт до сверки, как раньше.
    import embed as embed_mod
    vec = embed_mod.embed(text, "doc", edges)
    if vec is not None:
        # Сны в поиске дубля не участвуют (Шаг 58.2): вспомнилось настоящее,
        # похожее на сон, — это не «возврат ко сну», а событие, которое сон
        # переиначил. Ему дорога на сверку, как любому новому.
        near = eng.similar_memories(vec, embed_mod.DOC_MODEL, 1,
                                    skip_sources=("dream",))
        if near:
            log.info("вспоминание: ближе всего #%s (%.3f)", near[0]["id"], near[0]["sim"])
        if near and near[0]["sim"] >= SAME_FLOOR:
            with eng.unit():
                eng.touch_recall([near[0]["id"]], now, RETURN_BUMP)
            return f"вернулся к #{near[0]['id']}: {_clip(near[0]['text'], 200)}"

    happened_at = born + timedelta(days=age * timeutil.DAYS_IN_YEAR)
    verdict = check_memory({"age": age, "precision": precision, "text": text,
                            "happened_at": iso(happened_at)}, canon, born, edges.llm)
    if verdict != VERDICT_WRITE:
        # Текст остаётся в журнале: о чём он думал, видно и так, даже если в
        # биографию это не легло. «ничего нового» стирало бы сам заход.
        log.info("вспомнилось, но не записано (%s): %s", verdict, text[:60])
        if verdict == VERDICT_KNOWN:
            return f"вспомнилось уже известное: {text}"
        return f"вспомнилось не так, как было, - не записал: {text}"
    with eng.unit():
        eng.add_memory(happened_at, precision, text, "inferred", now=now)
    return text


def build_daydream_prompt(turn, choice, drives) -> str:
    about = choice["about"] or "куда уйдёт мысль"
    return (
        f"Ты задумался: {about}.\n"
        f"{_drive_line(choice, drives)}\n"
        "Мысль уходит, как уходят мысли: что было бы, если бы; как могло бы "
        "сложиться; где бы ты сейчас был. Это не воспоминание и не план - это "
        "то, что проходит в голове. Оно может быть неправдой, неправдоподобным, "
        "смешным или стыдным: в твою жизнь оно не записывается.\n\n"
        "Формат - ТОЛЬКО JSON:\n"
        '{"thought": "...", "tell": false}\n'
        "thought - куда ушла мысль, от первого лица, 2-3 фразы; tell - хочется "
        "ли об этом написать ему. Чаще нет."
    )


def _daydream(eng, edges, turn, choice, drives, now, tz) -> str | None:
    data = _parse_json(_ask(edges.llm, build_daydream_prompt(turn, choice, drives),
                            full=True) or "") or {}
    thought = _clip(data.get("thought"), OUTCOME_LIMIT)
    if not thought:
        return None
    if data.get("tell"):
        with eng.unit():
            eng.record_urge("daydream", _clip(thought, SUBJECT_LIMIT), DAYDREAM_URGE,
                            now, now + timedelta(hours=DAYDREAM_TTL_HOURS))
    return thought


# =============================================================================
# Модель
# =============================================================================

def _ask(client, prompt: str, full: bool = False) -> str | None:
    kwargs = {} if full else {"extra_body": {"thinking": {"type": "disabled"}}}
    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL if full else config.DEEPSEEK_MODEL_LIGHT,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
    except OpenAIError as err:
        log.warning("чем заняться: запрос упал: %s", err)
        return None
    return response.choices[0].message.content or ""


def _parse_json(raw: str):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _ints_one(value) -> int | None:
    try:
        return int(str(value).lstrip("#"))
    except (TypeError, ValueError):
        return None


def _clip(value, limit: int) -> str:
    s = " ".join(str(value or "").split()).strip()
    return s[:limit].rstrip()

