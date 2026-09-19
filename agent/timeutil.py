"""Время: разбор ISO-меток, часовой пояс, человеческий рендер.

Модуль не знает ни про state, ни про диск, ни про LLM — его импортируют
и `store`, и `mind`, и `config`, а он не импортирует ничего из проекта.
Часов не дёргает: `now` всегда приходит параметром.
"""
import logging
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:          # py<3.9 / отсутствует tzdata
    ZoneInfo = None

DEFAULT_TZ_HOURS = 3.0

WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

DAYS_IN_YEAR = 365.2425

# Возраст памяти — нарочно нечёткий и без календарных обещаний:
# метки описывают прошедшее время, а не дату, поэтому не зависят от TZ
# и не врут на границе суток. (порог в часах, ярлык)
AGE_BUCKETS = (
    (1, "только что"),
    (18, "недавно"),
    (4 * 24, "на днях"),
    (10 * 24, "на прошлой неделе"),
    (25 * 24, "пару недель назад"),
    (70 * 24, "с месяц назад"),
)
AGE_FALLBACK = "давно"

# Давность НАСТРОЕНИЯ (Шаг 44). Свой словарь, а не `AGE_BUCKETS`, потому что
# шкалы расходятся на два порядка: у объекта «недавно» — это до восемнадцати
# часов, а для настроения восемнадцать часов — почти вся его жизнь. Взяли бы
# общий — и «только что» накрыло бы час, в течение которого настроение как раз
# и меняется, а всё остальное слилось бы в «недавно».
#
# Первый порог возвращает пустую строку, а не слово: свежее настроение в
# пометке не нуждается — оно и так «сейчас», это сказано рамкой строки.
# Верхняя граница шкалы — `mind.MOOD_FADE_HOURS`: дальше настроение просто не
# рендерится, и слова для «третьего дня» здесь нет не по забывчивости, а
# потому, что произнести его некому.
MOOD_BUCKETS = (
    (1.5, ""),
    (6.0, "уже несколько часов"),
    (14.0, "полдня"),
    (26.0, "почти сутки"),
)
MOOD_FALLBACK = "второй день"


def mood_age_word(since: datetime | None, now: datetime) -> str:
    """Сколько держится настроение, человеческими словами. Нет метки -> ''.

    Пустая строка и у свежего, и у безвестного: для рендера это одно и то же —
    пометки не будет. Разводить их значило бы завести исход, на который
    читателю нечем ответить (то же правило, что у `_clean_linger`).
    """
    if since is None:
        return ""
    hours = (now - since).total_seconds() / 3600.0
    if hours < 0:
        return ""
    for limit, label in MOOD_BUCKETS:
        if hours < limit:
            return label
    return MOOD_FALLBACK



def parse_tz(spec, default_hours: float = DEFAULT_TZ_HOURS) -> timezone:
    """Строка из окружения -> tzinfo.

    Понимает числовое смещение ("3", "+3", "-5.5", "05:30") и имя зоны
    ("Europe/Moscow", если доступен zoneinfo). Мусор или пусто -> дефолт:
    часовой пояс не тот повод, чтобы не запуститься.
    """
    fallback = timezone(timedelta(hours=default_hours))
    if spec is None:
        return fallback

    s = str(spec).strip()
    if not s or s.lower() in ("none", "null", "false"):
        return fallback

    body, sign = s, 1
    if body[0] in "+-":
        sign = -1 if body[0] == "-" else 1
        body = body[1:]

    hours = None
    try:
        if ":" in body:
            h, m = body.split(":", 1)
            hours = int(h) + int(m) / 60.0
        else:
            hours = float(body)
    except ValueError:
        hours = None

    if hours is not None:
        if abs(hours) > 14:
            logging.warning("parse_tz: смещение вне диапазона: %s", s)
            return fallback
        return timezone(timedelta(hours=sign * hours))

    if ZoneInfo is not None:
        try:
            return ZoneInfo(s)
        except Exception:
            pass

    logging.warning("parse_tz: не разобрал часовой пояс: %s", s)
    return fallback


def parse_ts(ts: str) -> datetime | None:
    """Разбор ISO-строки. Naive-метка достраивается до UTC, чтобы
    вычитание с aware-`now` не падало. Битая метка -> None."""
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def humanize_age(then: datetime | None, now: datetime) -> str | None:
    """Сколько прошло, человеческими словами. Метки нет -> None:
    за отсутствие метки объект не наказываем (то же правило, что
    у `effective_salience`)."""
    if then is None:
        return None
    hours = (now - then).total_seconds() / 3600.0
    if hours < 0:
        return AGE_BUCKETS[0][1]
    for limit, label in AGE_BUCKETS:
        if hours < limit:
            return label
    return AGE_FALLBACK


def render_now(now: datetime) -> str:
    """'пятница, 14 августа, 19:40'. На вход — уже локальное время."""
    return (
        f"{WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]} "
        f"{now.year}-го, {now.hour:02d}:{now.minute:02d}"
    )

def age_years(born: datetime | None, at: datetime | None) -> int | None:
    """Сколько полных лет исполнилось к моменту `at`. Нет метки -> None.

    То же правило, что у `humanize_age`: за отсутствие метки не наказываем.
    Отрицательный возраст тоже даёт None — событие раньше рождения законно
    (рассказанное о том, что было до тебя) и рендерится отдельной веткой.
    """
    if born is None or at is None:
        return None
    years = int((at - born).days // DAYS_IN_YEAR)
    return years if years >= 0 else None


def years_word(n: int) -> str:
    """год / года / лет.

    Жила в `mind` как приватная, пока читателей было двое и оба там же:
    строка про возраст персонажа и строка про его возраст в момент
    воспоминания. Переехала сюда на Шаге 39, когда появился третий читатель
    ВНЕ `mind` — печать сухого прогона рождения, которая говорила «сейчас 51
    лет». Правило переезда записано в самом докстринге, который тут стоял:
    функция живёт там, где её читатели, и общий лист — это то место, где
    сходятся `mind` и `agent`.
    """
    if 11 <= n % 100 <= 14:
        return "лет"
    last = n % 10
    if last == 1:
        return "год"
    if last in (2, 3, 4):
        return "года"
    return "лет"
