"""Проход эссе: захотел — пишет вечер, не захотел — молчит.

Не ветка cycle.py: второе дело держим отдельно, пока не видно общего каркаса
(это прямо сказано в 0011_reading.sql). Почты нет. Тема с экрана нет.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import config
import outbox
from openai import OpenAIError

log = logging.getLogger("essay")

ESSAY_HOUR_FROM = 19
ESSAY_HOUR_TO = 24
ESSAY_QUIET_HOURS = 1.0
ESSAY_INTERVAL_HOURS = 20.0
ESSAY_URGE = 1.2
ESSAY_TTL_HOURS = 72.0
CHUNK_CHARS = 2500


def essay_tick(eng, edges, now: datetime, *, tz=None, force: bool = False):
    """Один заход. None — не время или не захотел."""
    tz = tz or config.TZ
    if not force:
        local = now.astimezone(tz)
        if not (ESSAY_HOUR_FROM <= local.hour < ESSAY_HOUR_TO):
            return None
        stamps = [t for t in (eng.last_exchange(), eng.last_utterance()) if t]
        if stamps:
            idle = (now - max(stamps)).total_seconds() / 3600.0
            if 0.0 <= idle < ESSAY_QUIET_HOURS:
                return None
        settled = eng.essay_at()
        if settled is not None:
            hours = (now - settled).total_seconds() / 3600.0
            if 0.0 <= hours < ESSAY_INTERVAL_HOURS:
                return None

    current = eng.current_essay()
    if current is None:
        return _begin(eng, edges, now)
    return _continue(eng, edges, current, now)


def _begin(eng, edges, now: datetime):
    notes = eng.untold_notes(8)
    if not notes:
        with eng.unit():
            eng.set_essay_at(now)
        return None

    turn = eng.snapshot(now)
    prompt = _want_prompt(turn, notes)
    data = _ask_json(edges.llm, prompt, light=True)
    if not data or not data.get("write"):
        with eng.unit():
            eng.set_essay_at(now)
        return None

    title = (data.get("title") or "").strip()
    why = (data.get("why") or "").strip()
    if not title or not why:
        with eng.unit():
            eng.set_essay_at(now)
        return None

    path = outbox.path_for(title)
    with eng.unit():
        rel = str(path.relative_to(outbox.root()) if _under(path, outbox.root()) else path)
        row = eng.open_essay(title, why, rel, now)
        eng.set_essay_at(now)
        if row is None:
            return None
    log.info("начал эссе: %s (%s)", title, why)
    return f"начал эссе «{title}»"


def _continue(eng, edges, essay, now: datetime):
    turn = eng.snapshot(now)
    past = eng.passages_so_far(essay["id"], 8)
    prompt = _write_prompt(turn, essay, past)
    data = _ask_json(edges.llm, prompt, light=False)
    if not data:
        with eng.unit():
            eng.set_essay_at(now)
        return None

    quit = data.get("quit")
    chunk = (data.get("text") or "").strip()
    path = _resolve(essay["file_path"])

    with eng.unit():
        eng.set_essay_at(now)
        if chunk:
            written = outbox.append(
                path, chunk,
                header=f"# {essay['title']}\n\n_{essay['why']}_",
            )
            if written:
                eng.add_passage(essay["id"], chunk, now)
        if quit:
            eng.close_essay(essay["id"], now, str(quit))
            eng.record_urge(
                "essay", essay["title"], ESSAY_URGE, now,
                now + timedelta(hours=ESSAY_TTL_HOURS))
        elif data.get("done"):
            eng.close_essay(essay["id"], now, "написал")
            eng.record_urge(
                "essay", essay["title"], ESSAY_URGE, now,
                now + timedelta(hours=ESSAY_TTL_HOURS))

    if quit:
        log.info("бросил эссе: %s — %s", essay["title"], quit)
        return f"бросил эссе «{essay['title']}»"
    if data.get("done") and chunk:
        log.info("написал эссе: %s", essay["title"])
        return f"написал эссе «{essay['title']}»"
    if chunk:
        log.info("дописал эссе: %s (%s знаков)", essay["title"], len(chunk))
        return f"дописал эссе «{essay['title']}»"
    return None


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve(stored: str) -> Path:
    p = Path(stored)
    if p.is_absolute():
        return p
    return outbox.root() / p


def _ask_json(client, prompt: str, *, light: bool):
    model = config.DEEPSEEK_MODEL_LIGHT if light else config.DEEPSEEK_MODEL
    kwargs = dict(model=model, messages=[{"role": "user", "content": prompt}])
    if light:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    try:
        response = client.chat.completions.create(**kwargs)
    except OpenAIError as err:
        log.warning("эссе: запрос упал: %s", err)
        return None
    raw = response.choices[0].message.content or ""
    return _parse_json(raw)


def _parse_json(raw: str):
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        log.warning("эссе: ответ не JSON")
        return None


def _want_prompt(turn, notes) -> str:
    who = turn.name or "персонаж"
    traits = ", ".join(turn.traits) if turn.traits else "ещё без черт"
    lines = "\n".join(f"- {n['text']}" for n in notes)
    return (
        f"Ты — служебный проход эссе. Задача: решить, есть ли СЕЙЧАС "
        f"у {who} что сказать длинным текстом — не репликой.\n\n"
        f"Его зовут {who}. Каким он стал: {traits}.\n"
        f"Настроение: {turn.mood}.\n\n"
        f"Нерассказанные мысли с полей книг:\n{lines}\n\n"
        f"Это не задание и не тема снаружи. Чаще правильный ответ — не писать: "
        f"мысль, из которой не складывается текст, лучше оставить мыслью.\n"
        f"Не бери «историю», «философию», «литературу» как жанр. Бери одну "
        f"свою зацепку, если она тянет.\n\n"
        f"Формат — ТОЛЬКО JSON:\n"
        f'{{"write": false, "title": null, "why": null, "seed": null}}\n'
        f"Если пишет: write=true, title — как он сам назвал бы, why — одной "
        f"фразой зачем, seed — о чём первая фраза (можно null).\n"
    )


def _write_prompt(turn, essay, past) -> str:
    who = turn.name or "персонаж"
    traits = ", ".join(turn.traits) if turn.traits else ""
    prev = "\n".join(f"- {c}" for c in past) if past else "(ещё ничего не написано)"
    return (
        f"Ты пишешь эссе. Это твой вечер и твой текст: тему никто не задавал.\n\n"
        f"Тебя зовут {who}. Черты: {traits}. Настроение: {turn.mood}.\n"
        f"Эссе: «{essay['title']}». Зачем взялся: {essay['why']}.\n\n"
        f"Что уже сказано (конспекты вечеров):\n{prev}\n\n"
        f"Напиши СЛЕДУЮЩИЙ кусок, до {CHUNK_CHARS} знаков, связный, от себя, "
        f"без заголовков и без списка. Не пересказывай конспекты.\n\n"
        f"Формат — ТОЛЬКО JSON:\n"
        f'{{"text": "...", "done": false, "quit": null}}\n'
        f" - text: кусок или пустая строка, если сегодня не пишется;\n"
        f" - done: true — текст закончен;\n"
        f" - quit: строка-причина, если бросаешь. null — не бросаешь.\n"
    )
