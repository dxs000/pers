"""Демон: процесс, который живёт, пока включён сервер (Шаг 29)."""
import argparse
import logging
import signal
import sys
import time
import logsetup
from datetime import datetime, timezone

import config
import cycle
import library
import engine as engine_mod
import outside
import sky
import store_pg
import timeutil
import essay as essay_mod
import news as news_mod
import drives as drives_mod
import agenda as agenda_mod
import embed as embed_mod
import him as him_mod
import voice as voice_mod
from mind import summarize_session

POLL_SECONDS = 5.0
BACKGROUND_SECONDS = 60.0

_STOP = False


def _on_signal(signum, _frame) -> None:
    global _STOP
    _STOP = True
    logging.info("получен сигнал %s — закрываю сессию и выхожу", signum)


def _norm_place(name: str) -> str:
    return (name or "").strip().lower().replace("ё", "е")


def require_named_timezone() -> None:
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
    logging.info("полка: %s книг, из них новых %s", len(shelf), len(got["added"]))
    for path in got["added"]:
        logging.info("на полке появилось: %s", path)
    for path in got["removed"]:
        logging.info("с полки убрано: %s", path)
    for path in got["missing"]:
        logging.warning("книга пропала, а он её читает: %s", path)
    for bad in got["conflicts"]:
        logging.warning(
            "книгу перегнали, пока он её читал: %s — было %s знаков, стало %s. "
            "Позиция чтения указывает не туда; строка не тронута",
            bad["text_path"], bad["was"], bad["now"])


def finish_session(eng, now: datetime, edges: cycle.Edges) -> dict | None:
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
    if buf.get("messages"):
        try:
            cycle.record_curiosity(eng, edges, eng.snapshot(now), buf, now)
        except Exception as err:
            logging.warning("finish_session: любопытство не собралось: %s", err)
        # Шаг 59: кто он мне теперь. После любопытства, а не до: тот проход
        # смотрит на разговор глазами «о чём спросить», этот — «кого я
        # узнал», и снимок ему нужен уже с закрытой сессией в эпизодах.
        try:
            him_mod.learn(eng, edges, eng.snapshot(now), buf, now)
        except Exception as err:
            logging.warning("finish_session: о нём не собралось: %s", err)
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


def _inner(eng) -> None:
    """Экран, покажи: внутри что-то случилось (Шаг 61).

    Тот же канал, что у реплики. Отдельный канал значил бы второго
    слушателя в Express и вторую подписку в браузере ради одного и того же
    действия — перечитать `/session`, где теперь лежит и внутреннее.
    """
    try:
        store_pg.notify(eng.conn, store_pg.CHANNEL_REPLY)
    except Exception as err:
        logging.debug("notify: %s", err)


def idle_tick(eng, edges: cycle.Edges) -> None:
    now = datetime.now(timezone.utc)
    try:
        if cycle.promise_tick(
                eng, edges, now,
                announce=lambda _text: store_pg.notify(
                    eng.conn, store_pg.CHANNEL_REPLY)) is not None:
            return
    except Exception as err:
        logging.warning("напоминание не удалось: %s", err)
    try:
        if cycle.day_tick(eng, edges, now) is not None:
            _inner(eng)
            return
    except Exception as err:
        logging.warning("день не подведён: %s", err)
    try:
        if cycle.dream_tick(eng, edges, now) is not None:
            _inner(eng)
            return
    except Exception as err:
        logging.warning("сон не приснился: %s", err)
    try:
        if cycle.reconsider_traits(eng, edges, now) is not None:
            return
    except Exception as err:
        logging.warning("черты не пересмотрены: %s", err)
    # Голос (Шаг 60) — сразу за чертами: тяга говорить выводится из них, и
    # пересмотренные черты с прежней тягой — два разных человека в одном.
    try:
        if voice_mod.talk_tick(eng, edges, now) is not None:
            return
    except Exception as err:
        logging.warning("тяга говорить не выведена: %s", err)
    try:
        if drives_mod.drives_tick(eng, edges, now) is not None:
            return
    except Exception as err:
        logging.warning("побуждения не пересмотрены: %s", err)
    # Чтение, эссе и новости больше не ходят по своим расписаниям (Шаг 57):
    # их зовёт проход «чем заняться», когда он сам так решил. Выше остаются
    # дела, которые не выбирают: обещания, итог дня, сон, пересмотр себя.
    try:
        if agenda_mod.agenda_tick(eng, edges, now) is not None:
            _inner(eng)
            return
    except Exception as err:
        logging.warning("решение, чем заняться, не состоялось: %s", err)
    # Векторы досчитываются без модели и без заслонок (Шаг 58): свежая строка
    # биографии должна находиться по смыслу уже в следующем разговоре.
    try:
        if embed_mod.embed_tick(eng, edges) is not None:
            return
    except Exception as err:
        logging.warning("векторы не досчитаны: %s", err)
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
            next_background = time.monotonic() + BACKGROUND_SECONDS


