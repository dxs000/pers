"""HTTP-контур инспектора: тонкий вид над `inspector.py` (Шаг 31).

Шаг 30 сделал читающий контракт и оставил один незакрытый вопрос — чем эти
структуры доедут до браузера. Здесь он закрыт, и вместе с ним названо
решение, которое роадмап держал умолчанием.

## Почему по HTTP говорит Python, а не Express

4a-1 звучала как «React + Express поверх готового контракта: взял
структуру, отдал JSON». Взять питоновскую структуру Express не может. У
него ровно два пути, и оба плохи:

- **ходить в базу самому** — тогда семь запросов, порог отсечки, предел
  числа, отбор фактов в промпт и три состояния очереди появляются ВТОРОЙ
  копией в JS. Ровно то, ради отсутствия чего писался Шаг 30, и та копия,
  которую сбруя не удержала бы по устройству: `golden.py` сравнивает
  байты, до Node ей не дотянуться;
- **звать Python по HTTP** — тогда между Node и Python появляется прямой
  HTTP, который 4b отвергает поимённо: у них уже есть общая точка встречи,
  и она транзакционная.

Отсюда: JSON отдаёт тот, у кого есть структуры, — Python. Роль Node в 4a
сжимается до раздачи статики React, а настоящая его работа откладывается
до 4b, где он мост к WebSocket. «PERN» при этом честнее назвать
«P + Python API + React», чем тащить Express ради буквы.

## Что здесь есть и чего нет

**Есть:** восемь маршрутов один-в-один с выдачами инспектора, разбор двух
параметров (`at`, `limit`), внятные коды отказа и WSGI-приложение,
которое сбруя зовёт БЕЗ сокета.

**Нет доменного знания.** Ни одного `SELECT`, ни одного порога, ни одной
причины отсечки. Тело обработчика — вызов `inspector.*`; если однажды в
этом файле появится `if eff < ...`, значит копия всё-таки завелась, просто
на другом языке.

**Нет записи.** Не обещанием: соединение открывает `inspector.connect`,
то есть `default_transaction_read_only`, и `POST` отклоняется маршрутом
раньше, чем дошёл бы до базы.

**Нет CORS.** Это решение, а не забывчивость: заголовок, разрешающий
чужому origin читать память персонажа, — вещь, которую заводят вместе с
аутентификацией (4c), а не «чтобы Vite не ругался». Дев-сервер витрины
ходит сюда прокси-правилом, прод — тем же origin, что и статика.

**Нет соединения на процесс.** Открывается на запрос и закрывается вместе
с ним. Read-only соединение дёшево, а долгоживущее в вебе — это пул и
вопрос «чья транзакция чья», на который до Фазы 5 нечем ответить.

**Нет стриминга, чата, WebSocket и статики.** Витрина — следующий шаг.

## Валюта времени

`now` приезжает от края (`clock`), а не берётся внутри, ровно по правилу
`mind` и `inspector`: нечистое живёт на краю. Отсюда же и `?at=` — тем же
приёмом страница промпта показывает память на любой момент, а сбруя
получает детерминированный ответ.
"""

import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs

import inspector

JSON_TYPE = "application/json; charset=utf-8"

# Тело всегда заканчивается переводом строки: ответ, прочитанный `curl` без
# `-o`, не должен склеиваться с приглашением оболочки. Форма та же, что у
# артефактов сбруи (`indent=2`, `ensure_ascii=False`), и это не совпадение —
# сценарий `http` сверяет тело с прямым вызовом инспектора побайтно.
_DUMP = dict(ensure_ascii=False, indent=2)


class _BadRequest(Exception):
    """Параметр разобрать нельзя. 400, и с объяснением, что именно не так."""


# =============================================================================
# Обработчики: каждый — одна строка поверх инспектора
# =============================================================================
def _overview(conn, args):
    return inspector.overview(conn, args["now"])


def _objects(conn, args):
    return inspector.objects(conn, args["now"])


def _facts(conn, args):
    """Факты объекта. Несуществующий объект — пустой список, а не 404.

    404 означал бы «такого объекта нет», а узнать это отдельным запросом
    инспектор не предлагает, и заводить ради вежливости кода восьмую выдачу
    значило бы завести метод, чьё поведение никто не проверял. Пустой
    список честен: фактов у этого id нет — ни одного.
    """
    return inspector.facts(conn, args["id"])


def _episodes(conn, args):
    return inspector.episodes(conn, args["limit"])


def _sessions(conn, args):
    return inspector.sessions(conn, args["limit"])


def _messages(conn, args):
    return inspector.conversation(conn, args["id"], args["limit"])


def _queue(conn, args):
    return inspector.queue(conn, args["limit"])


def _prompt(conn, args):
    return inspector.prompt(conn, args["now"], args["tz"])


# Маршрут — путь, обработчик и набор параметров, которые он умеет читать.
# Параметры перечислены явно, а не разбираются все подряд: `?limit=` на
# странице промпта означал бы ручку, которой нет, — то самое обещание, от
# которого предостерегает дисциплина `config`.
ROUTES = (
    (re.compile(r"^/api/overview$"), _overview, ("now",)),
    (re.compile(r"^/api/objects$"), _objects, ("now",)),
    (re.compile(r"^/api/objects/(?P<id>\d+)/facts$"), _facts, ()),
    (re.compile(r"^/api/episodes$"), _episodes, ("limit",)),
    (re.compile(r"^/api/sessions$"), _sessions, ("limit",)),
    (re.compile(r"^/api/sessions/(?P<id>\d+)/messages$"), _messages, ("limit",)),
    (re.compile(r"^/api/queue$"), _queue, ("limit",)),
    (re.compile(r"^/api/prompt$"), _prompt, ("now", "tz")),
)


