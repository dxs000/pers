"""Демон: процесс, который живёт, пока включён сервер (Шаг 29)."""
import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

import config
import cycle
import library
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


def require_named_timezone() -> None:
    """Падать, если APP_TZ задан числом или не задан вовсе (Шаг 0.2).

    `parse_tz` понимает и «3», и «Europe/Helsinki», и раньше числовое
    смещение считалось законной настройкой. Оно и работает — ровно полгода:
    фиксированное смещение не знает про переход на зимнее время, и с конца
    октября персонаж начинает ошибаться на час. Ошибаться молча — в логе
    ничего, в промпте правдоподобное время, и выглядит это не как настройка,
    а как «он опять путает время».

    Предупреждение здесь не годится: оно ушло бы в лог демона, который никто
    не читает, и по тому же доводу, по которому `config` ничего не печатает.
    А `check_timezone` ниже эту ловушку не закрывала — она сравнивает зону
    места с `config.TZ` и молчит, когда имя зоны не задано, то есть
    срабатывает только там, где проблемы уже нет.
    """
    spec = (config.TZ_SPEC or "").strip()
    if not spec or spec.lstrip("+-")[:1].isdigit():
        raise RuntimeError(
            f"APP_TZ={spec or '(не задан)'} — числовое смещение не знает про "
            "переход на зимнее время. Задайте именем зоны, например "
            "APP_TZ=Europe/Helsinki"
        )


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


def sync_shelf(eng) -> None:
    """Свести полку с каталогом при подъёме (Шаг 48).

    При подъёме, а не по расписанию: книги на полке появляются РУКАМИ —
    человек кладёт файл и зовёт `convert.py`, — и заметить это в ту же минуту
    незачем. Демон поднимается чаще, чем пополняется библиотека.

    Исключение не выпускается наружу по тому же правилу, что у `idle_tick`:
    сломанная полка не повод не запускать разговор. Персонаж, у которого нет
    каталога, просто не читает.
    """
    try:
        shelf = library.catalog()
    except Exception as err:
        logging.warning("полка недоступна: %s", err)
        return
    if not shelf:
        logging.info("полка пуста: %s", config.LIBRARY_DIR)
        return
    try:
        with eng.unit():
            got = eng.sync_books(shelf)
    except Exception as err:
        logging.warning("полка не свелась с каталогом: %s", err)
        return

    logging.info("полка: %s книг, из них новых %s",
                 len(shelf), len(got["added"]))
    for path in got["added"]:
        logging.info("на полке появилось: %s", path)
    for path in got["removed"]:
        logging.info("с полки убрано: %s", path)
    for path in got["missing"]:
        # Взятая книга пропала с полки. Строку не трогаем — к ней привязаны
        # порции и заметки, — но читать её нечем, и сказать об этом надо
        # громко: иначе чтение молча перестанет происходить.
        logging.warning("книга пропала, а он её читает: %s", path)
    for bad in got["conflicts"]:
        logging.warning(
            "книгу перегнали, пока он её читал: %s — было %s знаков, стало %s. "
            "Позиция чтения указывает не туда; строка не тронута",
            bad["text_path"], bad["was"], bad["now"])


def finish_session(eng, now: datetime, edges: cycle.Edges) -> dict | None:
    """Закрыть сессию: выжимка, эпизод и (Шаг 41) незакрытый вопрос.

    **Края приезжают целиком, а не одной моделью.** До Шага 41 сюда
    передавался `edges.llm`, и это было верно, пока читатель буфера был один.
    Их стало два, и оба служебные; передавать модель дважды или тянуть второй
    аргумент значило бы решать за `cycle`, чем ему пользоваться.

    Порядок жёсткий: сначала эпизод, потом повод. Эпизод — память, повод —
    настроение; упади процесс между ними, теряется второе, и это правильная
    сторона для потери.
    """
    buf = eng.summary_buffer()
    summary = None
    if buf.get("messages"):
        logging.info("записываю разговор: %s реплик", len(buf["messages"]))
        try:
            summary = summarize_session(eng.snapshot(now), buf, edges.llm)
        except Exception as err:
            logging.warning("finish_session: выжимка не собралась: %s", err)
    with eng.unit():
        episode = eng.close_session(now, summary)
    if episode:
        logging.info(
            "сессия закрыта: %s, %s обменов, выжимка: %s",
            episode["id"], episode["exchanges"], "есть" if summary else "нет",
        )
    # После закрытия, а не до: буфер уже прочитан, а повод не должен
    # существовать у разговора, который ещё идёт. Промах сети роняет ровно
    # этот проход — сессия к этому моменту закрыта и записана.
    if buf.get("messages"):
        try:
            cycle.record_curiosity(eng, edges, eng.snapshot(now), buf, now)
        except Exception as err:
            logging.warning("finish_session: любопытство не собралось: %s", err)
    return episode

