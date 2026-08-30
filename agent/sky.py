"""Небо: положение солнца и фаза луны из координат и момента времени.

Офлайн-арифметика. Ни сети, ни ключа, ни режима отказа: всё считается из
широты, долготы и даты. Модуль не знает ни про state, ни про диск, ни про
LLM и не импортирует из проекта **ничего** — то же правило, что у `timeutil`.
Часов не дёргает: `when` всегда приходит параметром.

**Здесь нет рендера.** Наружу торчат числа, метки времени и короткие коды
(`"day"`, `"civil"`, `"night"`); слова, которыми это скажет персонаж, живут
в слое рендера — как `WEEKDAYS`/`MONTHS` живут в `timeutil.render_now`, а не
в `store`.

Хранение и возврат — UTC (aware `datetime`). Часовой пояс нужен ровно в одном
месте: определить, какие календарные сутки считать «сегодняшними» для восхода
и заката. Поэтому `tz` — необязательный параметр, а не импорт из `config`.

Точность — NOAA solar equations, порядка ±1 минуты на восход/закат в средних
широтах. Этого с запасом хватает для «стемнело», ради которого всё затевается.
"""

import logging
import math
from datetime import datetime, time, timedelta, timezone

# --- Пороги высоты солнца (градусы) --------------------------------------
# -0.833 — не ноль: солнце садится, когда его *верхний край* касается
# горизонта (радиус диска ~0.267°) и атмосферная рефракция приподнимает
# картинку ещё на ~0.567°. Классическая величина, ею считают все альманахи.
HORIZON = -0.833
CIVIL = -6.0
NAUTICAL = -12.0
ASTRONOMICAL = -18.0

# Коды состояния света. Не слова для промпта — коды для слоя рендера.
LIGHT_DAY = "day"
LIGHT_CIVIL = "civil"
LIGHT_NAUTICAL = "nautical"
LIGHT_ASTRONOMICAL = "astronomical"
LIGHT_NIGHT = "night"

# Пороги событий: код -> (высота, имя утреннего, имя вечернего)
_EVENTS = (
    ("sun", HORIZON, "sunrise", "sunset"),
    ("civil", CIVIL, "civil_dawn", "civil_dusk"),
    ("nautical", NAUTICAL, "nautical_dawn", "nautical_dusk"),
    ("astronomical", ASTRONOMICAL, "astronomical_dawn", "astronomical_dusk"),
)

# --- Луна ----------------------------------------------------------------
SYNODIC_MONTH = 29.530588853  # средний синодический месяц, суток
_NEW_MOON_REF = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)

MOON_PHASES = (
    (0.020, "new"),
    (0.240, "waxing_crescent"),
    (0.280, "first_quarter"),
    (0.480, "waxing_gibbous"),
    (0.520, "full"),
    (0.720, "waning_gibbous"),
    (0.780, "last_quarter"),
    (0.980, "waning_crescent"),
)
MOON_PHASE_FALLBACK = "new"


# =========================================================================
# Вход: приведение аргументов. Мусор не роняет расчёт — тот же принцип,
# что у `timeutil.parse_tz` и `store.load_state`.
# =========================================================================

def _as_utc(when: datetime) -> datetime:
    """Naive-метка достраивается до UTC — как в `timeutil.parse_ts`."""
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _clamp_lat(lat: float) -> float:
    try:
        value = float(lat)
    except (TypeError, ValueError):
        logging.warning("sky: не разобрал широту: %r", lat)
        return 0.0
    if not -90.0 <= value <= 90.0:
        logging.warning("sky: широта вне диапазона: %r", lat)
        return max(-90.0, min(90.0, value))
    return value


def _wrap_lon(lon: float) -> float:
    try:
        value = float(lon)
    except (TypeError, ValueError):
        logging.warning("sky: не разобрал долготу: %r", lon)
        return 0.0
    return (value + 180.0) % 360.0 - 180.0


def _clamp_unit(x: float) -> float:
    """Страховка от -1.0000000002 на входе в asin/acos."""
    return max(-1.0, min(1.0, x))


# =========================================================================
# Астрономия: положение солнца (NOAA)
# =========================================================================

def _julian_day(when: datetime) -> float:
    return _as_utc(when).timestamp() / 86400.0 + 2440587.5


def _julian_century(jd: float) -> float:
    return (jd - 2451545.0) / 36525.0


