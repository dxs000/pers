"""Заход к новостям: посмотреть, не пересказать.

Если не отозвалось — тишина. Если отозвалось — реплика в чат
в этом же заходе, мимо заслонок background_tick.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

import config
import web
from openai import OpenAIError

log = logging.getLogger("news")

NEWS_HOUR_FROM = 8
NEWS_HOUR_TO = 22
NEWS_QUIET_HOURS = 0.25
NEWS_INTERVAL_HOURS = 0.5
NEWS_URGE = 1.15
NEWS_TTL_HOURS = 24.0
NEWS_QUERY = "главные новости России и мира сегодня"


def news_tick(eng, edges, now: datetime, *, tz=None, force: bool = False,
              announce=None):
    tz = tz or config.TZ
    if not force:
        local = now.astimezone(tz)
        if not (NEWS_HOUR_FROM <= local.hour < NEWS_HOUR_TO):
            return None
        stamps = [t for t in (eng.last_exchange(), eng.last_utterance()) if t]
        if stamps:
            idle = (now - max(stamps)).total_seconds() / 3600.0
            if 0.0 <= idle < NEWS_QUIET_HOURS:
                return None
        settled = _news_at(eng)
        if settled is not None:
            hours = (now - settled).total_seconds() / 3600.0
            if 0.0 <= hours < NEWS_INTERVAL_HOURS:
                return None

    if not edges.search_key:
        log.info("новости: ключа поиска нет — проход выключен")
        with eng.unit():
            _set_news_at(eng, now)
        return None

    turn = eng.snapshot(now)
    query = NEWS_QUERY
    found = web.search(
        query,
        edges.search,
        edges.search_key,
        max_results=3,
        topic="news",
        days=1,
        now=now,
    )
    with eng.unit():
        _set_news_at(eng, now)

    if found is None:
        log.info("новости: поиска не было")
        return None
    if not found:
        log.info("новости: пустая лента")
        return None

    data = _ask_json(edges.llm, _stir_prompt(turn, found))
    if not data or not data.get("stirred"):
        why = (data or {}).get("why") or "не отозвалось"
        log.info("новости: прошёл мимо — %s", why)
        return "глянул в новости, мимо"

    about = " ".join(str(data.get("about") or "").split()).strip()
    why = " ".join(str(data.get("why") or "").split()).strip()
    subject = about or why
    if not subject:
        log.info("новости: отозвалось без предмета — не говорю")
        return "глянул в новости, мимо"

    impulse = {
        "kind": "news",
        "subject": subject[:200],
        "id": None,
        "urge": NEWS_URGE,
    }
    text = _speak(eng, edges, turn, impulse, now, tz)
    if not text:
        expires = now + timedelta(hours=NEWS_TTL_HOURS)
        with eng.unit():
            eng.record_urge("news", subject[:200], NEWS_URGE, now, expires)
        log.info("новости: отозвалось, сказать не вышло — импульс оставлен")
        return f"отозвалось: {subject[:80]}"

    with eng.unit():
        eng.append_utterance(text, now)
    log.info("новости в чат: %s", text[:80])
    if announce is not None:
        announce(text)
    return text


def _speak(eng, edges, turn, impulse, now, tz):
    from mind import speak_first
    try:
        return speak_first(
            turn, impulse, edges.llm,
            memory=eng.working_memory(),
            now=now.astimezone(tz),
            last_exchange=eng.last_exchange(),
        )
    except Exception as err:
        log.warning("новости: реплика не собралась: %s", err)
        return None


def _news_at(eng):
    try:
        row = eng.conn.execute(
            "SELECT news_at FROM agent WHERE id = 1"
        ).fetchone()
    except Exception:
        return None
    return row["news_at"] if row else None


def _set_news_at(eng, now):
    try:
        eng.conn.execute(
            "UPDATE agent SET news_at = %s WHERE id = 1", (now,)
        )
    except Exception as err:
        log.warning("новости: не записал news_at: %s", err)


def _ask_json(client, prompt: str):
    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL_LIGHT,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        )
    except OpenAIError as err:
        log.warning("новости: запрос упал: %s", err)
        return None
    return _parse_json(response.choices[0].message.content or "")


def _parse_json(raw: str):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        log.warning("новости: ответ не JSON")
        return None


def _stir_prompt(turn, found) -> str:
    who = turn.name or "персонаж"
    traits = ", ".join(turn.traits) if getattr(turn, "traits", None) else ""
    items = "\n".join(
        f"- {it.get('title')}: {it.get('snippet')}"
        for it in found
    )
    return (
        f"Ты — служебный проход. {who} глянул в новости. "
        f"Задача — решить, отозвалось ли хоть что-то ЕМУ, "
        f"а не составить сводку.\n\n"
        f"Черты: {traits or 'ещё без черт'}. "
        f"Настроение: {turn.mood}.\n\n"
        f"Лента (не пересказывать):\n{items}\n\n"
        f"Чаще правильный ответ — не отозвалось. "
        f"Не бери «важно вообще». Бери только то, что задело бы его.\n"
        f"Не формулируй новость заново.\n\n"
        f"Формат — ТОЛЬКО JSON:\n"
        '{"stirred": false, "about": null, "why": "мимо"}\n'
        "Если отозвалось: stirred=true, about — короткая зацепка "
        "(не заголовок ленты), why — одной фразой почему его.\n"
    )


def attach_reason() -> None:
    try:
        import mind
        mind.IMPULSE_REASONS.setdefault(
            "news",
            "Ты глянул в новости. Ленту не пересказывай. "
            "Скажи только если задело — и своими словами.",
        )
    except Exception:
        pass


attach_reason()