def cmd_read() -> int:
    edges = cycle.open_edges()
    eng = engine_mod.open_engine()
    try:
        sync_shelf(eng)
        got = cycle.reading_tick(eng, edges, datetime.now(timezone.utc), force=True)
    finally:
        eng.close()
        edges.close()
    if got is None:
        print("заход состоялся, читать не стал — причина строкой выше")
        return 1
    print(got)
    return 0


def cmd_news() -> int:
    edges = cycle.open_edges()
    eng = engine_mod.open_engine()
    try:
        got = news_mod.news_tick(eng, edges, datetime.now(timezone.utc),
                                 force=True)
    finally:
        eng.close()
        edges.close()
    if got is None:
        print("заход не состоялся — причина строкой выше")
        return 1
    print(got)
    return 0


def cmd_drives() -> int:
    """Один проход побуждений сейчас, мимо заслонок времени (Шаг 56)."""
    edges = cycle.open_edges()
    eng = engine_mod.open_engine()
    try:
        got = drives_mod.drives_tick(eng, edges, datetime.now(timezone.utc),
                                     force=True)
        drives = eng.open_drives(datetime.now(timezone.utc))
    finally:
        eng.close()
        edges.close()
    if got is None:
        print("проход не состоялся — причина строкой выше")
        return 1
    print("\n".join(got) or "без перемен")
    print("\nсейчас открыто:")
    for d in drives:
        print(f"  [{d['score']:.2f}] {drives_mod.KIND_WORDS[d['kind']]}: {d['text']}"
              f"  <- #{', #'.join(map(str, d['sources']))}")
    return 0


def cmd_agenda() -> int:
    """Одно решение «чем заняться» сейчас, мимо заслонок времени (Шаг 57)."""
    edges = cycle.open_edges()
    eng = engine_mod.open_engine()
    try:
        got = agenda_mod.agenda_tick(eng, edges, datetime.now(timezone.utc),
                                     force=True)
    finally:
        eng.close()
        edges.close()
    if got is None:
        print("решения не было — причина строкой выше")
        return 1
    print(got)
    return 0


def cmd_voice() -> int:
    """Как он сейчас говорит и кто для него собеседник (Шаги 59–60)."""
    eng = engine_mod.open_engine()
    try:
        now = datetime.now(timezone.utc)
        v = voice_mod.voice(eng, now)
        t = eng.talk()
        print("голос:", voice_mod.describe(v))
        if t.get("why"):
            print("  тяга — потому что", t["why"])
        print(f"  тишина сейчас: {cycle.silence_urge(eng, now, v):.2f} "
              f"(порог {cycle.IMPULSE_FLOOR})")
        seen = eng.him_view()
        print("\nон:", seen.get("view") or "(взгляда ещё нет)")
        for f in eng.him_facts():
            print(f"  [{f['id']}] {f['text']}")
        for th in eng.open_threads("user"):
            print(f"  у него: [{th['id']}] {th['text']}")
        met = eng.acquaintance()
        print(f"  знакомы с {met['since']}, разговоров {met['talks']}")
    finally:
        eng.close()
    return 0


def cmd_shape() -> int:
    """Форма биографии: где жизнь вспомнена, где пусто (Шаг 64)."""
    import anchor as anchor_mod
    eng = engine_mod.open_engine()
    try:
        now = datetime.now(timezone.utc)
        turn = eng.snapshot(now)
        born = timeutil.parse_ts(turn.born_at or "")
        age_now = timeutil.age_years(born, now)
        canon = eng.all_memories()
        print(f"{turn.name or '(без имени)'}, {age_now} — вспомнено по отрезкам жизни:")
        print(anchor_mod.render_shape(canon, born, age_now))
        a = anchor_mod.draw(canon, born, age_now, now, "dream")
        if a is not None:
            print(f"сон сейчас тянул бы к {a.age} (отрезок {a.lo}–{a.hi})")
    finally:
        eng.close()
    return 0


