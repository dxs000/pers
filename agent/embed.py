"""Векторы смысла: Yandex AI Studio, Text Embeddings v2 (Шаг 58).

Правило модуля-края то же, что у `web`, `sky`, `outside`: **любая неудача —
`None`, а не исключение.** Нет ключа, сеть молчит, сервис отказал — память
работает как раньше, по весу и давности, а не падает.

Модели две, и это не избыточность. Документ (воспоминание) и запрос (реплика
собеседника) векторизуются РАЗНЫМИ моделями, обученными в паре: короткая
реплика и длинная сцена иначе не попадают в одно пространство. Но воспоминание
с воспоминанием (дубль при вспоминании) сравниваются моделью документа с обеих
сторон — это сравнение двух сцен, а не поиск.

Автономная проверка ключа и порогов:
    python agent.py --embed "первый класс, ранец"
"""

from __future__ import annotations

import logging
import math
import os
import time

import httpx

import config

log = logging.getLogger("embed")

EMBED_URL = os.getenv(
    "EMBED_URL", "https://ai.api.cloud.yandex.net/foundationModels/v1/textEmbedding")
DOC_MODEL = os.getenv("EMBED_DOC_MODEL", "text-embeddings-v2-doc/latest")
QUERY_MODEL = os.getenv("EMBED_QUERY_MODEL", "text-embeddings-v2-query/latest")
TEXT_LIMIT = 2000   # воспоминание — одна-две фразы; длиннее — сбой, а не сцена

# Темп (Шаг 58.1). Квота Яндекса по умолчанию — 10 запросов в секунду на
# эмбеддинги, и первый досчёт живой биографии упёрся в неё на одиннадцатой
# строке: пачка шла без пауз. Держим чуть ниже квоты, чтобы соседний вызов
# (реплика в разговоре) не получил 429 из-за фонового досчёта.
EMBED_RPS = float(os.getenv("EMBED_RPS", "8"))
MIN_INTERVAL = 1.0 / EMBED_RPS if EMBED_RPS > 0 else 0.0
# Сколько ждать перед единственным повтором после 429. Секунда — окно квоты.
RETRY_AFTER = 1.0
_last_call = 0.0


def _pace() -> None:
    global _last_call
    delay = _last_call + MIN_INTERVAL - time.monotonic()
    if delay > 0:
        time.sleep(delay)
    _last_call = time.monotonic()


def model_tag(kind: str) -> str:
    """Что пишется в `memories.embedding_model`. Запрос сравнивается с
    документами, поэтому тег хранится только у документной модели."""
    return DOC_MODEL if kind == "doc" else QUERY_MODEL


def enabled(edges) -> bool:
    return bool(getattr(edges, "ai_key", "") and config.YANDEX_FOLDER_ID
                and getattr(edges, "search", None) is not None)


def embed(text: str, kind: str, edges) -> list[float] | None:
    """Текст -> нормированный вектор. `kind` — 'doc' или 'query'."""
    if not enabled(edges):
        return None
    body = " ".join((text or "").split())[:TEXT_LIMIT]
    if not body:
        return None
    uri = f"emb://{config.YANDEX_FOLDER_ID}/{model_tag(kind)}"
    try:
        for attempt in (1, 2):
            _pace()
            response = edges.search.post(
                EMBED_URL,
                json={"modelUri": uri, "text": body},
                headers={"Authorization": f"Api-Key {edges.ai_key}",
                         "x-folder-id": config.YANDEX_FOLDER_ID},
            )
            # 429 — не отказ, а «не так быстро»: один повтор через окно квоты.
            # Второй 429 уже отказ: значит, рядом идёт кто-то ещё, и ждать
            # дальше в фоновом тике незачем — досчитает следующий.
            if getattr(response, "status_code", 200) == 429 and attempt == 1:
                log.info("эмбеддинг: 429, повтор через %.1f с", RETRY_AFTER)
                time.sleep(RETRY_AFTER)
                continue
            break
        response.raise_for_status()
        vec = (response.json() or {}).get("embedding")
    except httpx.HTTPStatusError as err:
        log.warning("эмбеддинг: сервис отказал: HTTP %s | %s",
                    err.response.status_code, err.response.text[:200])
        return None
    except (httpx.HTTPError, ValueError) as err:
        log.warning("эмбеддинг: запрос упал: %s", err)
        return None
    if not vec:
        log.warning("эмбеддинг: в ответе нет вектора")
        return None
    return normalize([float(x) for x in vec])


def normalize(vec: list[float]) -> list[float] | None:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return None
    return [x / norm for x in vec]


# =============================================================================
# Фоновый проход: досчитать векторы биографии
# =============================================================================
# Без модели и без заслонок времени: вызов дешёвый (сотые доли копейки за
# строку), и досчитывать надо сразу, как появилась новая строка — иначе
# свежее воспоминание не найдётся по смыслу ровно тогда, когда о нём говорят.
# Пачка ограничена, чтобы первый запуск на биографии в тысячу строк не занял
# тик на минуты: досчитается за несколько тиков.
EMBED_BATCH = 20


def embed_tick(eng, edges, *, batch: int = EMBED_BATCH) -> int | None:
    """Досчитать до `batch` векторов. `None` — нечего или нечем."""
    if not enabled(edges):
        return None
    pending = eng.memories_to_embed(DOC_MODEL, batch)
    if not pending:
        return None
    done = 0
    for m in pending:
        vec = embed(m["text"], "doc", edges)
        if vec is None:
            break   # сеть легла — остальное досчитает следующий тик
        with eng.unit():
            eng.set_memory_embedding(m["id"], vec, DOC_MODEL)
        done += 1
    if done:
        log.info("векторы: досчитано %s из %s", done, len(pending))
    return done or None
