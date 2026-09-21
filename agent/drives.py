"""Побуждения: чего он хочет, чего боится, во что верит (Шаг 56).

## Зачем

Черты отвечают на вопрос «какой он», нити — «чем занят». Ни то ни другое не
отвечает, куда его тянет, и потому до этого шага направление ему задавал
код: ленту новостей выбирала константа, книгу — полка, сон — расписание.
Побуждения — первый слой, из которого направление может браться у НЕГО. Этот
шаг их только заводит и показывает разговору, сну, дню и новостям. Выбор
действий по ним — следующий шаг.

## Устройство

Проход служебный и говорит о персонаже в третьем лице, как черты и сон: он
РЕШАЕТ ЗА него, что в нём выросло, а не спрашивает. Вход — вся биография с
номерами строк, черты с их основаниями, открытые нити и уже записанные
побуждения. Выход — что появилось, что подтвердилось, что кончилось.

**Без номера строки побуждение не пишется.** Это держит писатель
(`_parse`) и хранилище (`store_character.add_drive`), а не только промпт.
Характер, выросший из событий, должен быть проверяем: по каждому «боится»
видно, из какого события это взялось.

**Сон может оставить страх, желание или вопрос, но не убеждение.** Черты сон
не трогает вовсе (Шаг 42: приснившееся — не событие его жизни). Побуждения
мягче: люди просыпаются с тревогой, которой вчера не было, и она правда
остаётся. А вот верить во что-то потому, что приснилось, — это уже не сон, а
бред, и такой кандидат отвергается разбором.

**Затухание — механика, а не решение модели.** Промпт просит закрывать
только то, что по записанному кончилось. Всё, что жизнь просто перестала
подтверждать, гаснет само (`drive_score`, полураспад 45 суток) и уходит из
промпта разговора, не закрываясь: вернись повод — оно всплывёт снова.

## Заслонки

Как у черт — по росту биографии (`DRIVES_STEP` новых строк с прошлого
прохода), плюс не чаще раза в сутки и не во время разговора. Метка ставится
и при пустом исходе: пустой ответ — обычный («хотят люди годами одного и
того же»), и спрашивать о нём каждую минуту незачем.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import config
import timeutil
from mind import _memory_mark, _memory_when
from openai import OpenAIError

log = logging.getLogger("drives")

DRIVES_STEP = 3              # новых строк биографии с прошлого прохода
DRIVES_FLOOR = 4             # меньше — судить не по чему
DRIVES_INTERVAL_HOURS = 20.0
DRIVES_QUIET_HOURS = 1.0
DRIVES_OPENED_LIMIT = 2      # новых за проход
DRIVES_PROMPT_LIMIT = 8      # сколько открытых показать проходу
DRIVE_TEXT_LIMIT = 160
DRIVES_PURSUITS_LIMIT = 8
DRIVE_WHY_LIMIT = 200
DRIVE_CLOSED_WHY_LIMIT = 40

# Род словом в промпте и обратно. Слова — те, какими о человеке говорят, а не
# ярлыки таблицы: модель, которой дали «want/fear», пишет анкету.
KIND_WORDS = {
    "want": "хочет",
    "fear": "боится",
    "belief": "верит",
    "question": "не даёт покоя",
}
_KIND_BY_WORD = {w: k for k, w in KIND_WORDS.items()}
# То же во втором лице — для промпта самого персонажа.
KIND_WORDS_YOU = {
    "want": "хочешь",
    "fear": "боишься",
    "belief": "веришь",
    "question": "не даёт покоя",
}


# =============================================================================
# Проход
# =============================================================================

def drives_tick(eng, edges, now: datetime, *, force: bool = False) -> list[str] | None:
    """Один проход. `None` — заслонка; список — проход состоялся (пустой —
    ничего не изменилось, и это обычный исход)."""
    turn = eng.snapshot(now)
    born = timeutil.parse_ts(turn.born_at or "")
    age_now = timeutil.age_years(born, now)
    if born is None or age_now is None:
        return None

    at = eng.drives_at()
    if not force:
        stamps = [t for t in (eng.last_exchange(), eng.last_utterance()) if t]
        if stamps:
            idle = (now - max(stamps)).total_seconds() / 3600.0
            if 0.0 <= idle < DRIVES_QUIET_HOURS:
                return None
        if at is not None:
            hours = (now - at).total_seconds() / 3600.0
            if 0.0 <= hours < DRIVES_INTERVAL_HOURS:
                return None
        if eng.memories_since(at) < DRIVES_STEP:
            return None

    canon = eng.all_memories()
    if len(canon) < DRIVES_FLOOR:
        return None

    current = eng.open_drives(now, DRIVES_PROMPT_LIMIT)
    # Ничего не делавшие заходы («ничего») сюда не едут: их много, и список из
    # десяти «ничего не делал» заслонил бы те три дела, по которым видно человека.
    pursuits = [p for p in eng.recent_pursuits(now, DRIVES_PURSUITS_LIMIT * 3)
                if p["action"] != "rest"][-DRIVES_PURSUITS_LIMIT:]
    prompt = build_prompt(turn, canon, born, age_now, current,
                          eng.trait_reasons(), pursuits)
    raw = _ask(edges.llm, prompt)
    result = parse(raw, canon, current) if raw is not None else None

    changes: list[str] = []
    with eng.unit():
        eng.set_drives_at(now)
        if result:
            for d in result["opened"]:
                eng.add_drive(d["kind"], d["text"], d["why"], d["from"], now)
                changes.append(f"+ {KIND_WORDS[d['kind']]}: {d['text']}")
            if result["stronger"]:
                eng.strengthen_drives(result["stronger"], now)
                changes += [f"↑ {_text_of(current, i)}" for i in result["stronger"]]
            if result["closed"]:
                eng.close_drives(result["closed"], now)
                changes += [f"× {_text_of(current, i)} ({why})"
                            for i, why in result["closed"]]
    if raw is None:
        return None
    for line in changes:
        log.info("побуждение: %s", line)
    if not changes:
        log.info("побуждения: без перемен")
    return changes


def _text_of(current: list[dict], drive_id: int) -> str:
    for d in current:
        if d["id"] == drive_id:
            return d["text"]
    return f"#{drive_id}"


# =============================================================================
# Промпт
# =============================================================================

def render_canon_numbered(canon: list[dict], born) -> str:
    """Биография с номерами строк. Номер — `memories.id`, а не порядковый:
    он же уедет в `drive_sources`, и сверять его есть с чем."""
    if not canon:
        return "(пока ничего не записано)"
    return "\n".join(
        f"[#{m['id']}] [{_memory_when(m, born)}] {_memory_mark(m)}{m['text']}"
        for m in canon
    )


def _render_traits(traits, reasons: dict) -> str:
    if not traits:
        return "Черт пока не записано."
    lines = []
    for t in traits:
        why = reasons.get(t)
        lines.append(f"- {t} ({why})" if why else f"- {t}")
    return "Каким он стал:\n" + "\n".join(lines)


def _render_current(current: list[dict]) -> str:
    if not current:
        return ("Что уже записано о том, чего он хочет и боится: пока ничего. "
                "Это первый раз, когда на него смотрят так.")
    lines = [f"[{i}] {KIND_WORDS[d['kind']]}: {d['text']} (потому что {d['basis']})"
             for i, d in enumerate(current, 1)]
    return ("Что уже записано о том, чего он хочет и боится "
            "(номер - чтобы сослаться):\n" + "\n".join(lines))


def build_prompt(turn, canon, born, age_now: int, current: list[dict],
                 reasons: dict, pursuits: list[dict] | None = None) -> str:
    kinds = ", ".join(f"«{w}»" for w in KIND_WORDS.values())
    parts = [
        "Ты - служебный проход побуждения. Задача: увидеть, чего этот человек "
        "хочет, чего боится, во что верит и какой вопрос не даёт ему покоя - "
        "по тому, что с ним было.\n",
        f"Персонажа зовут {turn.name}. Ему {age_now} {timeutil.years_word(age_now)}.",
        _render_traits(turn.traits, reasons),
        f"Настроение сейчас: {turn.mood}.\n",
        "Его жизнь, как она записана (номер - чтобы сослаться на строку):",
        render_canon_numbered(canon, born) + "\n",
    ]
    if turn.threads:
        parts.append("Что у него сейчас не закончено:\n"
                     + "\n".join(f"- {t['text']}" for t in turn.threads) + "\n")
    if pursuits:
        # Шаг 57: что он делал по своей воле. Проход побуждений видит поведение,
        # а не только события: неделя, в которую он трижды лез разбираться в
        # одном и том же, говорит о нём больше строки биографии.
        import agenda as agenda_mod
        parts.append("Что он делал по своей воле последние дни:\n" + "\n".join(
            f"- {agenda_mod.ACTION_PAST[p['action']]}"
            + (f" ({p['about']})" if p.get("about") else "")
            + f" - потому что {p['why']}" for p in pursuits) + "\n")
    parts.append(_render_current(current) + "\n")
    parts.append(
        "Побуждение - не цель из анкеты («развиваться», «быть счастливым»), а "
        "то, что он сказал бы себе сам, если бы был честен. Оно вырастает из "
        "событий: одно и то же событие у разных людей рождает разное, и "
        "задача - увидеть, что оно родило у ЭТОГО.\n"
    )
    parts.append(
        "Правила:\n"
        " - каждое новое опирается на строки жизни выше - по номерам #. Без "
        "номера оно не запишется;\n"
        " - приснившееся (помечено «(снилось)») может оставить страх, желание "
        "или вопрос - сны так и делают. Но не веру: во что-то верят не потому, "
        "что приснилось;\n"
        f" - не больше {DRIVES_OPENED_LIMIT} новых, и чаще всего ни одного: "
        "хотят и боятся люди годами одного и того же;\n"
        " - то, что жизнь снова подтвердила, назови номером в stronger;\n"
        " - закрывай, только если по записанному видно, что кончилось: "
        "сбылось, прошло, разуверился, ответил себе. Не видно - не трогай: "
        "неподтверждённое гаснет само;\n"
        " - хотеть несовместимого можно, люди так и живут. Противоречить "
        "записанному нельзя.\n"
    )
    parts.append(
        "Формат - ТОЛЬКО JSON, без пояснений:\n"
        '{"opened": [{"kind": "боится", "text": "...", "why": "...", '
        '"from": [3, 5]}], "stronger": [1], "closed": [{"n": 2, "why": "прошло"}]}\n'
    )
    parts.append(
        "Про поля:\n"
        f" - kind: одно из {kinds};\n"
        " - text: одной фразой, как он сказал бы себе сам, но без «я хочу» и "
        "«я боюсь» в начале - род уже назван;\n"
        " - why: одной фразой - как он это связал, а не пересказ событий;\n"
        " - from: номера строк жизни (#);\n"
        " - stronger и closed: номера из списка записанного выше. Пустые "
        "списки - законный и частый ответ."
    )
    return "\n".join(parts)


# =============================================================================
# Модель и разбор
# =============================================================================

def _ask(client, prompt: str) -> str | None:
    # Модель ПОЛНАЯ, как у черт и сна: здесь выносится суждение о человеке,
    # и лёгкая модель на нём выдаёт анкету.
    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
    except OpenAIError as err:
        log.warning("побуждения: запрос упал: %s", err)
        return None
    return response.choices[0].message.content or ""


def _one_line(value, limit: int) -> str:
    s = " ".join(str(value or "").split()).strip()
    return s[:limit].rstrip()


def _ints(value) -> list[int]:
    out = []
    for v in value if isinstance(value, list) else []:
        try:
            out.append(int(str(v).lstrip("#")))
        except (TypeError, ValueError):
            continue
    return out


def parse(raw: str, canon: list[dict], current: list[dict]) -> dict | None:
    """Ответ -> {opened, stronger, closed} с уже проверенными ссылками.

    Битый кандидат выбрасывается по одному, а не роняет ответ: как у
    `_parse_names`, деградация по записи, а не по всему.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        log.warning("побуждения: ответ не JSON")
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        log.warning("побуждения: ответ не JSON")
        return None

    by_id = {m["id"]: m for m in canon}
    by_n = {i: d["id"] for i, d in enumerate(current, 1)}

    opened = []
    for item in data.get("opened") or []:
        if not isinstance(item, dict):
            continue
        kind = _KIND_BY_WORD.get(_one_line(item.get("kind"), 20).lower())
        body = _one_line(item.get("text"), DRIVE_TEXT_LIMIT)
        why = _one_line(item.get("why"), DRIVE_WHY_LIMIT)
        sources = [i for i in _ints(item.get("from")) if i in by_id]
        if not kind or not body or not why:
            continue
        if not sources:
            log.info("побуждения: без основания, не пишу — %s", body)
            continue
        if kind == "belief" and all(by_id[i].get("source") == "dream" for i in sources):
            log.info("побуждения: вера из сна, не пишу — %s", body)
            continue
        opened.append({"kind": kind, "text": body, "why": why,
                       "from": sorted(set(sources))})
        if len(opened) >= DRIVES_OPENED_LIMIT:
            break

    closed = []
    for item in data.get("closed") or []:
        if not isinstance(item, dict):
            continue
        n = _ints([item.get("n")])
        why = _one_line(item.get("why"), DRIVE_CLOSED_WHY_LIMIT)
        if n and n[0] in by_n and why:
            closed.append((by_n[n[0]], why))
    closed_ids = {i for i, _ in closed}

    stronger = sorted({by_n[n] for n in _ints(data.get("stronger"))
                       if n in by_n and by_n[n] not in closed_ids})

    return {"opened": opened, "stronger": stronger, "closed": closed}


# =============================================================================
# Рендер для чужих промптов
# =============================================================================

def render_for_self(drives: list[dict]) -> str | None:
    """Блок системного промпта. Во втором лице: это он сам о себе."""
    if not drives:
        return None
    lines = [f"- {KIND_WORDS_YOU[d['kind']]}: {d['text']}" for d in drives]
    return ("Что в тебе сейчас живёт (это не тема для разговора, а то, откуда "
            "ты смотришь; вслух - только если к месту):\n" + "\n".join(lines))


def render_about(drives: list[dict], title: str) -> str | None:
    """Блок служебного промпта: день, сон, новости. В третьем лице."""
    if not drives:
        return None
    lines = [f"- {KIND_WORDS[d['kind']]}: {d['text']}" for d in drives]
    return f"{title}\n" + "\n".join(lines)
