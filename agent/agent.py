"""Демон: процесс, который живёт, пока включён сервер (Шаг 29).

`main.py` расщеплён надвое. Сюда переехало всё, что делает персонажа
персонажем, — петля, разрешение места, границы сессии; в `cli.py` осталась
оболочка, и она теперь просто клиент. **Переехало целиком, а не копией:**
`resolve_place` и `finish_session` тут те же функции, что были в `main`, с
одной правкой — `print` заменён на `logging`, потому что у демона stdout
никто не читает, а строка, ушедшая в никуда, это потерянная диагностика.

## Зачем демон, если сам по себе он ничего не даёт

Роадмап отвечал на это заранее и отвечает до сих пор: сегодняшний агент в
фоне просто спал бы. Он нужен как **условие** для двух вещей, ради которых
проект и затевался, — инициативы в тишине (аккумулятор желания говорить
обязан копиться, когда никто не пишет) и фоновой жизни (Curator, Dreamer).
Плюс он же снимает холодный старт на каждую реплику и делает очевидным
ответ на вопрос «кто владелец записи».

## Что здесь есть и чего нет

**Есть:** петля на `LISTEN/NOTIFY` с таймаутом, разгребание накопленного
на старте, закрытие сессии по `SIGTERM`, внятный отказ, если базы нет.

**Нет пула соединений**, и это решение, а не недоделка. Пул нужен там, где
транзакций несколько одновременно; у демона Шага 29 их не бывает —
он однопоточный, ход идёт за ходом, и второй транзакции взяться неоткуда.
Завести пул сейчас значило бы завести механику без задачи, а вместе с ней
вопрос «чья транзакция чья», на который нечем ответить, пока нет фоновых
петель. Условие названо: **пул приходит вместе с Фазой 5**, первым же
проходом, ушедшим с критического пути.

**Нет водяного знака `digested_through`.** Он делается вместе с
восстановительным проходом — порознь это мёртвая колонка, и такая в
проекте уже была (`source` на Шаге 14).

**Заслонка ретривера по-прежнему считает ходы.** У демона «три хода»
перестают быть длительностью: между ходами могут пройти сутки. Долг назван
в роадмапе и остаётся там же — мерить временем значит править схему
(`agent.last_search_ts`), а порог сегодня не на чем обосновать. Что
изменилось к лучшему: счётчик хотя бы перестал обнуляться каждым запуском
клиента — процесс теперь живёт дольше разговора.
"""

import logging
import signal
import sys
from datetime import datetime, timezone

import config
import cycle
import engine as engine_mod
import outside
import sky
import store_pg
import timeutil
from mind import summarize_session

# Сколько демон ждёт уведомления, прежде чем проснуться самому.
#
# Таймаут здесь не «на всякий случай», у него две работы, и обе нужны:
#
# 1. **Проба живости.** Postgres рвёт простаивающие соединения, а по
#    мёртвому каналу `NOTIFY` не приходит вовсе — то есть отказ выглядит
#    как тишина, и отличить его от «никто не пишет» нечем. Пробуждение по
#    таймауту делает настоящий запрос (`pending()`), и разрыв всплывает
#    исключением, а не молчанием.
# 2. **Страховка от пропущенного уведомления.** Пропасть ему в норме
#    негде — сервер копит их на время работы, — но петля, зависящая от
#    того, что уведомление точно дойдёт, чинится только перезапуском.
#
# Пять секунд — ещё и потолок задержки выхода: обработчик сигнала ставит
# флаг, а ждущий вызов дочитывает свой таймаут (PEP 475 возобновляет
# прерванное ожидание). `TimeoutStopSec` у systemd на порядок больше.
POLL_SECONDS = 5.0

_STOP = False


def _on_signal(signum, _frame) -> None:
    """Попросить петлю остановиться. В обработчике — только флаг.

    Закрывать сессию отсюда нельзя: `finish_session` ходит в сеть за
    выжимкой и пишет в базу, а обработчик сигнала прерывает произвольную
    точку программы — в том числе середину транзакции. Флаг же
    проверяется там, где известно, что ход закончен.
    """
    global _STOP
    _STOP = True
    logging.info("получен сигнал %s — закрываю сессию и выхожу", signum)


def _norm_place(name: str) -> str:
    return (name or "").strip().lower().replace("ё", "е")