# =============================================================================
# Разбор параметров
# =============================================================================
def _parse_at(raw: str, clock) -> datetime:
    """`?at=` — момент, на который показывать память. По умолчанию — сейчас.

    Наивная метка отклоняется, а не достраивается поясом. Достроить её
    молча значило бы отдать человеку страницу на момент, о котором он не
    просил: `psycopg` истолковал бы её поясом СЕРВЕРА, а тот с поясом
    персонажа не обязан совпадать — расхождение ровно того рода, что уже
    дважды поймал переезд (пояс базы, локаль базы).
    """
    if not raw:
        return clock()
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        raise _BadRequest(f"at: не разбирается как ISO-8601: {raw!r}") from None
    if at.tzinfo is None:
        raise _BadRequest(f"at: нужен пояс, иначе момент неоднозначен: {raw!r}")
    return at.astimezone(timezone.utc)


def _parse_limit(raw: str) -> int | None:
    """`?limit=` — сколько строк. Нет параметра — все, как у инспектора."""
    if not raw:
        return None
    try:
        limit = int(raw)
    except ValueError:
        raise _BadRequest(f"limit: нужно целое, получено {raw!r}") from None
    if limit < 1:
        raise _BadRequest(f"limit: нужно положительное, получено {limit}")
    return limit


def _collect(names, match, query, clock, tz) -> dict:
    args = {}
    if "id" in match:
        args["id"] = int(match["id"])
    if "now" in names:
        args["now"] = _parse_at(query.get("at", [""])[0], clock)
    if "tz" in names:
        args["tz"] = tz
    if "limit" in names:
        args["limit"] = _parse_limit(query.get("limit", [""])[0])
    return args


# =============================================================================
# Приложение
# =============================================================================
def _respond(start_response, status: str, payload, headers=()):
    body = (json.dumps(payload, **_DUMP) + "\n").encode("utf-8")
    start_response(status, [
        ("Content-Type", JSON_TYPE),
        ("Content-Length", str(len(body))),
        *headers,
    ])
    return [body]


def build_app(*, connect=None, clock=None, tz=None):
    """Собрать WSGI-приложение. Края приезжают параметрами.

    Фабрика, а не модульное приложение, ровно затем же, зачем `Edges` в
    `cycle` (Шаг 27): соединение, часы и пояс — края процесса, и сбруе они
    нужны заглушенными. Приложение, читающее `datetime.now()` и `config.TZ`
    изнутри, эталоном не проверяется вовсе.

    `config` импортируется ЛЕНИВО и только ради пояса по умолчанию: он
    тянет `httpx` и `dotenv`, а вид над инспектором обязан подниматься и
    там, где ни того, ни другого нет.
    """
    connect = connect or (lambda: inspector.connect())
    clock = clock or (lambda: datetime.now(timezone.utc))
    if tz is None:
        import config

        tz = config.TZ

    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "") or "/"
        query = parse_qs(environ.get("QUERY_STRING", ""))

        route = next((r for r in ROUTES if r[0].match(path)), None)
        if route is None:
            return _respond(start_response, "404 Not Found",
                            {"error": "нет такого маршрута", "path": path})

        # Метод проверяется ПОСЛЕ пути, чтобы 405 доставался только
        # существующим маршрутам: «такого адреса нет» и «сюда так нельзя» —
        # разные ответы, и человек с опечаткой в пути должен получить
        # первый, а не второй.
        if method != "GET":
            return _respond(start_response, "405 Method Not Allowed",
                            {"error": "только GET", "method": method},
                            headers=[("Allow", "GET")])

        pattern, handler, names = route
        try:
            args = _collect(names, pattern.match(path).groupdict(), query,
                            clock, tz)
        except _BadRequest as err:
            return _respond(start_response, "400 Bad Request", {"error": str(err)})

        conn = connect()
        try:
            payload = handler(conn, args)
        except Exception as err:
            # Наружу — класс исключения, а не текст. Текст ошибки Postgres
            # локализуется настройкой сервера (`lc_messages`), то есть
            # зависит от площадки; в лог он идёт целиком, в ответ — нет.
            logging.exception("запрос не удался: %s %s", method, path)
            return _respond(start_response, "500 Internal Server Error",
                            {"error": "запрос не удался", "kind": type(err).__name__})
        finally:
            # Закрывается ВСЕГДА. Соединение, пережившее запрос, доживёт до
            # первой длинной блокировки и станет тем, из-за чего сбруя
            # замирала на `TRUNCATE` (см. `golden.open_engine`).
            conn.close()

        return _respond(start_response, "200 OK", payload)

    return app


def main() -> int:
    """Поднять сервер. Слушает петлю, а не сеть.

    `127.0.0.1` по умолчанию — не осторожность, а исполнение 4c: наружу
    сервис выходит через реверс-прокси с TLS и аутентификацией, и до тех
    пор в состоянии лежит сырой текст диалога, доступный любому, кто дотянется
    до порта.
    """
    import argparse
    from wsgiref.simple_server import make_server

    parser = argparse.ArgumentParser(
        description="Инспектор по HTTP: только чтение, только JSON.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

    with make_server(args.host, args.port, build_app()) as srv:
        logging.info("инспектор слушает http://%s:%s/api/overview",
                     args.host, args.port)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            logging.info("остановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
