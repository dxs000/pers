"""Проход эссе: захотел — пишет вечер, не захотел — молчит.

Не ветка cycle.py. Почты нет. Тема с экрана нет.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import config
import outbox
import store_essay
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
        settled = _essay_at(eng)
        if settled is not None:
            hours = (now - settled).total_seconds() / 3600.0
            if 0.0 <= hours < ESSAY_INTERVAL_HOURS:
                return None
    current = store_essay.current_essay(eng.conn)
    if current is None:
        return _begin(eng, edges, now)
    return _continue(eng, edges, current, now)


def _begin(eng, edges, now: datetime):
    notes = eng.untold_notes(8)
    if not notes:
        with eng.unit():
            _set_essay_at(eng, now)
        return None
    turn = eng.snapshot(now)
    data = _ask_json(edges.llm, _want_prompt(turn, notes), light=True)
    if not data or not data.get("write"):
        with eng.unit():
            _set_essay_at(eng, now)
        return None
    title = (data.get("title") or "").strip()
    why = (data.get("why") or "").strip()
    if not title or not why:
        with eng.unit():
            _set_essay_at(eng, now)
        return None
    path = outbox.path_for(title)
    with eng.unit():
        rel = str(path.relative_to(outbox.root()) if _under(path, outbox.root()) else path)
        row = store_essay.open_essay(eng.conn, title, why, rel, now)
        _set_essay_at(eng, now)
        if row is None:
            return None
    log.info("начал эссе: %s (%s)", title, why)
    return f"начал эссе «{title}»"


def _continue(eng, edges, essay, now: datetime):
    turn = eng.snapshot(now)
    past = store_essay.passages_so_far(eng.conn, essay["id"], 8)
    data = _ask_json(edges.llm, _write_prompt(turn, essay, past), light=False)
    if not data:
        with eng.unit():
            _set_essay_at(eng, now)
        return None
    quit = data.get("quit")
    chunk = (data.get("text") or "").strip()
    path = _resolve(essay["file_path"])
    with eng.unit():
        _set_essay_at(eng, now)
        if chunk:
            written = outbox.append(
                path, chunk,
                header=f"# {essay['title']}\n\n_{essay['why']}_",
            )
            if written:
                store_essay.add_passage(eng.conn, essay["id"], chunk, now)
        if quit:
            store_essay.close_essay(eng.conn, essay["id"], str(quit), now)
            eng.record_urge("essay", essay["title"], ESSAY_URGE, now,
                            now + timedelta(hours=ESSAY_TTL_HOURS))
        elif data.get("done"):
            store_essay.close_essay(eng.conn, essay["id"], "написал", now)
            eng.record_urge("essay", essay["title"], ESSAY_URGE, now,
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


def _essay_at(eng):
    row = eng.conn.execute("SELECT essay_at FROM agent WHERE id = 1").fetchone()
    return row["essay_at"] if row else None


def _set_essay_at(eng, now):
    eng.conn.execute("UPDATE agent SET essay_at = %s WHERE id = 1", (now,))


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve(stored: str) -> Path:
    p = Path(stored)
    return p if p.is_absolute() else outbox.root() / p


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
    return _parse_json(response.choices[0].message.content or "")


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
        f"Это не задание. Чаще правильный ответ — не писать.\n"
        f"Не бери жанр как тему. Бери одну свою зацепку, если тянет.\n\n"
        f"Формат — ТОЛЬКО JSON:\n"
        "{\"write\": false, \"title\": null, \"why\": null, \"seed\": null}\n"
        f"Если пишет: write=true, title, why одной фразой.\n"
    )


def _write_prompt(turn, essay, past) -> str:
    who = turn.name or "персонаж"
    traits = ", ".join(turn.traits) if turn.traits else ""
    prev = "\n".join(f"- {c}" for c in past) if past else "(ещё ничего не написано)"
    return (
        f"Ты пишешь эссе. Тему никто не задавал.\n\n"
        f"Тебя зовут {who}. Черты: {traits}. Настроение: {turn.mood}.\n"
        f"Эссе: «{essay['title']}». Зачем: {essay['why']}.\n\n"
        f"Уже сказано:\n{prev}\n\n"
        f"Следующий кусок, до {CHUNK_CHARS} знаков, от себя.\n\n"
        f"Формат — ТОЛЬКО JSON:\n"
        "{\"text\": \"...\", \"done\": false, \"quit\": null}\n"
    )