def _solar_params(t: float) -> tuple[float, float]:
    """Склонение солнца (градусы) и уравнение времени (минуты) на юлианский век `t`.

    Склонение отвечает за «где солнце по высоте», уравнение времени — за
    расхождение солнечных и часовых суток (до ±16 минут за год).
    """
    # средняя долгота и средняя аномалия
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    m_rad = math.radians(m)

    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    # уравнение центра: поправка на эллиптичность орбиты
    c = (
        math.sin(m_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m_rad) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m_rad) * 0.000289
    )

    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    # наклон эклиптики
    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))

    decl = math.degrees(
        math.asin(_clamp_unit(math.sin(math.radians(eps)) * math.sin(math.radians(app_long))))
    )

    y = math.tan(math.radians(eps / 2.0)) ** 2
    l0_rad = math.radians(l0)
    eq_time = 4.0 * math.degrees(
        y * math.sin(2 * l0_rad)
        - 2 * e * math.sin(m_rad)
        + 4 * e * y * math.sin(m_rad) * math.cos(2 * l0_rad)
        - 0.5 * y * y * math.sin(4 * l0_rad)
        - 1.25 * e * e * math.sin(2 * m_rad)
    )
    return decl, eq_time


def solar_altitude(lat: float, lon: float, when: datetime) -> float:
    """Геометрическая высота солнца над горизонтом, градусы.

    Без поправки на рефракцию: пороги (`HORIZON` и ниже) её уже учитывают,
    вводить её дважды значило бы врать на полградуса у самого горизонта.
    """
    when = _as_utc(when)
    lat = _clamp_lat(lat)
    lon = _wrap_lon(lon)

    decl, eq_time = _solar_params(_julian_century(_julian_day(when)))

    minutes = when.hour * 60 + when.minute + when.second / 60.0
    true_solar_time = (minutes + eq_time + 4.0 * lon) % 1440.0
    hour_angle = math.radians(true_solar_time / 4.0 - 180.0)

    lat_rad = math.radians(lat)
    decl_rad = math.radians(decl)
    sin_alt = (
        math.sin(lat_rad) * math.sin(decl_rad)
        + math.cos(lat_rad) * math.cos(decl_rad) * math.cos(hour_angle)
    )
    return math.degrees(math.asin(_clamp_unit(sin_alt)))


def light_state(altitude: float) -> str:
    """Высота солнца -> код состояния света.

    Это и есть замена порогам «утро/день/вечер» по часам: 19:00 июня и
    19:00 декабря различаются здесь, а не в таблице часов.
    """
    if altitude > HORIZON:
        return LIGHT_DAY
    if altitude > CIVIL:
        return LIGHT_CIVIL
    if altitude > NAUTICAL:
        return LIGHT_NAUTICAL
    if altitude > ASTRONOMICAL:
        return LIGHT_ASTRONOMICAL
    return LIGHT_NIGHT


# =========================================================================
# События суток: восход, закат, сумерки
# =========================================================================

def _hour_angle(lat: float, decl: float, altitude: float) -> float | None:
    """Часовой угол (градусы) пересечения солнцем высоты `altitude`.

    `None` — солнце этой высоты в эти сутки **не пересекает**: либо весь
    день выше, либо весь день ниже. Это штатный ответ, а не ошибка. Именно
    здесь наивная реализация падает `ValueError` из `acos` — под Хельсинки
    в июне солнце ниже -6° не опускается, и порог просто не достигается.
    """
    lat_rad = math.radians(lat)
    decl_rad = math.radians(decl)
    denom = math.cos(lat_rad) * math.cos(decl_rad)
    if abs(denom) < 1e-12:  # ровно полюс: cos(широты) == 0
        return None
    cos_ha = (math.sin(math.radians(altitude)) - math.sin(lat_rad) * math.sin(decl_rad)) / denom
    if cos_ha > 1.0 or cos_ha < -1.0:
        return None
    return math.degrees(math.acos(cos_ha))


def _always_above(lat: float, decl: float, altitude: float) -> bool:
    """Куда именно «не пересекает»: солнце весь день выше порога или ниже?

    Смотрим на высоту в истинный полдень — она максимальна за сутки.
    """
    noon_alt = 90.0 - abs(lat - decl)
    return noon_alt > altitude


def _local_day_anchor(when: datetime, tz: timezone | None) -> datetime:
    """Полдень тех календарных суток, к которым относится `when`.

    Часовой пояс нужен ровно здесь: без него «сегодняшний закат» для
    долгот западнее Гринвича уезжал бы в соседние UTC-сутки.
    """
    if tz is None:
        local = _as_utc(when)
        return datetime.combine(local.date(), time(12), tzinfo=timezone.utc)
    local = _as_utc(when).astimezone(tz)
    return datetime.combine(local.date(), time(12), tzinfo=tz).astimezone(timezone.utc)


