"""Демон: процесс, который живёт, пока включён сервер (Шаг 29)."""
import argparse
import logging
import signal
import sys
import time
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

# Фон ходит РЕЖЕ реактивной ветки, и интервалы разведены намеренно (Шаг 37).
#
# Пять секунд — это про отзывчивость на реплику человека: он написал и ждёт.
# У фона такого адресата нет. Молчание меряется часами, погода кэширована на
# двадцать минут, событие «сменилось семейство» не протухает за минуту. Общий
# интервал означал 17 280 заходов в сутки — каждый со своими запросами к
# базе — ради решения, которое меняется дважды в день.
#
# Минута уменьшает это в двенадцать раз и не портит ничего: повод, замеченный
# на минуту позже, остаётся тем же поводом. Величина отдельная, а не
# `POLL_SECONDS * 12`, потому что связи между ними нет — они отвечают разным
# вопросам, и разъезжаться им можно свободно.
BACKGROUND_SECONDS = 60.0

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
    if buf.get("messages"):
        logging.info("записываю разговор: %s реплик", len(buf["messages"]))
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
            if cycle.digest_one(eng, edges, now):
                continue
            return
        if outcome.error:
            logging.warning(
                "ход не состоялся: %s — реплика ждёт следующего круга",
                outcome.error,
            )
            return
        if outcome.superseded:
            return


def idle_tick(eng, edges: cycle.Edges) -> None:
    """Фоновая работа: делается только когда очередь пуста (Шаг 35).

    **Реактивная ветка вытесняет фоновую, и это правило, а не оптимизация.**
    Человек, написавший реплику, ждёт ответа; персонаж, собравшийся заговорить
    сам, не ждёт ничего. Поставь фоновый заход впереди `drain` — и на каждую
    реплику накладывалась бы задержка вызова модели, которого никто не просил.

    Очередь перепроверяется ПОСЛЕ захода: заход ходит в сеть за погодой и в
    модель за репликой, и за это время реплика могла прийти. Возвращаемся в
    `serve`, который тут же позовёт `drain`.

    Исключение не выпускается наружу: фон — не то, из-за чего демон обязан
    падать. Реактивная половина от этого не зависит и должна пережить
    сломанный фон.
    """
    try:
        cycle.background_tick(
            eng, edges, datetime.now(timezone.utc),
            announce=lambda _text: store_pg.notify(
                eng.conn, store_pg.CHANNEL_REPLY),
        )
    except Exception as err:
        logging.warning("фоновый заход не удался: %s", err)


def serve(eng, edges: cycle.Edges) -> None:
    store_pg.listen(eng.conn, store_pg.CHANNEL_INBOX)
    logging.info("слушаю канал %s, пробуждение не реже %.0f с",
                 store_pg.CHANNEL_INBOX, POLL_SECONDS)
    drain(eng, edges)
    # Часы фона МОНОТОННЫЕ, а не настенные: интервал здесь — «сколько
    # прошло», а не «который час», и переводу времени или подкрутке NTP
    # влиять на него нечем. Настенное время персонажа
    # (`datetime.now(timezone.utc)`) живёт внутри самого захода, где оно и
    # означает время.
    next_background = time.monotonic()
    while not _STOP:
        for note in eng.conn.notifies(timeout=POLL_SECONDS, stop_after=1):
            logging.debug("уведомление: %s", note.channel)
        if _STOP:
            break
        drain(eng, edges)
        if _STOP:
            break
        if time.monotonic() >= next_background:
            idle_tick(eng, edges)
            # Отсчёт от МОМЕНТА ОКОНЧАНИЯ, а не от запланированного: заход, в
            # котором персонаж заговорил, длится вызов модели, и отсчёт от
            # плана дал бы следующий заход сразу же, догоняя расписание.
            # Фону догонять нечего.
            next_background = time.monotonic() + BACKGROUND_SECONDS


