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

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_MODEL_LIGHT = os.getenv("DEEPSEEK_MODEL_LIGHT", DEEPSEEK_MODEL)
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "60.0"))

YANDEX_SEARCH_API_KEY = (
    os.getenv("YANDEX_SEARCH_API_KEY")
    or os.getenv("SEARCH_API_KEY")
    or os.getenv("TAVILY_API_KEY")
    or ""
).strip()
YANDEX_FOLDER_ID = (os.getenv("YANDEX_FOLDER_ID") or "").strip()
TAVILY_API_KEY = YANDEX_SEARCH_API_KEY

DATABASE_URL = os.getenv("DATABASE_URL", "")
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


def require_dsn(test: bool = False) -> str:
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
    if not a or not b:
        return False
    return a.strip().rstrip("/") == b.strip().rstrip("/")

APP_NAME = os.getenv("APP_NAME", "App")
USER_PROMPT = os.getenv("USER_PROMPT", "User >")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "System >")
EXIT_WORD = os.getenv("EXIT_WORD", "exit")

TZ_SPEC = os.getenv("APP_TZ", "").strip()
TZ = parse_tz(TZ_SPEC)


def _coord(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logging.warning("config: не разобрал %s: %r", name, raw)
        return None


APP_LAT = _coord("APP_LAT")
APP_LON = _coord("APP_LON")
APP_PLACE = os.getenv("APP_PLACE", "").strip() or None

LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", BASE_DIR / "library")).expanduser()
OUTBOX_DIR = Path(os.getenv("OUTBOX_DIR", BASE_DIR / "outbox")).expanduser()
_log_raw = (os.getenv("AGENT_LOG") or "").strip()
AGENT_LOG = Path(_log_raw).expanduser() if _log_raw else (BASE_DIR / "var" / "agent.log")

_LIMITS = httpx.Limits(
    max_keepalive_connections=5,
    max_connections=10,
    keepalive_expiry=30.0,
)

_DISABLED = ("", "none", "null", "false")


def proxy_url() -> str | None:
    raw = (os.getenv("PROXY_URL") or "").strip()
    return raw if raw and raw.lower() not in _DISABLED else None


def _client_kwargs(timeout: float | None = None) -> dict:
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
    return httpx.Client(limits=_LIMITS, **_client_kwargs(timeout))


def require_api_key() -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("Не задан DEEPSEEK_API_KEY")
    return DEEPSEEK_API_KEY