def solar_noon(lon: float, when: datetime, tz: timezone | None = None) -> datetime:
    """Истинный полдень (солнце в верхней точке) для суток, содержащих `when`."""
    lon = _wrap_lon(lon)
    anchor = _local_day_anchor(when, tz)
    midnight = anchor.replace(hour=0, minute=0, second=0, microsecond=0)

    result = anchor
    for _ in range(2):  # уравнение времени берём в уже уточнённый момент
        _, eq_time = _solar_params(_julian_century(_julian_day(result)))
        result = midnight + timedelta(minutes=720.0 - 4.0 * lon - eq_time)
    return result


def sun_events(
    lat: float, lon: float, when: datetime, tz: timezone | None = None
) -> dict:
    """Восход, закат, три пары сумерек и полдень для суток, содержащих `when`.

    Все метки — aware UTC. Событие, которого в эти сутки нет, приходит
    как `None`, а в `never[code]` ложится, в какую сторону вырождение:
    `"above"` — солнце весь день выше порога (полярный день, белые ночи),
    `"below"` — весь день ниже (полярная ночь, сумерки не наступают).
    **Отсутствие события — обычный ответ, а не отказ.**
    """
    lat = _clamp_lat(lat)
    lon = _wrap_lon(lon)
    noon = solar_noon(lon, when, tz)

    result: dict = {"solar_noon": noon, "never": {}}
    decl, _ = _solar_params(_julian_century(_julian_day(noon)))

    for code, altitude, dawn_key, dusk_key in _EVENTS:
        ha = _hour_angle(lat, decl, altitude)
        if ha is None:
            result[dawn_key] = None
            result[dusk_key] = None
            result["never"][code] = "above" if _always_above(lat, decl, altitude) else "below"
            continue

        dawn = noon - timedelta(minutes=4.0 * ha)
        dusk = noon + timedelta(minutes=4.0 * ha)

        # Уточняем: склонение за полдня успевает сдвинуться, у высоких
        # широт это минуты. Одна итерация в каждую сторону — достаточно.
        refined = []
        for rough, sign in ((dawn, -1), (dusk, 1)):
            decl_local, _ = _solar_params(_julian_century(_julian_day(rough)))
            ha_local = _hour_angle(lat, decl_local, altitude)
            refined.append(rough if ha_local is None else noon + timedelta(minutes=sign * 4.0 * ha_local))

        result[dawn_key], result[dusk_key] = refined
        result["never"][code] = None

    return result


def day_length(lat: float, lon: float, when: datetime, tz: timezone | None = None) -> timedelta | None:
    """Продолжительность светового дня. `None` — полярный день или ночь:
    сутки без восхода длину дня не определяют (её надо звать 24 ч или 0,
    а это разные вещи — пусть решает слой выше, глядя на `never`)."""
    events = sun_events(lat, lon, when, tz)
    if events["sunrise"] is None or events["sunset"] is None:
        return None
    return events["sunset"] - events["sunrise"]


def day_length_trend(
    lat: float, lon: float, when: datetime, tz: timezone | None = None
) -> float | None:
    """На сколько секунд сегодняшний день длиннее вчерашнего.

    Плюс — прибывает, минус — убывает. `None` — сравнивать нечего
    (полярный день/ночь хотя бы в одни из двух суток).
    """
    today = day_length(lat, lon, when, tz)
    yesterday = day_length(lat, lon, _as_utc(when) - timedelta(days=1), tz)
    if today is None or yesterday is None:
        return None
    return (today - yesterday).total_seconds()


def next_sun_event(
    lat: float, lon: float, when: datetime, tz: timezone | None = None
) -> dict | None:
    """Ближайшие восход или закат **после** `when`.

    Смотрит сегодняшние сутки, потом следующие: в 23:00 «сегодняшний»
    восход уже позади, и честный ответ — завтрашний. `None` — событий нет
    вовсе (полярный день или ночь).
    """
    when = _as_utc(when)
    for offset in (0, 1):
        events = sun_events(lat, lon, when + timedelta(days=offset), tz)
        upcoming = [
            (events[key], kind)
            for key, kind in (("sunrise", "sunrise"), ("sunset", "sunset"))
            if events[key] is not None and events[key] > when
        ]
        if upcoming:
            moment, kind = min(upcoming)
            return {"kind": kind, "when": moment}
    return None


# =========================================================================
# Луна
# =========================================================================