def cmd_genesis(first_text: str, write: bool) -> int:
    """Рождение персонажа. Без `--write` только печатает.

    **Печать по умолчанию, запись по явному слову** — тот же предохранитель,
    что у `db.py --reset --prod`, и по той же причине: действие необратимо.
    Разница лишь в том, что там стиралась память, а здесь появляется жизнь,
    которую нельзя переиграть, не заведя другую базу.

    Предохранитель нужен ещё и потому, что рождение СЛУЧАЙНО: тяга берёт
    энтропию из места, момента и первых слов (`genesis`), и каждый прогон
    даёт другого человека. Посмотреть на нескольких, прежде чем оставить
    одного, — законный сценарий, и он не должен требовать правки кода.
    """

    edges = cycle.open_edges()
    eng = engine_mod.open_engine()
    try:
        now = datetime.now(timezone.utc)

        # Проверка ДО тяги, а не перед записью. Родившемуся персонажу план
        # показывать незачем: он предъявил бы другое имя и другую дату, чем
        # те, что у персонажа есть, — то есть выглядел бы предложением
        # переродиться, которого система не принимает. Дешевле и честнее
        # сказать это первой строкой, не сходив ни в модель, ни в геокодер.
        turn = eng.snapshot(now)
        if turn.born_at:
            print(f"персонаж уже родился: {turn.name}, {turn.born_at[:10]}, "
                  f"{turn.birthplace}")
            print("Рождение необратимо и бывает один раз. Чтобы получить "
                  "другого персонажа, нужна другая база.")
            return 1

        resolve_place(eng, edges, now)
        place = eng.place()
        if not place.get("label"):
            logging.error("место жизни не задано (APP_PLACE) — тянуть не от чего")
            return 1

        plan = cycle.plan_genesis(place, first_text, edges, now)
        b = plan.birth

        print(f"\nмир:      {place['label']} ({place.get('lat')}, "
              f"{place.get('lon')}), {now.isoformat(timespec='seconds')}")
        print(f"первые слова: {first_text!r}")
        print(f"\nтяга:     родился {b.born_at.date()}, "
              f"сейчас {b.age} {timeutil.years_word(b.age)}")
        print(f"          {b.distance_km:.1f} км, азимут {b.bearing:.1f}"
              + (f" -> {b.lat}, {b.lon}" if b.lat is not None else "")
              + ("  (там же, где живёт)" if b.same_place else ""))

        if plan.proposed:
            print(f"\nмодель предложила ({len(plan.proposed)}): "
                  + ", ".join(plan.proposed))
            if plan.survived:
                print("уцелело после геокодера "
                      f"(полоса {plan.band_km[0]:.0f}–{plan.band_km[1]:.0f} км):")
                for label, off, lat, lon in plan.survived:
                    print(f"   {label:<24} промах {off:>6.1f} км   ({lat}, {lon})")
            else:
                print("уцелело: ничего")

        print(f"\nместо рождения: {plan.birthplace} "
              f"({plan.birthplace_lat}, {plan.birthplace_lon})")

        if plan.names:
            print(f"\nимена ({len(plan.names)}):")
            for item in plan.names:
                mark = "->" if item["name"] == plan.name else "  "
                print(f" {mark} {item['name']:<16} | {item['reason']}")
        else:
            print("\nимена: модель не дала ни одного")

        if not plan.ok:
            print("\nГЕНЕЗИС НЕ СОСТОЯЛСЯ — записывать было бы нечего")
            return 1

        if not write:
            print(f"\nБЫЛО БЫ ЗАПИСАНО (добавьте --write):")
            print(f"  agent.name       = {plan.name}")
            print(f"  agent.born_at    = {b.born_at.isoformat()}")
            print(f"  agent.birthplace = {plan.birthplace}")
            print(f"  memories[1]      = ('era', 'genesis', {b.born_at.date()})")
            print(f"                     {plan.reason}")
            return 0

        with eng.unit():
            born = eng.record_birth(plan.name, b.born_at, plan.birthplace,
                                    plan.reason)
        if not born:
            # Сюда можно попасть, только если персонаж родился МЕЖДУ проверкой
            # наверху и этой строкой — то есть вторым запуском, шедшим
            # параллельно. Гонка невероятная (рождение делают руками один
            # раз), но отвечает на неё база, а не наша уверенность в том, что
            # так не бывает.
            print("\nЗАПИСЬ НЕ СОСТОЯЛАСЬ: персонаж уже родился. "
                  "Ничего не изменено.")
            return 1

        print(f"\nРОДИЛСЯ: {plan.name}, {b.born_at.date()}, {plan.birthplace}")
        print(f"  первое воспоминание: {plan.reason}")
        return 0
    finally:
        eng.close()
        edges.close()        


def main() -> int:

    parser = argparse.ArgumentParser(description="Демон персонажа.")
    parser.add_argument("--genesis", action="store_true",
                            help="прогнать рождение (только печать)")
    # `--write` вместо прежнего `--dry-run`, и это разворот умолчания.
    # Раньше `--genesis` без флагов означал «записать», а безопасный прогон
    # требовал слова. При необратимом действии умолчание обязано быть
    # безобидным: опечатка стоит одного лишнего запуска, а не одной чужой
    # жизни. Флаг `--dry-run` снят, а не оставлен синонимом, — молчаливо
    # принятый флаг, который больше ничего не значит, хуже отсутствующего.
    parser.add_argument("--write", action="store_true",
                            help="записать рождение (необратимо)")
    parser.add_argument("--text", default="привет",
                            help="первые слова, сказанные персонажу")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
        
    if args.genesis:
        return cmd_genesis(args.text, write=args.write)
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