def cmd_embed(text: str | None) -> int:
    """Досчитать векторы биографии и показать, что ближе к `text` (Шаг 58).

    Это же — проверка ключа и шкалы: пороги `EMBED_RELEVANT_FLOOR` и
    `EMBED_SAME_FLOOR` подбираются по тому, какие числа модель даёт на ЕГО
    биографии, а не на примере из документации.
    """
    edges = cycle.open_edges()
    eng = engine_mod.open_engine()
    try:
        if not embed_mod.enabled(edges):
            print("векторы выключены: нужен YANDEX_API_KEY (или ключ поиска) и YANDEX_FOLDER_ID")
            return 1
        while embed_mod.embed_tick(eng, edges, batch=50):
            pass
        done, total = eng.embedded_count(embed_mod.DOC_MODEL)
        print(f"посчитано векторов: {done} из {total} ({embed_mod.DOC_MODEL})")
        if done < total:
            print("досчитать не вышло — причина в логе строками выше")
        if not text:
            return 0
        for kind in ("query", "doc"):
            vec = embed_mod.embed(text, kind, edges)
            if vec is None:
                print(f"{kind}: вектор не получен")
                continue
            print(f"\nближе всего к «{text}» ({kind}):")
            for m in eng.similar_memories(vec, embed_mod.DOC_MODEL, 7):
                print(f"  {m['sim']:.3f}  #{m['id']:<4} {m['text'][:90]}")
        print("\nпороги: в разговор — от", end=" ")
        import store_pg
        print(f"{store_pg.RELEVANT_FLOOR} (query); дубль при вспоминании — от "
              f"{agenda_mod.SAME_FLOOR} (doc)")
    finally:
        eng.close()
        edges.close()
    return 0


def cmd_genesis(first_text: str, write: bool) -> int:
    edges = cycle.open_edges()
    eng = engine_mod.open_engine()
    try:
        now = datetime.now(timezone.utc)
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
            born = eng.record_birth(plan.name, b.born_at, plan.birthplace, plan.reason)
        if not born:
            print("\nЗАПИСЬ НЕ СОСТОЯЛАСЬ: персонаж уже родился. Ничего не изменено.")
            return 1
        print(f"\nРОДИЛСЯ: {plan.name}, {b.born_at.date()}, {plan.birthplace}")
        print(f"  первое воспоминание: {plan.reason}")
        return 0
    finally:
        eng.close()
        edges.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Демон персонажа.")
    parser.add_argument("--genesis", action="store_true", help="прогнать рождение (только печать)")
    parser.add_argument("--write", action="store_true", help="записать рождение (необратимо)")
    parser.add_argument("--text", default="привет", help="первые слова, сказанные персонажу")
    parser.add_argument("--read", action="store_true", help="один заход чтения сейчас")
    parser.add_argument("--news", action="store_true", help="один заход к новостям сейчас")
    parser.add_argument("--drives", action="store_true",
                        help="один проход побуждений сейчас и что открыто")
    parser.add_argument("--agenda", action="store_true",
                        help="одно решение «чем заняться» сейчас")
    parser.add_argument("--voice", action="store_true",
                        help="как он сейчас говорит и что знает о собеседнике")
    parser.add_argument("--shape", action="store_true",
                        help="форма биографии: где жизнь вспомнена, где пусто")
    parser.add_argument("--embed", nargs="?", const="", metavar="ТЕКСТ",
                        help="досчитать векторы биографии; с текстом — показать ближайшие")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logsetup.attach()
    try:
        require_named_timezone()
    except RuntimeError as err:
        logging.error("%s", err)
        return 2
    if args.genesis:
        return cmd_genesis(args.text, write=args.write)
    if args.read:
        return cmd_read()
    if args.news:
        return cmd_news()
    if args.drives:
        return cmd_drives()
    if args.agenda:
        return cmd_agenda()
    if args.voice:
        return cmd_voice()
    if args.shape:
        return cmd_shape()
    if args.embed is not None:
        return cmd_embed(args.embed or None)
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