def moon_phase(when: datetime) -> dict:
    """Фаза луны: доля цикла, освещённость, возраст в сутках и код фазы.

    Средний синодический месяц от известного новолуния — модель грубая
    (истинное новолуние гуляет на ±0.3 суток), но для «полнолуние за
    окном» точности хватает с запасом, а сети и эфемерид не нужно.
    """
    days = (_as_utc(when) - _NEW_MOON_REF).total_seconds() / 86400.0
    fraction = (days % SYNODIC_MONTH) / SYNODIC_MONTH
    illumination = (1.0 - math.cos(2.0 * math.pi * fraction)) / 2.0

    name = MOON_PHASE_FALLBACK
    for limit, label in MOON_PHASES:
        if fraction < limit:
            name = label
            break

    return {
        "phase": fraction,
        "illumination": illumination,
        "age_days": fraction * SYNODIC_MONTH,
        "name": name,
    }


# =========================================================================
# Слепок: одна точка входа для слоя рендера
# =========================================================================

def snapshot(lat: float, lon: float, when: datetime, tz: timezone | None = None) -> dict:
    """Всё небо на момент `when` одним вызовом. Чистая функция, ничего не мутирует.

    Слой рендера (Шаг 11b) берёт отсюда готовые числа и коды и решает, что
    из этого вообще стоит произносить. Здесь решений о словах нет.
    """
    when = _as_utc(when)
    altitude = solar_altitude(lat, lon, when)
    events = sun_events(lat, lon, when, tz)
    length = day_length(lat, lon, when, tz)

    # Направление, а не латч: высота десятью минутами раньше — та же чистая
    # арифметика. Само по себе `altitude` направления не знает, и без этого
    # «темнеет» в девять утра декабря звучит ровно наоборот.
    rising = altitude > solar_altitude(lat, lon, when - timedelta(minutes=10))

    return {
        "when": when,
        "altitude": altitude,
        "rising": rising,
        "light": light_state(altitude),
        "events": events,
        "day_length": length,
        "day_length_seconds": None if length is None else length.total_seconds(),
        "day_length_trend": day_length_trend(lat, lon, when, tz),
        "next_event": next_sun_event(lat, lon, when, tz),
        "moon": moon_phase(when),
    }


# =========================================================================
# Место и пояс: слепок, пригодный для промпта (Шаг 32)
# =========================================================================
# До Шага 32 эти двадцать строк жили в `cycle.sky_snapshot` и брали место
# из фасада (`eng.place()`). Фасад — дверь ПИСАТЕЛЯ, и открыть его на
# читающем соединении нельзя (`_ensure_agent` доливает дефолты, то есть сам
# является записью). Значит инспектор не мог показать блок среды, хотя небо
# считается офлайн и детерминированно, — и первый пункт `PROMPT_CAVEATS`
# держался не природой данных, а формой вызова.
#
# Отсюда переезд ИМЕННО СЮДА, а не в общий модуль хода: `sky` не импортирует
# из проекта ничего, поэтому звать его вправе и ход (у него фасад), и
# инспектор (у него read-only соединение), и сбруя. Копия при этом не
# заводится: расчёт остаётся один, меняется только то, кто приносит
# координаты.

def coords(lat, lon) -> tuple[float, float] | None:
    """Пара чисел или `None`. Единственное место, где решается, есть ли место.

    Читателей у правила два — небо и погода, — и разъехаться им нельзя:
    расчёт, считающий место заданным там, где сеть считает его пустым, дал бы
    блок среды с солнцем и без погоды по причине, которой нет в данных.

    Пусто бывает трояко, и все три случая законны: колонка `NULL` (место
    названо именем, геокодер ещё не ходил), ключа нет вовсе (`place()`
    выбрасывает пустые), `APP_LAT` из `.env` не разобрался в число.
    """
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def local_snapshot(lat, lon, when: datetime, tz: timezone) -> dict | None:
    """Небо на момент `when` в поясе `tz` — или `None`, если места нет.

    Пояс приходит ОБЯЗАТЕЛЬНЫМ параметром, без отката на `config.TZ`: он
    настройка запуска, а не свойство неба, и модулю, не знающему про проект,
    взять его неоткуда. Заодно это снимает источник недетерминизма №2 сбруи
    в самом низу, а не в каждом вызывающем.

    `except Exception` широкий сознательно. Арифметика выше — чистая и без
    режима отказа, но урок Шага 9 ровно в том, что падение чистой функции
    роняет диалог целиком, а погода за окном такой цены не стоит.
    """
    pair = coords(lat, lon)
    if pair is None:
        return None

    try:
        snap = snapshot(pair[0], pair[1], when, tz)
    except Exception as err:
        logging.warning("sky: слепок не собрался: %s", err)
        return None

    # Хранение UTC, рендер локальный: конвертация живёт на краю.
    event = snap.get("next_event")
    if event:
        snap["next_event"] = {"kind": event["kind"],
                              "when": event["when"].astimezone(tz)}
    return snap