def check_timezone(zone_name: str | None, now: datetime) -> None:
    """Согласован ли APP_TZ с местом. Расхождение — только запись в лог.

    Чинить автоматически нечего: пояс — настройка запуска, место — свойство
    объекта №0, и какое из двух неверно, знает только человек. Проверка
    приблизительная: `parse_tz` на нераспознанном имени молча вернёт дефолт,
    и тогда расхождение не всплывёт. Лучше, чем ничего, но не гарантия.
    """
    if not zone_name:
        return
    zone = timeutil.parse_tz(zone_name)
    if zone.utcoffset(now) != config.TZ.utcoffset(now):
        logging.warning(
            "часовой пояс не согласован с местом: APP_TZ даёт %s, а место в %s (%s). "
            "Надёжнее задать APP_TZ именем зоны, тогда переход на зимнее время учтётся сам",
            config.TZ.utcoffset(now), zone_name, zone.utcoffset(now),
        )


def resolve_place(eng, edges: cycle.Edges, now: datetime) -> bool:
    """Имя места -> координаты, разово и с записью в состояние.

    Зовётся только когда имя есть, а координат нет: разрешили один раз,
    записали — дальше источник истины он, и сеть больше не нужна. Первый в
    проекте факт, пришедший **не из диалога**, поэтому он приходит с
    `source` — та самая метка происхождения, которую роадмап обещал к
    Фазе 5 и которая понадобилась ровно на первом внешнем источнике.

    Геокодер отвечает почти всегда, в том числе на опечатку. Поэтому в
    состояние ложится и то, **о чём спрашивали** (`asked`), если ответ
    назвался иначе: единственный способ заметить, что персонаж переехал
    в чужой город, — увидеть расхождение глазами.
    """
    place = eng.place()
    name = place.get("label")
    if not name or (place.get("lat") is not None and place.get("lon") is not None):
        return False

    try:
        found = outside.geocode(name, edges.http)
    except Exception as err:
        logging.warning("resolve_place: геокодер не ответил: %s", err)
        return False

    if not found:
        return False

    place["lat"] = found["lat"]
    place["lon"] = found["lon"]
    place["source"] = found["source"]
    place["resolved_at"] = now.isoformat()

    where = ", ".join(p for p in (found["label"], found["admin1"], found["country"]) if p)
    if _norm_place(found["label"]) != _norm_place(name):
        place["asked"] = name
        logging.warning("место разрешено с расхождением: %r -> %s", name, where)
    else:
        logging.info("место разрешено: %s (%.4f, %.4f)", where, found["lat"], found["lon"])

    place["label"] = found["label"]
    with eng.unit():
        eng.save_place(place)
    check_timezone(found.get("timezone"), now)
    return True


def finish_session(eng, now: datetime, client) -> dict | None:
    """Закрыть сессию, по дороге попросив персонажа её вспомнить.

    Выжимка считается **до** `close_session`: та закрывает сессию, и после
    неё сжимать уже нечего. Проход не удался — эпизод всё равно пишется,
    с `summary: None`.

    `except Exception` шире обычного сознательно: этот вызов стоит на пути
    к выходу, в том числе по сигналу, и второе нажатие не должно уносить
    сессию вместе с эпизодом.

    **T3.** Закрытие — третья единица хода, и она обязательная, а не для
    симметрии: `close_session` пишет двумя стейтментами (эпизод вставить,
    сессию пометить закрытой), и под autocommit без границы между ними
    появилась бы щель. Упасть в неё значит получить эпизод, на который не
    смотрит ни одна закрытая сессия, а сессию — вечно открытой. Выжимка
    считается ДО единицы — она сетевая.
    """
    buf = eng.summary_buffer()
    summary = None
    if buf.get("exchanges"):
        logging.info("записываю разговор: %s обменов", len(buf["exchanges"]))
        try:
            summary = summarize_session(eng.snapshot(now), buf, client)
        except Exception as err:
            logging.warning("finish_session: выжимка не собралась: %s", err)

    with eng.unit():
        episode = eng.close_session(now, summary)
    if episode:
        logging.info(
            "сессия закрыта: %s, %s обменов, выжимка: %s",
            episode["id"], episode["exchanges"], "есть" if summary else "нет",
        )
    return episode


def drain(eng, edges: cycle.Edges, since_search: int) -> int:
    """Разобрать очередь до дна. Возвращает заслонку для следующего круга.

    Круг делается заново, пока очередь не опустеет: пока считался ответ,
    человек мог дописать, и ждать уведомления о том, что уже лежит в
    таблице, незачем.

    **Три причины выйти из петли, и они разные.** Пусто — работа сделана.
    Ошибка модели — реплика осталась непомеченной и вернётся следующим
    кругом; продолжать сразу значило бы долбить упавшего провайдера в
    полную скорость, а пауза до следующего пробуждения и есть та самая
    отсрочка. Перехват — пачку забрал кто-то другой, и она уже помечена,
    так что следующий `pending()` её не вернёт; выход тут не обязателен, но
    честнее: если демонов два, дальше пусть работает тот, кто выиграл.
    """
    while not _STOP:
        now = datetime.now(timezone.utc)
        outcome = cycle.handle_pending(
            eng, edges, now,
            since_search=since_search,
            announce=lambda _answer: store_pg.notify(
                eng.conn, store_pg.CHANNEL_REPLY),
            close_session=lambda: finish_session(
                eng, datetime.now(timezone.utc), edges.llm),
        )
        if outcome is None:
            return since_search
        since_search = outcome.since_search
        if outcome.error:
            logging.warning("ход не состоялся: %s — реплика ждёт следующего круга",
                            outcome.error)
            return since_search
        if outcome.superseded:
            return since_search
    return since_search


