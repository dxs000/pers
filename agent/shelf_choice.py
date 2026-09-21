"""Выбор книги с полки.

Живёт отдельно от `mind`, чтобы промпт выбора можно было править, не трогая
остальные проходы. Разбор ответа тот же: JSON `{take, why}` или отказ.
"""

from __future__ import annotations

import json
import logging

import config
from mind import _strip_fences
from openai import OpenAIError
from snapshot import Turn, clip_text

CHOICE_NONE = "не сегодня"
WHY_CHAR_LIMIT = 200
SHELF_LIMIT = 20
SHELF_PAST_LIMIT = 10
PORTION_HINT = 20_000


def _evenings_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "вечеров"
    last = n % 10
    if last == 1:
        return "вечер"
    if last in (2, 3, 4):
        return "вечера"
    return "вечеров"


def _render_shelf(shelf: dict) -> str:
    lines = []
    for n, book in enumerate(shelf.get("free", [])[:SHELF_LIMIT], 1):
        who = f"{book['author']}. " if book.get("author") else ""
        length = int(book.get("length") or 0)
        evenings = max(1, round(length / PORTION_HINT))
        lines.append(
            f"[{n}] {who}{book['title']} — сегодня один вечер; "
            f"вся книга примерно {evenings} {_evenings_word(evenings)}"
        )
    return "\n".join(lines)


def _build_choice_prompt(turn: Turn, shelf: dict) -> str:
    who = f"Тебя зовут {turn.name}."
    if turn.place_label:
        who += f" Живёшь в {turn.place_label}."

    parts = [
        "Ты подошёл к полке. Книги читаются по одному вечеру: взять — "
        "значит открыть сегодня, а не прочесть целиком за ночь.\n",
        who,
    ]
    traits = getattr(turn, "traits_line", "") or (
        ", ".join(turn.traits) if getattr(turn, "traits", None) else ""
    )
    if traits:
        parts.append(f"Каким ты стал: {traits}.")
    if turn.mood:
        parts.append(f"Сейчас твоё настроение — {turn.mood}.")
    parts.append("")
    parts.append("На полке:\n" + _render_shelf(shelf) + "\n")

    past = shelf.get("past") or []
    if past:
        parts.append(
            "Что уже закрыто (это не запрет брать другое):\n" + "\n".join(
                f"- {(b['author'] + '. ') if b.get('author') else ''}"
                f"{b['title']} — {b.get('why') or 'закрыл'}"
                for b in past[:SHELF_PAST_LIMIT]) + "\n")

    if turn.threads:
        parts.append(
            "Что у тебя сейчас не закончено:\n"
            + "\n".join(f"- {t['text']}" for t in turn.threads) + "\n")

    # Шаг 56. Книгу берут не под характер, а под то, чего хочется и что
    # тревожит. Связи «книга под побуждение» в базе нет — она появится в
    # `picked_why`, если так и есть.
    drives = getattr(turn, "drives", None) or []
    if drives:
        import drives as drives_mod
        parts.append(
            "Что в тебе сейчас живёт:\n"
            + "\n".join(f"- {drives_mod.KIND_WORDS_YOU[d['kind']]}: {d['text']}"
                        for d in drives) + "\n")

    parts.append(
        "На руках пусто, на полке есть непрочитанное — обычный вечер это "
        "взять одну. Толщина не причина пройти мимо: сегодня один кусок.\n"
        f"«{CHOICE_NONE}» — если правда ни одна не лежит, не из вежливости "
        "к спокойствию и не потому что книга длинная.\n"
    )
    parts.append(
        "Формат — ТОЛЬКО JSON, без пояснений:\n"
        '{"take": 1, "why": "..."}\n'
        "take — номер с полки выше, why — одна фраза, зачем именно эта. "
        f"Не брать: {{\"take\": null, \"why\": \"{CHOICE_NONE}\"}}\n"
    )
    return "\n".join(parts)


def _parse_choice(row: str, shelf: dict):
    text = _strip_fences(row or "").strip()
    if not text:
        logging.warning("выбор книги: пустой ответ")
        return None
    if text.lower().lstrip("«\"'").startswith(CHOICE_NONE):
        logging.info("выбор книги: не сегодня (словом, без JSON)")
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logging.warning("выбор книги: не разобрал JSON: %s", text[:200])
        return None
    if not isinstance(data, dict):
        logging.warning("выбор книги: ожидается object, пришло %s",
                        type(data).__name__)
        return None

    free = (shelf.get("free") or [])[:SHELF_LIMIT]
    why = " ".join(str(data.get("why") or "").split()).strip("«»\"'")
    take = data.get("take")
    if take is None:
        logging.info("выбор книги: не сегодня — %s", why or "без объяснения")
        return None
    try:
        num = int(take)
    except (TypeError, ValueError):
        logging.warning("выбор книги: take не число, а %r", take)
        return None
    if not 1 <= num <= len(free):
        logging.warning("выбор книги: назван номер %s, а на полке показано %s",
                        num, len(free))
        return None
    book = free[num - 1]
    if not why:
        logging.warning("выбор книги: назвали книгу без причины — не беру")
        return None
    return {"text_path": book["text_path"], "title": book["title"],
            "author": book.get("author"),
            "why": clip_text(why, WHY_CHAR_LIMIT)}


def choose_book(turn: Turn, shelf: dict, client):
    if not (shelf.get("free") or []):
        return None
    prompt = _build_choice_prompt(turn, shelf)
    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
    except OpenAIError as err:
        logging.warning("выбор книги: запрос упал: %s", err)
        return None
    return _parse_choice(response.choices[0].message.content or "", shelf)
