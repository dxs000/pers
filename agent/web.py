"""Окно наружу, часть поисковая: то, чего нет ни в памяти, ни в датчиках.

Третий модуль-край после `sky` и `outside`, правило то же: **любая
неудача — `None`, а не исключение.** Ключа нет, сеть молчит, сервис
отказал — поиска сегодня нет, диалог живёт.

Ищем через Yandex Search API v2 (синхронный `/v2/web/search`).
Ответ — Base64 XML в `rawData`. Наружу тот же контракт, что был у
Tavily: `[{title, url, snippet}, ...]`.

Автономная проверка:  `python web.py "что вчера случилось в Брянске"`
"""

from __future__ import annotations

import base64
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"

MAX_RESULTS = 3
SNIPPET_CHAR_LIMIT = 400
SEARCH_TTL_MINUTES = 60.0

_search_cache: dict[tuple, tuple[datetime, list[dict]]] = {}


def _clip(text: str, limit: int = SNIPPET_CHAR_LIMIT) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit].rstrip() + "..."


def _evict_stale(now: datetime, ttl_minutes: float = SEARCH_TTL_MINUTES) -> None:
    ttl = timedelta(minutes=ttl_minutes)
    for key, (at, _) in list(_search_cache.items()):
        if not timedelta(0) <= now - at < ttl:
            del _search_cache[key]


def _describe_refusal(response: httpx.Response) -> str:
    server = response.headers.get("server", "—")
    ctype = response.headers.get("content-type", "—")
    body = ""
    try:
        body = response.text.strip().replace("\n", " ")[:200]
    except Exception:
        pass
    if "json" not in ctype.lower():
        body = (body or "тело пустое") + "  <- не JSON"
    return f"server={server} | {ctype} | {body}"


def _text(el) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _parse_yandex_xml(xml_text: str, max_results: int) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as err:
        logging.warning("search: XML не разобрался: %s", err)
        return []
    results = []
    for doc in root.iter():
        tag = doc.tag.split("}")[-1].lower()
        if tag != "doc":
            continue
        fields = {}
        passages = []
        for child in list(doc):
            name = child.tag.split("}")[-1].lower()
            if name == "url":
                fields["url"] = _text(child)
            elif name == "title":
                fields["title"] = _text(child)
            elif name in ("headline", "modtime"):
                fields.setdefault(name, _text(child))
            elif name == "passages":
                for p in child:
                    if p.tag.split("}")[-1].lower() == "passage":
                        passages.append(_text(p))
        url = (fields.get("url") or "").strip()
        snippet = _clip(" ".join(passages) or fields.get("headline") or "")
        if not url or not snippet:
            continue
        results.append({
            "title": (fields.get("title") or "").strip() or url,
            "url": url,
            "snippet": snippet,
        })
        if len(results) >= max_results:
            break
    return results


def search(
    query: str,
    client: httpx.Client,
    api_key: str,
    max_results: int = MAX_RESULTS,
    topic: str | None = None,
    days: int | None = None,
    now: datetime | None = None,
    folder_id: str | None = None,
) -> list[dict] | None:
    """Поисковый запрос -> список результатов. Любая неудача -> `None`.

    Возвращает `[{title, url, snippet}, ...]`, не более `max_results`.
    `topic="news"` или `days<=1` — PERIOD_DAY и сортировка по времени.
    """
    q = (query or "").strip()
    if not q:
        return None

    folder = (folder_id or os.getenv("YANDEX_FOLDER_ID") or "").strip()
    if not api_key or not folder:
        logging.info("search: нет ключа или YANDEX_FOLDER_ID — поиск выключен")
        return None

    at = now or datetime.now(timezone.utc)
    _evict_stale(at)

    key = (q.lower(), max_results, topic, days)
    cached = _search_cache.get(key)
    if cached is not None:
        logging.info("search: ответ из кэша (%s)", q)
        return list(cached[1])

    fresh = (topic or "").lower() == "news" or (days is not None and days <= 2)
    body = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": q,
            "familyMode": "FAMILY_MODE_NONE",
            "fixTypoMode": "FIX_TYPO_MODE_ON",
        },
        "folderId": folder,
        "responseFormat": "FORMAT_XML",
        "l10n": "LOCALIZATION_RU",
        "maxPassages": "2",
        "groupSpec": {
            "groupMode": "GROUP_MODE_DEEP",
            "groupsOnPage": str(max(max_results, 3)),
            "docsInGroup": "1",
        },
    }
    if fresh:
        body["period"] = "PERIOD_DAY"
        body["sortSpec"] = {
            "sortMode": "SORT_MODE_BY_TIME",
            "sortOrder": "SORT_ORDER_DESC",
        }

    try:
        response = client.post(
            SEARCH_URL,
            json=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Api-Key {api_key}",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as err:
        logging.warning("search: сервис отказал (%s): HTTP %s | %s",
                        q, err.response.status_code, _describe_refusal(err.response))
        return None
    except httpx.HTTPError as err:
        logging.warning("search: запрос упал (%s): %s: %s",
                        q, type(err).__name__, err)
        return None
    except ValueError as err:
        logging.warning("search: ответ не разобрался (%s): %s", q, err)
        return None

    raw = (payload or {}).get("rawData")
    if not raw:
        logging.warning("search: в ответе нет rawData")
        return None
    try:
        xml_text = base64.b64decode(raw).decode("utf-8", errors="replace")
    except (ValueError, TypeError) as err:
        logging.warning("search: rawData не декодируется: %s", err)
        return None

    results = _parse_yandex_xml(xml_text, max_results)
    _search_cache[key] = (at, list(results))
    if not results:
        logging.info("search: ничего не нашлось (%s)", q)
    else:
        logging.info("search: яндекс, %s шт. (%s)", len(results), q)
    return results


def build_search_client(timeout: float = 15.0) -> httpx.Client:
    proxy = (os.getenv("SEARCH_PROXY_URL") or "").strip() or None
    return httpx.Client(
        timeout=httpx.Timeout(timeout),
        trust_env=False,
        follow_redirects=True,
        proxy=proxy,
    )


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    key = (
        os.getenv("YANDEX_SEARCH_API_KEY")
        or os.getenv("SEARCH_API_KEY")
        or os.getenv("TAVILY_API_KEY")
        or ""
    )
    folder = os.getenv("YANDEX_FOLDER_ID", "")
    proxy = (os.getenv("SEARCH_PROXY_URL") or "").strip()
    text = " ".join(sys.argv[1:]) or "новости сегодня"
    print(f"ключ:   {'задан, ' + str(len(key)) + ' симв.' if key else 'НЕ ЗАДАН'}")
    print(f"folder: {folder or 'НЕ ЗАДАН'}")
    print(f"прокси: {proxy or 'нет'}")
    print(f"запрос: {text}\n")
    with build_search_client() as c:
        found = search(text, c, key, topic="news", days=1)
    if found is None:
        print("\nрезультат: None — поиска не было.")
    elif not found:
        print("\nрезультат: пусто — искали, ничего не нашли.")
    else:
        print(f"\nрезультат: {len(found)}")
        for f in found:
            print(f"\n  {f['title']}\n  {f['url']}\n  {f['snippet'][:220]}")
