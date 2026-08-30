"""За окном, часть сетевая: то, что нельзя посчитать, а надо спросить.

Пара к `sky`. Тот считает небо из широты и даты — офлайн, без режима
отказа. Этот ходит в сеть, и потому у него режим отказа есть всегда:
**любая неудача возвращает `None`, а не исключение.** Сети нет, сервис
молчит, ответ не разобрался — блок среды просто беднее, диалог живёт.
Тот же принцип, что у трёх предохранителей служебных LLM-проходов.

Клиент приходит параметром, как `client` в `mind.reflect_mood`: модуль
не строит транспорт и не знает про прокси — этим занимается `config`,
а владеет клиентом `main`. Часов модуль не дёргает: `now` параметром.

**Здесь нет рендера.** Наружу — числа, коды и метки; слова живут в `mind`.

Open-Meteo: ключа не требует, лимитов для одиночных запросов нет.
"""

import logging
from datetime import datetime, timedelta

import httpx

GEOCODER_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Язык ответа геокодера. Канонические имена приезжают сразу по-русски
# («Хельсинки», а не «Helsinki») — персонаж говорит по-русски, и незачем
# заставлять его переводить название города самостоятельно.
GEOCODER_LANGUAGE = "ru"

# Что спрашиваем о текущей погоде. Больше, чем поедет в промпт: лишнее
# нужно Фазе 4 (резкое похолодание продавливает порог желания говорить).
CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,precipitation,"
    "weather_code,cloud_cover,wind_speed_10m"
)

# Погода не меняется за реплику. TTL — единственное, что отделяет диалог
# от сетевого запроса на каждый ход.
WEATHER_TTL_MINUTES = 20.0

# Кэш живёт в процессе и умирает с ним: это не память, а сиюминутный
# буфер. Ключ — округлённые координаты (переезд на соседнюю улицу не
# должен считаться новым местом).
_weather_cache: dict[tuple, tuple[datetime, dict]] = {}


def geocode(name: str, client: httpx.Client, language: str = GEOCODER_LANGUAGE) -> dict | None:
    """Имя места -> координаты и канонические имена. Не нашлось / сеть молчит -> `None`.

    Возвращает:
        `{lat, lon, label, country, admin1, timezone, source}`

    `label` — то, как место называет **сервис**, а не то, что ввёл человек.
    Разница между ними — единственный способ заметить опечатку: геокодер
    отвечает почти всегда, и на «Хелsinki» он вернёт что-нибудь
    правдоподобное. Сравнение оставлено вызывающему: здесь только факты.
    """
    query = (name or "").strip()
    if not query:
        return None

    try:
        response = client.get(
            GEOCODER_URL,
            params={"name": query, "count": 1, "language": language, "format": "json"},
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as err:
        logging.warning("geocode: запрос упал (%s): %s", query, err)
        return None
    except ValueError as err:  # тело не JSON
        logging.warning("geocode: ответ не разобрался (%s): %s", query, err)
        return None

    if not isinstance(payload, dict):
        logging.warning("geocode: ожидался объект, пришёл %s", type(payload).__name__)
        return None

    results = payload.get("results")
    if not results:
        # Штатный ответ: такого места сервис не знает. Не ошибка.
        logging.info("geocode: место не найдено: %s", query)
        return None

    first = results[0]
    if not isinstance(first, dict):
        logging.warning("geocode: неожиданная форма результата для %s", query)
        return None

    try:
        lat = float(first["latitude"])
        lon = float(first["longitude"])
    except (KeyError, TypeError, ValueError):
        logging.warning("geocode: в ответе нет координат для %s", query)
        return None

    label = str(first.get("name") or "").strip() or query

    return {
        "lat": lat,
        "lon": lon,
        "label": label,
        "country": str(first.get("country") or "").strip() or None,
        "admin1": str(first.get("admin1") or "").strip() or None,
        "timezone": str(first.get("timezone") or "").strip() or None,
        "source": "open-meteo",
    }


def _as_float(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def weather(
    lat: float,
    lon: float,
    now: datetime,
    client: httpx.Client,
    ttl_minutes: float = WEATHER_TTL_MINUTES,
) -> dict | None:
    """Текущая погода в точке. Сеть молчит и кэша нет -> `None`.

    Кэш на `ttl_minutes`: погода не меняется за реплику, а без TTL сетевой
    запрос уходил бы на каждый ход. `now` приходит параметром — часов
    модуль не дёргает, как и `sky`.

    Запрос упал, но в кэше что-то лежит — **отдаём протухшее** с меткой
    `stale`. Часовой давности температура ближе к правде, чем молчание;
    решать, стоит ли её произносить, будет слой рендера.
    """
    key = (round(_as_float(lat) or 0.0, 2), round(_as_float(lon) or 0.0, 2))
    cached = _weather_cache.get(key)

    if cached is not None:
        fetched_at, payload = cached
        age = now - fetched_at
        # Отрицательный возраст — часы прыгнули назад; считаем кэш негодным.
        if timedelta(0) <= age < timedelta(minutes=ttl_minutes):
            return dict(payload, stale=False)

    try:
        response = client.get(
            FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": CURRENT_FIELDS,
                "timezone": "UTC",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as err:
        logging.warning("weather: запрос упал: %s", err)
        return _stale(cached)
    except ValueError as err:
        logging.warning("weather: ответ не разобрался: %s", err)
        return _stale(cached)

    current = payload.get("current") if isinstance(payload, dict) else None
    if not isinstance(current, dict):
        logging.warning("weather: в ответе нет блока current")
        return _stale(cached)

    code = current.get("weather_code")
    data = {
        "temperature": _as_float(current.get("temperature_2m")),
        "apparent": _as_float(current.get("apparent_temperature")),
        "precipitation": _as_float(current.get("precipitation")),
        "cloud": _as_float(current.get("cloud_cover")),
        "wind": _as_float(current.get("wind_speed_10m")),
        "code": int(code) if isinstance(code, (int, float)) else None,
        "observed_at": current.get("time"),
        "source": "open-meteo",
    }

    # Пустой ответ (все поля None) кэшировать незачем — это не погода.
    if data["temperature"] is None and data["code"] is None:
        logging.warning("weather: ответ без температуры и кода")
        return _stale(cached)

    _weather_cache[key] = (now, data)
    return dict(data, stale=False)


def _stale(cached) -> dict | None:
    """Протухшее лучше пустого: сеть отвалилась, но погода час назад известна."""
    if cached is None:
        return None
    _, payload = cached
    return dict(payload, stale=True)
