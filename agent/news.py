"""Заход к новостям: он сам решает, куда глянуть, и говорит только через импульс.

## Что было и почему переписано (Шаг 55)

Первая редакция (Шаги 52–54) ломала три правила, которые проект держит
везде остальное время.

1. **Реплика шла мимо заслонок.** Отозвавшаяся новость уходила в чат в том же
   заходе, минуя паузу и бюджет суток `background_tick`. Заход был раз в
   15 минут круглые сутки, то есть в плохой новостной день персонаж мог
   заговорить десятки раз — ровно та навязчивость, от которой Шаг 37 завёл
   три заслонки. Теперь проход только ЗАПИСЫВАЕТ повод (`impulses.kind =
   'news'`), а говорить или нет, решает `background_tick`, как для погоды,
   сна и книги. Одна дверь в чат — одни правила на ней.

2. **Запрос был чужой.** Строка «главные новости России и мира сегодня»
   зашита в коде, то есть одна на всех персонажей, где бы они ни жили и
   чем бы ни занимались. Это ровно подсказка пути, которой проект избегает:
   человек, выросший под Кутаиси и работающий редактором, открывает не ту
   ленту, что пенсионер из Брянска. Теперь запрос набирает ОН, из того, что о
   нём записано: место, занятие, открытые нити, книга на руках. «Не сегодня»
   — законный ответ: не каждый день человек лезет в новости.

3. **Он не помнил, что уже читал.** Каждый заход смотрел ленту с чистого
   листа, и одна история, висящая в новостях неделю, отзывалась бы каждый
   день заново. Теперь в оба промпта уезжают предметы последних новостных
   поводов: и чтобы не набирать тот же запрос, и чтобы не отзываться на то,
   на что уже отозвался.

## Заслонки

Окно — день по ЕГО месту, как у чтения: ночью он спит, и новостной повод,
записанный в три часа ночи, протух бы к пробуждению. Тишина — час, как у
остальных фоновых проходов: пока идёт разговор, в ленту он не смотрит.
Интервал — три часа, считая от ЗАХОДА, а не от находки: пустой заход тоже
ставит метку, иначе «не сегодня» звало бы модель каждую минуту до вечера
(тот же довод, что у `agent.day_at`).
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

NEWS_HOUR_FROM = 8           # раньше — он ещё не проснулся
NEWS_HOUR_TO = 23            # позже — не лента, а бессонница
NEWS_QUIET_HOURS = 1.0       # столько никто не пишет — считаем, что он один
NEWS_INTERVAL_HOURS = 3.0    # не чаще; четыре-пять заходов за день
# Выше `cycle.IMPULSE_FLOOR`: отозвавшаяся новость — повод сам по себе, как
# сон. Но ниже сна (1.5): приснившееся своё сильнее прочитанного чужого.
NEWS_URGE = 1.15
# Новость протухает быстрее книги: к вечеру следующего дня она уже не новость,
# а то, что все обсудили без него.
NEWS_TTL_HOURS = 12.0
NEWS_RESULTS = 3
# Сколько прошлых новостных поводов показать и за какой срок. Неделя — столько
# крупная история держится в лентах.
NEWS_RECENT_DAYS = 7.0
NEWS_RECENT_LIMIT = 5
NEWS_SUBJECT_LIMIT = 200
QUERY_CHAR_LIMIT = 80
QUERY_NONE = "не сегодня"

# Метки исхода для `idle_tick`. Непустая строка значит «заход состоялся и
# стоил вызова модели» — дальше в этом тике идти не надо. `None` — заход не
# состоялся (заслонка), и тик продолжается к следующим проходам.
SKIPPED = "в новости не пошёл"
PASSED = "глянул в новости, мимо"


def news_tick(eng, edges, now: datetime, *, tz=None, force: bool = False):
    """Один заход к ленте. Возвращает метку исхода или `None` (заслонка).

    **Не говорит.** Самое сильное, что может случиться, — запись повода.
    Сказано ли будет, решит `background_tick` на следующем тике.
    """
    tz = tz or config.TZ
    if not force and not _gates_open(eng, now, tz):
        return None

    if not edges.search_key:
        log.info("новости: ключа поиска нет — проход выключен")
        with eng.unit():
            _set_news_at(eng, now)
        return None

    turn = eng.snapshot(now)
    recent = _recent_subjects(eng, now)

    query = _choose_query(edges.llm, turn, now.astimezone(tz), recent)
    if not query:
        with eng.unit():
            _set_news_at(eng, now)
        log.info("новости: не сегодня")
        return SKIPPED

    found = web.search(query, edges.search, edges.search_key,
                       max_results=NEWS_RESULTS, topic="news", days=1, now=now)
    with eng.unit():
        _set_news_at(eng, now)
    if not found:
        log.info("новости: по «%s» пусто или поиска не было", query)
        return PASSED

    data = _ask_json(edges.llm, build_stir_prompt(turn, now.astimezone(tz), query,
                                                 found, recent))
    if not data or not data.get("stirred"):
        log.info("новости: «%s» — прошёл мимо (%s)", query,
                 (data or {}).get("why") or "не отозвалось")
        return PASSED

    subject = _one_line(data.get("about")) or _one_line(data.get("why"))
    if not subject:
        log.info("новости: отозвалось без предмета — повод не заведён")
        return PASSED
    subject = subject[:NEWS_SUBJECT_LIMIT]

    with eng.unit():
        eng.record_urge("news", subject, NEWS_URGE, now,
                        now + timedelta(hours=NEWS_TTL_HOURS))
    log.info("новости: «%s» отозвалось — %s", query, subject[:80])
    return f"отозвалось: {subject[:80]}"


# =============================================================================
# Заслонки
# =============================================================================

def _gates_open(eng, now: datetime, tz) -> bool:
    local = now.astimezone(tz)
    if not (NEWS_HOUR_FROM <= local.hour < NEWS_HOUR_TO):
        return False
    stamps = [t for t in (eng.last_exchange(), eng.last_utterance()) if t]
    if stamps:
        idle = (now - max(stamps)).total_seconds() / 3600.0
        if 0.0 <= idle < NEWS_QUIET_HOURS:
            return False
    settled = _news_at(eng)
    if settled is not None:
        hours = (now - settled).total_seconds() / 3600.0
        if 0.0 <= hours < NEWS_INTERVAL_HOURS:
            return False
    return True


# =============================================================================
# Промпты
# =============================================================================

def _age(turn, now: datetime) -> int | None:
    if not turn.born_at:
        return None
    born = datetime.fromisoformat(turn.born_at)
    return now.year - born.year - ((now.month, now.day) < (born.month, born.day))


def _who(turn, now: datetime) -> str:
    """Шапка «кто он» для обоих промптов. Одна функция — одни слова."""
    name = turn.name or "Персонаж"
    parts = [f"Его зовут {name}."]
    age = _age(turn, now)
    if age is not None:
        parts.append(f"Ему {age}.")
    if turn.place_label:
        parts.append(f"Живёт: {turn.place_label}.")
    if turn.birthplace:
        parts.append(f"Родом: {turn.birthplace}.")
    lines = [" ".join(parts)]
    if turn.traits:
        lines.append(f"Каким он стал: {', '.join(turn.traits)}.")
    lines.append(f"Настроение: {turn.mood}.")
    facts = [f"- {a['key']}: {a['value']}" for a in (turn.self_assertions or [])]
    if facts:
        lines.append("Что он знает о себе:\n" + "\n".join(facts))
    drives = [f"- {d['kind_word']}: {d['text']}" for d in _drive_words(turn)]
    if drives:
        lines.append("Чего он хочет и чего боится:\n" + "\n".join(drives))
    threads = [f"- {t['text']}" for t in (turn.threads or [])]
    if threads:
        lines.append("Чем сейчас занята голова:\n" + "\n".join(threads))
    if turn.reading:
        title = turn.reading.get("title") or "книгу"
        author = turn.reading.get("author")
        lines.append(f"Читает: {title}" + (f" ({author})" if author else "") + ".")
    return "\n".join(lines)


def _drive_words(turn) -> list[dict]:
    import drives as drives_mod
    return [{"kind_word": drives_mod.KIND_WORDS[d["kind"]], "text": d["text"]}
            for d in (getattr(turn, "drives", None) or [])]


def _recent_block(recent: list[str]) -> str:
    if not recent:
        return "За последнюю неделю в новостях его ничего не задевало."
    return ("Что в новостях уже задевало его за неделю:\n"
            + "\n".join(f"- {s}" for s in recent))


def build_query_prompt(turn, now_local: datetime, recent: list[str]) -> str:
    return (
        "Ты - служебный проход новостей. Задача: решить, куда он сейчас "
        "глянет, когда возьмёт телефон посмотреть, что в мире.\n\n"
        f"{_who(turn, now_local)}\n"
        f"Сейчас {now_local:%H:%M}.\n\n"
        f"{_recent_block(recent)}\n\n"
        "Люди не читают «главные новости вообще». Каждый открывает своё: "
        "город, где живёт, или город, откуда родом; своё ремесло; то, что "
        "его сейчас не отпускает; иногда просто то, что все обсуждают. Что "
        "из этого откроет ОН - реши по тому, что о нём записано выше, а не "
        "по тому, что важно вообще.\n\n"
        "Не повторяй то, что уже задевало его на этой неделе: он это видел.\n\n"
        "Ответ - ОДНА строка: поисковый запрос в 2-6 слов, как набрал бы он "
        f"сам. Или ровно «{QUERY_NONE}», если сейчас он в новости не полезет - "
        "это обычный ответ, не каждый заход человек открывает ленту."
    )


def build_stir_prompt(turn, now_local: datetime, query: str,
                      found: list[dict], recent: list[str]) -> str:
    items = "\n".join(f"- {it.get('title')}: {it.get('snippet')}" for it in found)
    return (
        "Ты - служебный проход новостей. Задача: решить, задело ли его "
        "хоть что-то в ленте, а не составить сводку.\n\n"
        f"{_who(turn, now_local)}\n\n"
        f"{_recent_block(recent)}\n\n"
        f"Он набрал «{query}» и увидел:\n{items}\n\n"
        "Чаще правильный ответ - не задело. Задевает то, что касается его "
        "жизни, его места, его дела, его людей или того, что у него сейчас "
        "не закончено. «Важно вообще» - не повод. То, что уже задевало на "
        "этой неделе, - не повод во второй раз.\n\n"
        "Формат - ТОЛЬКО JSON:\n"
        '{"stirred": false, "about": null, "why": "мимо"}\n'
        "Если задело: stirred=true; about - что именно задело, его словами, "
        "одной фразой, не заголовок ленты; why - почему именно его."
    )


# =============================================================================
# Модель
# =============================================================================

def _choose_query(client, turn, now_local: datetime, recent: list[str]) -> str | None:
    raw = _ask(client, build_query_prompt(turn, now_local, recent))
    if raw is None:
        return None
    line = _one_line(raw.strip().strip("`").splitlines()[0] if raw.strip() else "")
    line = line.strip(" «»\"'.")
    if not line or line.lower().startswith(QUERY_NONE):
        return None
    return line[:QUERY_CHAR_LIMIT]


def _ask(client, prompt: str) -> str | None:
    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL_LIGHT,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        )
    except OpenAIError as err:
        log.warning("новости: запрос упал: %s", err)
        return None
    return response.choices[0].message.content or ""


def _ask_json(client, prompt: str):
    raw = _ask(client, prompt)
    return None if raw is None else _parse_json(raw)


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
        log.warning("новости: ответ не JSON")
        return None


def _one_line(value) -> str:
    return " ".join(str(value or "").split()).strip()


# =============================================================================
# Хранилище
# =============================================================================
# Запросы здесь, а не в `store_pg`, по той же причине, что и у `news_at` с
# первой редакции: модуль пока живёт своим швом. Переедут вместе, когда
# новости станут одним из действий прохода «чем заняться».

def _recent_subjects(eng, now: datetime) -> list[str]:
    try:
        rows = eng.conn.execute(
            """SELECT subject FROM impulses
                WHERE kind = 'news' AND subject IS NOT NULL
                  AND created_at >= %s
                ORDER BY created_at DESC LIMIT %s""",
            (now - timedelta(days=NEWS_RECENT_DAYS), NEWS_RECENT_LIMIT),
        ).fetchall()
    except Exception as err:
        log.warning("новости: не прочёл прошлые поводы: %s", err)
        return []
    return [r["subject"] for r in rows]


def _news_at(eng):
    try:
        row = eng.conn.execute("SELECT news_at FROM agent WHERE id = 1").fetchone()
    except Exception:
        return None
    return row["news_at"] if row else None


def _set_news_at(eng, now):
    eng.conn.execute("UPDATE agent SET news_at = %s WHERE id = 1", (now,))