def serve(eng, edges: cycle.Edges) -> None:
    """Петля демона: ждать, разгребать, ждать.

    Порядок первых двух действий обратный тому, каким кажется правильным:
    **сначала разгрести, потом подписаться на новое**. Демон мог лежать
    сутки, и в очереди его ждёт разговор; уведомления о нём не придёт —
    оно ушло в никуда, пока процесса не было. Подписка ловит будущее,
    таблица помнит прошлое, и начинать надо с таблицы.

    `LISTEN` при этом выдаётся ДО первого разгребания, а не после: между
    «прочитал очередь» и «подписался» есть щель, и реплика, пришедшая в
    неё, ждала бы до таймаута. Пять секунд — не катастрофа, но щель,
    которую видно, стоит закрыть порядком строк, а не терпением.
    """
    store_pg.listen(eng.conn, store_pg.CHANNEL_INBOX)
    logging.info("слушаю канал %s, пробуждение не реже %.0f с",
                 store_pg.CHANNEL_INBOX, POLL_SECONDS)

    since_search = cycle.RETRIEVER_COOLDOWN
    since_search = drain(eng, edges, since_search)

    while not _STOP:
        # `stop_after=1` — проснуться на первом же уведомлении, а не
        # копить их: что именно пришло, петлю не интересует, ей важно
        # только «есть повод сходить в таблицу». Остальные уведомления
        # доберутся следующим ожиданием и застанут очередь уже пустой.
        for note in eng.conn.notifies(timeout=POLL_SECONDS, stop_after=1):
            logging.debug("уведомление: %s", note.channel)
        if _STOP:
            break
        since_search = drain(eng, edges, since_search)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # Оба сигнала штатного выхода ведут в одно место. `SIGTERM` присылает
    # systemd на `stop` и `restart`, `SIGINT` — человек из терминала; без
    # обработчика каждая перезагрузка сервера теряла бы выжимку. Путь
    # `SIGKILL` прикрыт иначе — `session_stale` на старте закроет брошенную
    # сессию, — но терять эпизод на ШТАТНОМ рестарте нельзя.
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        edges = cycle.open_edges()
    except RuntimeError as err:
        logging.error("%s", err)
        return 1

    # База недоступна — отказ с ПЕРВОЙ строки и ненулевой код возврата.
    # Юнит с `Restart=on-failure` иначе ушёл бы в молчаливую петлю
    # рестартов; пункт 8 чек-листа одноразовых путей ровно про это.
    try:
        eng = engine_mod.open_engine()
    except Exception as err:
        logging.error("хранилище недоступно: %s", err)
        edges.close()
        return 1
    logging.info("хранилище: %s", eng.name)

    boot = datetime.now(timezone.utc)
    resolve_place(eng, edges, boot)

    # Брошенная сессия закрывается на старте — это и есть страховка от
    # `SIGKILL`. Порог тот же, что у живого разговора: рестарт через минуту
    # оставляет сессию открытой НАМЕРЕННО, разговор продолжается с того же
    # места, и рабочая память поднимается из хранилища (Шаг 24).
    if eng.session_stale(boot):
        finish_session(eng, boot, edges.llm)

    place = eng.place()
    if place.get("label") and sky.local_snapshot(
            place.get("lat"), place.get("lon"), boot, config.TZ) is None:
        logging.warning(
            "место задано именем (%s), но координат нет: геокодер не помог и "
            "APP_LAT/APP_LON не разобраны — блок среды выключен", place["label"]
        )

    logging.info("%s поднят", config.APP_NAME)
    try:
        serve(eng, edges)
    finally:
        # Сессия закрывается на ЛЮБОМ выходе, включая падение петли.
        # Выжимка стоит одного вызова модели; эпизод, потерянный из-за
        # исключения в неожиданном месте, не вернётся никогда.
        finish_session(eng, datetime.now(timezone.utc), edges.llm)
        eng.close()
        edges.close()
        logging.info("остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
