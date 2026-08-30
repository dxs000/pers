"""Демон: процесс, который живёт, пока включён сервер (Шаг 29).

`main.py` расщеплён надвое. Сюда переехало всё, что делает персонажа
персонажем, — петля, разрешение места, границы сессии; в `cli.py` осталась
оболочка, и она теперь просто клиент.
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

POLL_SECONDS = 5.0

_STOP = False


def _on_signal(signum, _frame) -> None:
    global _STOP
    _STOP = True
    logging.info("получен сигнал %s — закрываю сессию и выхожу", signum)


def _norm_place(name: str) -> str:
    return (name or "").strip().lower().replace("ё", "е")


def check_timezone(zone_name: str | None, now: datetime) -> None:
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


def drain(eng, edges: cycle.Edges) -> None:
    """Разобрать очередь до дна."""
    while not _STOP:
        now = datetime.now(timezone.utc)
        outcome = cycle.handle_pending(
            eng, edges, now,
            announce=lambda _answer: store_pg.notify(
                eng.conn, store_pg.CHANNEL_REPLY),
            close_session=lambda: finish_session(
                eng, datetime.now(timezone.utc), edges.llm),
        )
        if outcome is None:
            return
        if outcome.error:
            logging.warning(
                "ход не состоялся: %s — реплика ждёт следующего круга",
                outcome.error,
            )
            return
        if outcome.superseded:
            return


def serve(eng, edges: cycle.Edges) -> None:
    store_pg.listen(eng.conn, store_pg.CHANNEL_INBOX)
    logging.info("слушаю канал %s, пробуждение не реже %.0f с",
                 store_pg.CHANNEL_INBOX, POLL_SECONDS)

    drain(eng, edges)

    while not _STOP:
        for note in eng.conn.notifies(timeout=POLL_SECONDS, stop_after=1):
            logging.debug("уведомление: %s", note.channel)
        if _STOP:
            break
        drain(eng, edges)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        edges = cycle.open_edges()
    except RuntimeError as err:
        logging.error("%s", err)
        return 1

    try:
        eng = engine_mod.open_engine()
    except Exception as err:
        logging.error("хранилище недоступно: %s", err)
        edges.close()
        return 1
    logging.info("хранилище: %s", eng.name)

    boot = datetime.now(timezone.utc)
    resolve_place(eng, edges, boot)

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
        finish_session(eng, datetime.now(timezone.utc), edges.llm)
        eng.close()
        edges.close()
        logging.info("остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())