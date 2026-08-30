"""Окно наружу, часть поисковая: то, чего нет ни в памяти, ни в датчиках.

Третий модуль-край после `sky` и `outside`, правило то же: **любая
неудача — `None`, а не исключение.** Ключа нет, сеть молчит, сервис
отказал — поиска сегодня нет, диалог живёт.

**Транспорт приходит параметром**, как в `outside.weather`. Официальный
SDK Tavily здесь сознательно не используется: он несёт свой транспорт на
`requests`, а тот по умолчанию подхватывает системные `HTTP_PROXY` /
`HTTPS_PROXY` — на этом мы уже обожглись. Запрос уходил через прокси,
прокси отдавал страницу 403, SDK превращал её в `ForbiddenError` **с
пустым текстом**, и отличить «фильтр на пути» от «протухший ключ» было
нечем. Свой `httpx.Client` с `trust_env=False` такого не допускает, а
цена — сорок строк разбора JSON.

**Ключ уходит двумя способами сразу** — заголовком `Bearer` и полем
`api_key` в теле. Tavily принимал обе формы в разное время; дублирование
дешевле, чем выяснять, какая актуальна сегодня.

**Здесь нет рендера.** Наружу — заголовок, ссылка, кусок текста; слова
живут в `mind`.

Автономная проверка:  `python web.py "что вчера случилось в Брянске"`
Она печатает не только результаты, но и диагностику ответа (код,
заголовок `server`, тип содержимого) — по ней сразу видно, ответило
приложение Tavily или фильтр на пути.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

SEARCH_URL = "https://api.tavily.com/search"

# Три — потолок, за которым блок в промпте начинает конкурировать с
# памятью персонажа за внимание модели.
MAX_RESULTS = 3

# Гигиена транспорта, не рендер: бортик на то, что входит в процесс.
SNIPPET_CHAR_LIMIT = 400

# Сколько живёт найденное. Раньше срока не было вовсе, и это читалось как
# «кэш умирает вместе с процессом» — верно для процесса длиной в разговор
# и неверно для демона (3c), который живёт месяцами: там бессрочный кэш
# превращается в вечную память о том, что нашлось в понедельник, причём
# память невидимую — ретривер отчитывался бы в лог «ответ из кэша», а
# персонаж уверенно пересказывал бы позапрошлую погоду.
#
# Час, а не двадцать минут, как у погоды: погода меняется сама, выдача
# поиска — когда меняется мир. Порог на глаз и правится на живом, как
# `RETRIEVER_COOLDOWN`.
SEARCH_TTL_MINUTES = 60.0

# Кэш живёт в процессе и умирает с ним — не память, а буфер (как
# `_weather_cache` в `outside`). Заодно предохранитель от повторного
# поиска одного и того же подряд. Значение — `(когда положили, результат)`.
_search_cache: dict[tuple, tuple[datetime, list[dict]]] = {}


def _clip(text: str, limit: int = SNIPPET_CHAR_LIMIT) -> str:
    s = (text or "").strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "..."


def _evict_stale(now: datetime, ttl_minutes: float = SEARCH_TTL_MINUTES) -> None:
    """Выбросить протухшее. Чистка на обращении, а не по таймеру.

    Двумя строками закрывается вторая половина той же беды: без срока кэш
    не только врал, но и рос без потолка — за месяц работы демона в нём
    осело бы всё, что персонаж когда-либо искал. Отрицательный возраст
    (часы прыгнули назад) считается негодным, как в `outside.weather`.
    """
    ttl = timedelta(minutes=ttl_minutes)
    for key, (at, _) in list(_search_cache.items()):
        if not timedelta(0) <= now - at < ttl:
            del _search_cache[key]


def _describe_refusal(response: httpx.Response) -> str:
    """Собрать из отказа то, что поможет отличить фильтр от сервиса.

    Пустое тело или HTML вместо JSON — улика: отвечало не приложение.
    Заголовок `server` обычно называет виновника прямо (`nginx` у
    фильтра против `awselb` у Tavily).
    """
    server = response.headers.get("server", "—")
    ctype = response.headers.get("content-type", "—")
    body = ""
    try:
        body = response.text.strip().replace("\n", " ")[:200]
    except Exception:
        pass
    if "json" not in ctype.lower():
        body = body or "тело пустое"
        body += "  <- не JSON: похоже, ответил не Tavily, а фильтр на пути"
    return f"server={server} | {ctype} | {body}"


def search(
    query: str,
    client: httpx.Client,
    api_key: str,
    max_results: int = MAX_RESULTS,
    topic: str | None = None,
    days: int | None = None,
    now: datetime | None = None,
) -> list[dict] | None:
    """Поисковый запрос -> список результатов. Любая неудача -> `None`.

    Возвращает `[{title, url, snippet}, ...]`, не более `max_results`.

    `topic="news"` вместе с `days` сужает выдачу до свежих новостей —
    ровно та ручка, которой не хватало обычной выдаче: без неё запрос
    «что вчера случилось в городе N» возвращает главные страницы
    порталов, чьи описания говорят «здесь бывают новости», а не что
    произошло. Пока не подключено к ретриверу: сперва надо увидеть, как
    Tavily справляется без этого.

    Отличие `None` от `[]` содержательное и доезжает до слов в `mind`:
    `None` — «поиска не было» (нет ключа, упала сеть, сервис отказал),
    `[]` — «искали, не нашли». Первое персонажу знать незачем, второе он
    вправе произнести («глянул — ничего внятного»).

    Ключ приходит параметром, а не читается из `config`: модуль-край не
    лезет в настройки проекта. `now` — оттуда же и затем же, зачем он у
    `outside.weather`: часы в этом проекте живут на краю и приходят
    параметром. Не передали — за «сейчас» берётся системное время, и это
    единственная поблажка, сделанная ради автономной проверки модуля.
    """
    q = (query or "").strip()
    if not q:
        return None

    if not api_key:
        # Штатная ситуация, не ошибка: ключа нет — проход выключен.
        logging.info("search: ключ не задан, поиск выключен")
        return None

    at = now or datetime.now(timezone.utc)
    _evict_stale(at)

    key = (q.lower(), max_results, topic, days)
    cached = _search_cache.get(key)
    if cached is not None:
        logging.info("search: ответ из кэша (%s)", q)
        return list(cached[1])

    body = {
        "api_key": api_key,
        "query": q,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
    }
    if topic:
        body["topic"] = topic
    if days:
        body["days"] = days

    try:
        response = client.post(
            SEARCH_URL,
            json=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
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

    raw = (payload or {}).get("results")
    if raw is None:
        logging.warning("search: в ответе нет блока results")
        return None
    if not isinstance(raw, list):
        logging.warning("search: results не список, а %s", type(raw).__name__)
        return None

    # Отбираем **до** среза, а не после: записи без ссылки или без текста
    # иначе съедали бы слоты, и три пустых результата впереди оставили бы
    # нас без единого годного.
    results = []
    for item in raw:
        if len(results) >= max_results:
            break
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        snippet = _clip(str(item.get("content") or ""))
        if not url or not snippet:
            continue
        results.append({
            "title": str(item.get("title") or "").strip() or url,
            "url": url,
            "snippet": snippet,
        })

    # Пустой список кэшируем тоже: повторять бесплодный запрос подряд
    # незачем.
    _search_cache[key] = (at, list(results))
    if not results:
        logging.info("search: ничего не нашлось (%s)", q)
    return results


def build_search_client(timeout: float = 8.0) -> httpx.Client:
    """Клиент для поиска: **мимо прокси проекта**, но со своим, если задан.

    `trust_env=False` — не смотреть в системные `HTTP_PROXY`/`HTTPS_PROXY`:
    именно они утаскивали запрос в прокси, который поисковые домены не
    пропускает. Свой прокси для поиска задаётся отдельной переменной
    `SEARCH_PROXY_URL`, чтобы два канала (модель и поиск) не путались.

    Живёт здесь, а не в `main`, чтобы автономная проверка ходила ровно
    тем же путём, что и программа: иначе «у меня скрипт работает, а в
    приложении нет» останется неразрешимым.
    """
    import os

    proxy = (os.getenv("SEARCH_PROXY_URL") or "").strip() or None
    return httpx.Client(
        timeout=httpx.Timeout(timeout),
        trust_env=False,
        follow_redirects=True,
        proxy=proxy,
    )


if __name__ == "__main__":
    # Автономная проверка: python web.py "что вчера случилось в Брянске"
    import os
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    key = os.getenv("TAVILY_API_KEY", "")
    proxy = (os.getenv("SEARCH_PROXY_URL") or "").strip()
    text = " ".join(sys.argv[1:]) or "новости сегодня"

    print(f"ключ:   {'задан, ' + str(len(key)) + ' симв, начинается с ' + key[:9] if key else 'НЕ ЗАДАН'}")
    print(f"прокси: {proxy or 'нет (идём напрямую)'}")
    print(f"запрос: {text}\n")

    with build_search_client() as c:
        found = search(text, c, key)

    if found is None:
        print("\nрезультат: None — поиска не было, причина в строке WARNING выше.")
    elif not found:
        print("\nрезультат: пусто — искали, ничего не нашли.")
    else:
        print(f"\nрезультат: {len(found)}")
        for f in found:
            print(f"\n  {f['title']}\n  {f['url']}\n  {f['snippet'][:220]}")
