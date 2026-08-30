"""Настройки запуска: ключи, площадка, часовой пояс, фабрика http-клиента.

Дисциплина модуля (Шаг 16):
    - **Ничего не печатает.** У демона (3c) stdout никто не читает, и
      `print` из библиотечного места — не косметика, а потерянная
      диагностика. Всё, что модуль хочет сказать, идёт в `logging`.
    - **Ничего мёртвого.** Переменная, которую никто не читает, — это
      обещание ручки, которой нет: правишь `.env`, ничего не меняется,
      и виноват в этом кто угодно, кроме конфига.
"""

import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from timeutil import parse_tz

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# `STATE_PATH` здесь стоял до Шага 26. Состояние жило файлом на диске;
# теперь оно живёт в Postgres, и путь к файлу стал бы ровно тем мёртвым,
# от которого предостерегает докстринг модуля: обещанием ручки, которой нет.

# =============================================================================
# LLM
# =============================================================================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# Дефолт обязан быть рабочим именем модели, а не заглушкой: на машине без
# `.env` первый же запрос уходит именно с ним, и опечатка вылезает
# невнятной ошибкой провайдера в момент, когда отлаживаешь совсем другое.
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "60.0"))

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# =============================================================================
# Postgres
# =============================================================================
# Рабочая база. В ней живёт память персонажа.
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Тестовая база — ОТДЕЛЬНАЯ, и это не педантизм. `store_pg.load_fixture`
# начинается с `TRUNCATE`: сбруя обязана стартовать с известного состояния,
# иначе прогон зависит от того, что осталось от прошлого. Смотри она в
# рабочую базу — `golden.py --check` стирал бы память персонажа, и стирал
# бы молча, потому что команда выглядит как проверка.
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


def require_dsn(test: bool = False) -> str:
    """Строка подключения или внятная ошибка. По лекалу `require_api_key`.

    При `test=True` дополнительно требует, чтобы тестовая база НЕ совпадала
    с рабочей. Совпадение — не «странная настройка», а команда на снос
    памяти, и запускать её по недосмотру нельзя.
    """
    dsn = TEST_DATABASE_URL if test else DATABASE_URL
    if not dsn:
        name = "TEST_DATABASE_URL" if test else "DATABASE_URL"
        raise RuntimeError(f"Не задан {name} (см. .env.example)")
    if test and _same_database(dsn, DATABASE_URL):
        raise RuntimeError(
            "TEST_DATABASE_URL совпадает с DATABASE_URL. Сбруя начинает с "
            "TRUNCATE — на рабочей базе это снос памяти персонажа. "
            "Заведите отдельную базу: createdb persona_test"
        )
    return dsn


def _same_database(a: str, b: str) -> bool:
    """Одна ли это база. Сравнение грубое и нарочно осторожное: DSN можно
    записать десятком способов, и точное сравнение легко обмануть, поэтому
    достаточное подозрение считается совпадением."""
    if not a or not b:
        return False
    return a.strip().rstrip("/") == b.strip().rstrip("/")

# =============================================================================
# Оболочка
# =============================================================================
APP_NAME = os.getenv("APP_NAME", "App")
USER_PROMPT = os.getenv("USER_PROMPT", "User >")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "System >")
EXIT_WORD = os.getenv("EXIT_WORD", "exit")

# Часовой пояс рендера. Числовое смещение работает всегда; имя зоны
# ("Asia/Tbilisi") — только если в системе есть база tzdata. На голом
# сервере её может не быть, и `parse_tz` тогда молча вернёт дефолт:
# согласованность пояса с местом проверяет `main.check_timezone`.
TZ = parse_tz(os.getenv("APP_TZ", "3"))


def _coord(name: str) -> float | None:
    """Координата из окружения. Не задана или мусор -> None: место —
    свойство объекта №0, а не обязательная настройка. Нет координат —
    блок среды просто не появляется, диалог живёт."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logging.warning("config: не разобрал %s: %r", name, raw)
        return None


# Посев места. Источник истины после первого запуска — state["self"]["place"],
# отсюда значение только доливается через ensure_self (как name/traits).
# Часовой пояс APP_TZ обязан быть согласован с этими координатами.
APP_LAT = _coord("APP_LAT")
APP_LON = _coord("APP_LON")
APP_PLACE = os.getenv("APP_PLACE", "").strip() or None

# =============================================================================
# HTTP
# =============================================================================
_LIMITS = httpx.Limits(
    max_keepalive_connections=5,
    max_connections=10,
    keepalive_expiry=30.0,
)

_DISABLED = ("", "none", "null", "false")


def proxy_url() -> str | None:
    """Прокси проекта из `.env` или None.

    Раньше отсюда возвращался словарь `{"http://": url}`, из которого
    вызывающий брал первое значение. Форма намекала на пер-схемное
    проксирование, которого не было ни дня; свёрнуто в строку.

    Поиск ходит **не через этот прокси**: у него своя фабрика
    (`web.build_search_client`) и своя переменная `SEARCH_PROXY_URL` —
    поисковые домены прокси проекта не пропускает.
    """
    raw = (os.getenv("PROXY_URL") or "").strip()
    return raw if raw and raw.lower() not in _DISABLED else None


def _client_kwargs(timeout: float | None = None) -> dict:
    """Общие параметры httpx-клиента: таймаут, прокси, редиректы.

    `trust_env=False` — системные HTTP_PROXY/HTTPS_PROXY игнорируются
    сознательно: канал задаётся только своей переменной, иначе на чужой
    машине запрос уезжает неизвестно куда.
    """
    kwargs = dict(
        timeout=httpx.Timeout(REQUEST_TIMEOUT if timeout is None else timeout),
        trust_env=False,
        follow_redirects=True,
    )
    proxy = proxy_url()
    if proxy:
        kwargs["proxy"] = proxy
        logging.info("прокси: %s", proxy)
    else:
        logging.info("прокси не настроен — прямое соединение")
    return kwargs


def get_sync_client(timeout: float | None = None) -> httpx.Client:
    """Фабрика синхронного клиента. Требует httpx >= 0.28 (`proxy=`, не `proxies=`)."""
    return httpx.Client(limits=_LIMITS, **_client_kwargs(timeout))


# =============================================================================
# Валидация окружения
# =============================================================================
def require_api_key() -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("Не задан DEEPSEEK_API_KEY")
    return DEEPSEEK_API_KEY
