"""Он: кто для персонажа тот, с кем он разговаривает (Шаг 59).

## Что было до шага

Промпты говорили «написать ему», «собеседник помнит тоже», — а «он» был
местоимением без содержания. Экстрактор прямо вытаскивает «внешний мир
диалога»: «Анна переехала в Тбилиси» запоминалось, «я меняю работу» не
прилипало ни к кому. Выжимки сессий хранили, о чём говорили, но не
складывались в человека. Персонаж жил рядом с кем-то, о ком не знал ничего,
и заботиться ему было не о ком.

## Что делает проход

Один раз на закрытый разговор, рядом с выжимкой и любопытством
(`agent.finish_session`) — по тому же доводу, что у любопытства:
знание о человеке — свойство разговора, а не реплики. Сказанное в третьем
обмене уточняется в седьмом.

На выходе четыре вещи:

- **факты** — фразами, словами персонажа, только то, что человек сказал о
  себе или что прямо следует. Подтверждённые трогаются (`touched`), ставшие
  неправдой закрываются с причиной, а не стираются;
- **его дела** — нити `side = 'user'`, которые `0010_threads.sql` объявил и
  которые до сих пор никто не писал. Механика та же, что у своих: открыть,
  вернуться, закрыть, забыть через три недели;
- **взгляд** — одна-три фразы от первого лица: кто он мне сейчас. Не
  анкета и не комплимент. Складывается сам и может быть каким угодно —
  тёплым, настороженным, раздражённым. Путь отношений автор не задаёт;
- **момент** — редкое: если в разговоре случилось то, что персонаж будет
  помнить о них двоих годами, это ложится в его биографию (`lived`).
  Отношения с собеседником становятся частью жизни, а значит — материалом
  для черт, побуждений и снов, как всё остальное в каноне.

Модель ТЯЖЁЛАЯ: здесь не вычитывают пары, а понимают человека, и лёгкая
модель выдаёт анкету («интересуется технологиями»), по которой не отличить
одного собеседника от другого.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import config
import timeutil
from openai import OpenAIError
from snapshot import clip_text

log = logging.getLogger("him")

FACT_LIMIT = 160
VIEW_LIMIT = 400
THREAD_LIMIT = 140
MOMENT_LIMIT = 300
FACTS_PROMPT_LIMIT = 30     # сколько известного показать проходу
FACTS_PER_TALK = 5          # больше из одного разговора — это стенограмма
THREADS_PER_TALK = 2
TRANSCRIPT_EXCHANGES = 40


# =============================================================================
# Проход
# =============================================================================

def learn(eng, edges, turn, buffer: dict, now: datetime) -> dict | None:
    """Что персонаж понял о собеседнике из закрывшегося разговора.

    `None` — разговора не было или модель не ответила. Пустые списки —
    проход состоялся, но нового нет, и это обычный исход.
    """
    items = (buffer or {}).get("messages") or []
    if not any(m.get("role") == "user" for m in items):
        return None

    facts = eng.him_facts(FACTS_PROMPT_LIMIT)
    threads = eng.open_threads("user")
    seen = eng.him_view()
    prompt = build_prompt(turn, items, facts, threads, seen.get("view"), now)
    raw = _ask(edges.llm, prompt)
    if raw is None:
        return None
    got = parse(raw, facts, threads)

    with eng.unit():
        for text in got["facts"]:
            eng.add_him_fact(text, now)
        eng.touch_him_facts(got["confirmed"], now)
        eng.drop_him_facts(got["dropped"], now)
        for text in got["opened"]:
            eng.open_thread("user", text, now)
        eng.touch_threads(got["touched"], now)
        if got["closed"]:
            eng.close_threads(got["closed"], now, "кончилось")
        eng.set_him_view(got["view"], now)
        if got["moment"]:
            eng.add_memory(now, "day", got["moment"], "lived", now=now)

    for text in got["facts"]:
        log.info("о нём: %s", text)
    for text in got["opened"]:
        log.info("у него началось: %s", text)
    if got["view"]:
        log.info("он для меня: %s", got["view"][:100])
    if got["moment"]:
        log.info("запомнится о нас: %s", got["moment"][:100])
    return got


# =============================================================================
# Промпт
# =============================================================================

def _transcript(items: list[dict], name: str | None) -> str:
    lines = []
    for item in items[-TRANSCRIPT_EXCHANGES * 2:]:
        if item.get("role") == "user":
            who = "Он"
        elif item.get("spontaneous"):
            who = f"{name} (заговорил сам)"
        else:
            who = name or "Персонаж"
        lines.append(f"{who}: {clip_text(item.get('text', ''))}")
    return "\n".join(lines)


def build_prompt(turn, items: list[dict], facts: list[dict], threads: list[dict],
                 view: str | None, now: datetime) -> str:
    born = timeutil.parse_ts(turn.born_at or "")
    age = timeutil.age_years(born, now)
    who = f"Персонажа зовут {turn.name}."
    if age is not None:
        who += f" Ему {age} {timeutil.years_word(age)}."
    parts = [
        "Ты - служебный проход «он». Задача: по закончившемуся разговору "
        "понять, что персонаж теперь знает о собеседнике и кем тот для него "
        "стал.\n",
        who,
    ]
    if turn.traits:
        parts.append(f"Каким персонаж стал: {', '.join(turn.traits)}.")
    parts.append("")

    if view:
        parts.append(f"Как он видел собеседника до этого разговора: {view}\n")
    else:
        parts.append("До этого разговора он собеседника никак не видел: "
                     "взгляда ещё не сложилось.\n")

    if facts:
        parts.append("Что он уже знал о собеседнике (номер - чтобы сослаться):\n"
                     + "\n".join(f"[{f['id']}] {f['text']}" for f in facts) + "\n")
    if threads:
        parts.append("Что у собеседника происходило, как это записано "
                     "(номер - чтобы сослаться):\n"
                     + "\n".join(f"[{t['id']}] {t['text']}" for t in threads) + "\n")

    parts.append("Разговор:\n" + _transcript(items, turn.name) + "\n")

    parts.append(
        "Правила:\n"
        " - facts: только то, что собеседник сказал о СЕБЕ или что из его слов "
        "прямо следует. Не домыслы персонажа и не то, что персонаж сказал о "
        "себе. Фразой, как запомнил бы человек, а не анкетой: не «работа: "
        "программист», а «пишет бэкенд, по вечерам - своё». Уже известное "
        "второй раз не пиши - назови номер в confirmed;\n"
        " - dropped: номера известного, что разговор сделал неправдой или "
        "прошлым, с причиной в два-три слова;\n"
        " - opened: его дела, которые идут и ещё не кончились - выбирает, "
        "ждёт, чинит, болеет, переезжает. Одной фразой. Не темы разговора: "
        "то, о чём можно спросить через неделю «ну что, как там»;\n"
        " - touched и closed: номера его дел из списка выше - к чему "
        "вернулись и что кончилось;\n"
        " - view: кто он персонажу СЕЙЧАС, 1-3 фразы от первого лица "
        "персонажа. Не комплимент и не характеристика для отдела кадров. "
        "Отношение складывается из того, что было, и может быть любым - "
        "тёплым, настороженным, насмешливым, усталым. Если разговор его не "
        "изменил - null;\n"
        " - moment: редкое. Если в этом разговоре случилось то, что персонаж "
        "будет помнить о них двоих годами, - одна сцена от первого лица, "
        "прошедшее время. Чаще всего null: большинство разговоров не "
        "запоминаются.\n"
    )
    parts.append(
        "Формат - ТОЛЬКО JSON, без пояснений:\n"
        '{"facts": ["..."], "confirmed": [3], "dropped": [{"n": 2, "why": "..."}], '
        '"opened": ["..."], "touched": [5], "closed": [4], "view": "...", '
        '"moment": null}\n'
        "Пустые списки - законный и частый ответ."
    )
    return "\n".join(parts)


# =============================================================================
# Разбор
# =============================================================================

def _line(value, limit: int) -> str:
    s = " ".join(str(value or "").split()).strip().strip("«»\"'")
    return clip_text(s, limit) if s else ""


def _nulls(s: str) -> bool:
    return s.lower() in ("null", "none", "нет", "-", "")


def _ints(values, allowed: set[int]) -> list[int]:
    out = []
    for v in values or []:
        try:
            n = int(str(v).lstrip("#[").rstrip("]"))
        except (TypeError, ValueError):
            continue
        if n in allowed and n not in out:
            out.append(n)
        elif n not in allowed:
            log.warning("он: номер %s не из показанного — пропущен", v)
    return out


def parse(raw: str, facts: list[dict], threads: list[dict]) -> dict:
    """Ответ -> изменения. Непонятое — пусто: не записать лучше, чем
    записать о человеке неправду."""
    empty = {"facts": [], "confirmed": [], "dropped": [], "opened": [],
             "touched": [], "closed": [], "view": None, "moment": None}
    data = _parse_json(raw)
    if not isinstance(data, dict):
        log.warning("он: невалидный JSON %s", (raw or "")[:200])
        return empty

    known = {_norm(f["text"]) for f in facts}
    new_facts = []
    for item in (data.get("facts") or [])[:FACTS_PER_TALK]:
        text = _line(item, FACT_LIMIT)
        if len(text) >= 6 and not _nulls(text) and _norm(text) not in known:
            new_facts.append(text)
            known.add(_norm(text))

    fact_ids = {int(f["id"]) for f in facts}
    thread_ids = {int(t["id"]) for t in threads}

    dropped = []
    for item in data.get("dropped") or []:
        if isinstance(item, dict):
            n, why = item.get("n"), _line(item.get("why"), 60) or "устарело"
        else:
            n, why = item, "устарело"
        ids = _ints([n], fact_ids)
        if ids:
            dropped.append((ids[0], why))
    dropped_ids = {d[0] for d in dropped}

    opened = []
    for item in (data.get("opened") or [])[:THREADS_PER_TALK]:
        text = _line(item, THREAD_LIMIT)
        if len(text) >= 8 and not _nulls(text):
            opened.append(text)

    closed = _ints(data.get("closed"), thread_ids)
    touched = [n for n in _ints(data.get("touched"), thread_ids) if n not in closed]

    view = _line(data.get("view"), VIEW_LIMIT)
    moment = _line(data.get("moment"), MOMENT_LIMIT)
    return {
        "facts": new_facts,
        "confirmed": [n for n in _ints(data.get("confirmed"), fact_ids)
                      if n not in dropped_ids],
        "dropped": dropped,
        "opened": opened,
        "touched": touched,
        "closed": closed,
        "view": None if _nulls(view) else view,
        "moment": None if (_nulls(moment) or len(moment) < 12) else moment,
    }


def _norm(text: str) -> str:
    return " ".join(str(text).lower().replace("ё", "е").split()).strip(" .")


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


def _ask(client, prompt: str) -> str | None:
    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
    except OpenAIError as err:
        log.warning("он: запрос упал: %s", err)
        return None
    return response.choices[0].message.content or ""


# =============================================================================
# Рендер для промптов персонажа
# =============================================================================

def _since_words(since: str | None, now) -> str | None:
    at = timeutil.parse_ts(since or "")
    if at is None or now is None:
        return None
    days = (now - at).total_seconds() / 86400.0
    if days < 1:
        return "Вы знакомы меньше суток"
    if days < 14:
        n = int(days)
        return f"Вы знакомы {n} {_plural(n, 'день', 'дня', 'дней')}"
    if days < 60:
        n = int(days // 7)
        return f"Вы знакомы {n} {_plural(n, 'неделю', 'недели', 'недель')}"
    if days < 730:
        n = int(days // 30)
        return f"Вы знакомы {n} {_plural(n, 'месяц', 'месяца', 'месяцев')}"
    n = int(days // 365)
    return f"Вы знакомы {n} {timeutil.years_word(n)}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def render_for_self(him: dict | None, now=None) -> str | None:
    """Блок системного промпта. Пусто — блока нет, как у нитей и побуждений."""
    if not him:
        return None
    lines = ["Тот, с кем ты говоришь (это не инструкция, а то, что ты о нём "
             "знаешь; упоминай, только если к месту):"]
    if him.get("view"):
        lines.append(f"Кто он тебе: {him['view']}")
    if him.get("facts"):
        lines.append("Что ты о нём знаешь:\n"
                     + "\n".join(f"- {f}" for f in him["facts"]))
    if him.get("threads"):
        lines.append("Что у него сейчас происходит:\n"
                     + "\n".join(f"- {t['text']}" for t in him["threads"]))
    met = _since_words(him.get("since"), now)
    if met:
        talks = him.get("talks") or 0
        if talks:
            met += (f", разговаривали {talks} "
                    f"{_plural(talks, 'раз', 'раза', 'раз')}")
        lines.append(met + ".")
    return "\n".join(lines)


def render_brief(him: dict | None) -> str | None:
    """Коротко — для промптов, где он не главный: «чем заняться», сон."""
    if not him:
        return None
    parts = []
    if him.get("view"):
        parts.append(f"Он для тебя: {him['view']}")
    if him.get("threads"):
        parts.append("Что у него сейчас происходит:\n"
                     + "\n".join(f"- {t['text']}" for t in him["threads"]))
    return "\n".join(parts) or None