def drain(eng, edges: cycle.Edges) -> None:
    while not _STOP:
        now = datetime.now(timezone.utc)
        outcome = cycle.handle_pending(
            eng, edges, now,
            announce=lambda _answer: store_pg.notify(
                eng.conn, store_pg.CHANNEL_REPLY),
            close_session=lambda: finish_session(
                eng, datetime.now(timezone.utc), edges),
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

    **Фоновых дел с Шага 42 три, и за круг делается одно.** Сон пишет,
    пересмотр черт пишет, заговаривание говорит; все трое ходят в модель, и
    сделать их подряд значило бы на одном круге заплатить трижды за работу,
    которой никто не ждёт.

    Порядок — по цене отказа, а не по важности. Сон отказывает первой
    проверкой и бесплатно (не ночь — и заход кончился, не сходив ни в базу за
    памятью, ни в модель). Пересмотр черт — двумя счётными запросами. Фоновый
    заход дороже обоих: он спрашивает погоду, то есть ходит в сеть, ещё до
    того, как решит молчать.

    Приснившееся в этот же круг НЕ рассказывается. Импульс заведён, и
    подхватит его `background_tick` на следующем круге или утром — по своим
    заслонкам, а не потому, что сон случился только что.
    """
    now = datetime.now(timezone.utc)
    # Обещания — первыми (Шаг 43), и это порядок, а не очерёдность: долг не
    # проходит через заслонки инициативы, и пропустить его сквозь них
    # невозможно — исчерпанный бюджет суток проглотил бы напоминание молча.
    try:
        if cycle.promise_tick(
                eng, edges, now,
                announce=lambda _text: store_pg.notify(
                    eng.conn, store_pg.CHANNEL_REPLY)) is not None:
            return
    except Exception as err:
        logging.warning("напоминание не удалось: %s", err)
    # День (Шаг 46) — до сна. Окна у них не пересекаются (вечер против ночи),
    # так что порядок ни на что не влияет сегодня; он выбран по смыслу: сон
    # берёт дневной остаток, а день к этому моменту уже подведён.
    try:
        if cycle.day_tick(eng, edges, now) is not None:
            return
    except Exception as err:
        logging.warning("день не подведён: %s", err)
    try:
        if cycle.dream_tick(eng, edges, now) is not None:
            return
    except Exception as err:
        logging.warning("сон не приснился: %s", err)
    # Чтение (Шаг 49) — после сна и до черт. Порядок тот же, по цене отказа:
    # первая проверка у него часовая, то есть бесплатная, как ночь у сна.
    # Перед днём и сном он не встаёт намеренно: у тех окна узкие (вечер, ночь)
    # и пропущенный заход возвращается только через сутки, а у чтения окно в
    # пятнадцать часов — уступить круг ему ничего не стоит.
    try:
        if cycle.reading_tick(eng, edges, now) is not None:
            return
    except Exception as err:
        logging.warning("чтение не состоялось: %s", err)
    try:
        if cycle.reconsider_traits(eng, edges, now) is not None:
            return
    except Exception as err:
        logging.warning("черты не пересмотрены: %s", err)
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


def cmd_read() -> int:
    """Один заход чтения прямо сейчас (Шаг 49).

    **Предохранителя вроде `--write` у генезиса тут нет, и это не оплошность.**
    Чтение необратимо ровно в том смысле, в каком необратим прожитый вечер:
    страницы прочитаны, позиция сдвинулась, — но переиграть тут нечего, это и
    есть нормальная работа прохода. Сухого прогона у него быть не может:
    единственный способ узнать, что он прочитал, — дать ему прочитать.

    Смысл команды — не «прогнать тест», а не ждать шести часов, глядя в лог.
    Демон при этом может быть запущен: оба ходят через `advance_reading` с
    условием на позицию, и одновременный заход кончится отказом, а не двойным
    чтением.
    """
    edges = cycle.open_edges()
    eng = engine_mod.open_engine()
    try:
        sync_shelf(eng)
        got = cycle.reading_tick(eng, edges, datetime.now(timezone.utc),
                                 force=True)
    finally:
        eng.close()
        edges.close()
    if got is None:
        # Причину называет лог, и называет точно: пустая полка, отказ его
        # словами или непонятый ответ модели. Перечислять их здесь заново
        # значило бы гадать вслух там, где рядом напечатан ответ.
        print("заход состоялся, читать не стал — причина строкой выше")
        return 1
    print(got)
    return 0


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
    parser.add_argument("--read", action="store_true",
                            help="один заход чтения сейчас, минуя заслонки времени")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
        
    # До всего остального: пояс участвует и в рождении (дата), и в ночи, и в
    # сроках обещаний. Ошибиться в нём молча дороже, чем не запуститься.
    try:
        require_named_timezone()
    except RuntimeError as err:
        logging.error("%s", err)
        return 2

    if args.genesis:
        return cmd_genesis(args.text, write=args.write)
    if args.read:
        return cmd_read()
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
    sync_shelf(eng)
    boot = datetime.now(timezone.utc)
    resolve_place(eng, edges, boot)
    if eng.session_stale(boot):
        finish_session(eng, boot, edges)
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
        finish_session(eng, datetime.now(timezone.utc), edges)
        eng.close()
        edges.close()
        logging.info("остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())