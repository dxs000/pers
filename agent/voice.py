"""Голос: сколько и как часто он говорит сам (Шаг 60).

## Что было до шага

Пять независимых глушителей, каждый правый по отдельности, вместе давали
угрюмое молчание:

- тишина созревала 17 часов (с шестого часа по 1.4 в сутки до порога 1.0),
  а если разговора не было ни разу, не созревала никогда;
- любая реплика умножала ВСЕ несказанные поводы на 0.3 — замечание о погоде
  хоронило сон (1.5 -> 0.45) и желание написать (1.3 -> 0.39);
- пауза 2 часа и потолок 6 реплик в сутки — одни на всех персонажей;
- промпты «чем заняться», находки и мысли уговаривали молчать («чаще нет»);
- всё, что он делал сам, оставалось в журнале, невидимое в разговоре.

Константы задавали характер, которого у персонажа могло и не быть:
замкнутый и болтливый молчали одинаково.

## Что теперь

Пауза, суточный потолок и скорость тишины выводятся из двух величин:

- **тяга** (`agent.talk`, 0..1) — черта. Выводится из его черт и их
  оснований отдельным проходом после каждого их пересмотра. Автор не
  решает, болтлив ли персонаж: это решает то, что с ним было;
- **отклик** — доля его реплик по своей воле, на которые ответили в
  пределах `REPLY_WINDOW_HOURS`. Со сглаживанием Лапласа: на чистом старте
  0.5, а не 0 и не 1. Отвечают — он смелеет, молчат — затихает.

И отдельно **серия**: сколько раз подряд он сказал в пустоту после
последней реплики человека. Каждая следующая удваивает паузу. Это не
привычка, а сейчас: человек, написавший трижды без ответа, четвёртый раз
ждёт.

Всё вычисляется на лету из `messages` и `agent`: хранится только тяга,
потому что она — вывод модели, а не арифметика.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import config
from openai import OpenAIError

log = logging.getLogger("voice")

TALK_DEFAULT = 0.5
REPLY_WINDOW_HOURS = 3.0     # ответ позже — уже не ответ на реплику, а новый разговор
REPLY_SAMPLE = 10            # сколько последних его реплик смотреть

# Края. Середина (x = 0.5) даёт паузу около 1.9 ч, 8 реплик в сутки и порог
# тишины примерно через 10 часов — против прежних 2 ч, 6 и 17 часов.
COOLDOWN_MAX_HOURS = 3.0
COOLDOWN_MIN_HOURS = 0.75
PER_DAY_MIN = 3
PER_DAY_MAX = 12
SILENCE_RATE_MIN = 1.0       # в сутки: порог через 24 ч тишины
SILENCE_RATE_MAX = 4.0       # в сутки: порог через 6 ч
SILENCE_START_HOURS = 2.0    # раньше — пауза в разговоре, а не тишина
STREAK_FREE = 1              # столько реплик без ответа не удлиняют паузу


@dataclass(frozen=True)
class Voice:
    talk: float
    reply: float
    x: float
    cooldown_hours: float
    per_day: int
    silence_rate: float
    streak: int
    answered: int
    total: int


def _lerp(lo: float, hi: float, t: float) -> float:
    return lo + (hi - lo) * max(0.0, min(1.0, t))


def voice(eng, now: datetime) -> Voice:
    """Как он сейчас говорит. Без модели и без записи — два запроса."""
    t = eng.talk().get("talk")
    talk = TALK_DEFAULT if t is None else float(t)
    stats = eng.reply_stats(now, REPLY_WINDOW_HOURS, REPLY_SAMPLE)
    reply = (stats["answered"] + 1) / (stats["total"] + 2)
    x = 0.5 * talk + 0.5 * reply
    streak = int(stats["streak"])
    cooldown = _lerp(COOLDOWN_MAX_HOURS, COOLDOWN_MIN_HOURS, x)
    cooldown *= 2 ** max(0, streak - STREAK_FREE)
    return Voice(
        talk=talk, reply=reply, x=x,
        cooldown_hours=cooldown,
        per_day=int(round(_lerp(PER_DAY_MIN, PER_DAY_MAX, x))),
        silence_rate=_lerp(SILENCE_RATE_MIN, SILENCE_RATE_MAX, x),
        streak=streak, answered=stats["answered"], total=stats["total"],
    )


def describe(v: Voice) -> str:
    return (f"тяга {v.talk:.2f}, отклик {v.answered}/{v.total} -> {v.reply:.2f}, "
            f"итог {v.x:.2f}: пауза {v.cooldown_hours:.2f} ч, до {v.per_day} в сутки, "
            f"тишина {v.silence_rate:.2f}/сут, без ответа подряд {v.streak}")


# =============================================================================
# Тяга: проход после пересмотра черт
# =============================================================================

def talk_tick(eng, edges, now: datetime) -> float | None:
    """Вывести тягу говорить из черт. `None` — не время или не вышло.

    Зовётся, когда черты пересмотрены позже, чем выведена тяга. Отдельным
    проходом, а не строкой в промпте черт: черты — список слов с основаниями,
    и число в нём было бы чужим полем, которое модель начала бы подгонять под
    слова. Здесь наоборот: слова уже есть, и число из них выводится.
    """
    got = eng.talk()
    traits_at, at = got.get("traits_at"), got.get("at")
    if traits_at is None or (at is not None and at >= traits_at):
        return None
    turn = eng.snapshot(now)
    if not turn.traits:
        return None
    raw = _ask(edges.llm, build_prompt(turn, eng.trait_reasons()))
    value, why = parse(raw) if raw is not None else (None, None)
    with eng.unit():
        eng.set_talk(value, why, now)
    if value is None:
        return None
    log.info("тяга говорить: %.2f — %s", value, why)
    return value


def build_prompt(turn, reasons: dict) -> str:
    lines = []
    for t in turn.traits:
        r = reasons.get(t)
        lines.append(f"- {t}" + (f" ({r})" if r else ""))
    return (
        "Ты - служебный проход голос. Задача: по характеру человека решить, "
        "насколько его тянет говорить с другими первым.\n\n"
        f"Его зовут {turn.name}.\n"
        "Каким он стал (и из чего это видно):\n" + "\n".join(lines) + "\n\n"
        "Речь не о том, умеет ли он говорить, а о том, пишет ли он первым: "
        "делится ли тем, что увидел и подумал, или держит при себе, пока не "
        "спросят. Бывают люди, которые пишут по три раза на дню, и бывают те, "
        "из кого слова не вытянешь. Оба нормальны.\n\n"
        "Ответь одной строкой: число от 0 до 1, вертикальная черта, почему - "
        "одной фразой. 0 - первым не пишет почти никогда, 1 - пишет всё, что "
        "приходит в голову.\n\n"
        "0.35 | держит мысли при себе, пока не спросят"
    )


def parse(raw: str) -> tuple[float | None, str | None]:
    for line in (raw or "").splitlines():
        head, _, tail = line.partition("|")
        head = head.strip().strip("*").replace(",", ".")
        try:
            value = float(head)
        except ValueError:
            continue
        if 0.0 <= value <= 1.0:
            why = " ".join(tail.split())[:200] or None
            return round(value, 2), why
    log.warning("голос: не разобрал %r", (raw or "")[:120])
    return None, None


def _ask(client, prompt: str) -> str | None:
    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL_LIGHT,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        )
    except OpenAIError as err:
        log.warning("голос: запрос упал: %s", err)
        return None
    return response.choices[0].message.content or ""
