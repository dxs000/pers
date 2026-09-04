import config
import logging
import json
import timeutil
from datetime import datetime, timezone
from openai import OpenAIError
from snapshot import Turn, clip_text

SELF_ASSERTION_LIMIT = 6
OBJECTS_LIMIT = 7
OBJECTS_ASSERTIONS_LIMIT = 3
_TRUSTED_CONF = {"med", "high"}
_CONF_WEIGHT = {"high":2, "med":1}
# Вес происхождения — независимая ось ранга (вариант «б» из ROADMAP). Шкала
# `confidence` схлопнута (почти всё `high`), поэтому промоушен доверия висит
# не на ней, а здесь: подтверждённое вторым источником весит выше всего,
# затем добытое из сети (персонаж сам сходил проверить), затем разговор.
_SOURCE_WEIGHT = {"web": 1, "self": 0, "user": 0}
SILENCE_MIN_HOURS = 1.0
SUMMARY_CHAR_LIMIT = 400
SUMMARY_EXCHANGES_LIMIT = 40
# Поисковый запрос: короткий, одной строкой. Длинный ответ — почти всегда
# признак того, что модель написала рассуждение вместо запроса.
QUERY_CHAR_LIMIT = 120

# Сколько меток памяти показываем ретриверу, чтобы он не искал известное.
RETRIEVER_MEMORY_LIMIT = 12

# Сколько символов сниппета доезжает до промпта. `web` уже режет по 400 —
# это второй, более жёсткий бортик, уже на стороне слов: три результата по
# 400 символов заняли бы в промпте больше места, чем вся память персонажа.
FINDING_CHAR_LIMIT = 250

EPISODES_LIMIT = 3
EPISODE_MAX_AGE_HOURS = 70*24

# Слова для кодов из `sky`. Арифметика неба ничего не знает про русский —
# сюда она отдаёт коды, а называет их слой рендера (как WEEKDAYS в timeutil).
# Два набора: «смеркается» и «светает» — одна и та же высота солнца,
# разное направление. Направление приходит в слепке готовым.
SKY_LIGHT_FALLING = {
    "day": "светло",
    "civil": "смеркается",
    "nautical": "темнеет",
    "astronomical": "почти темно",
    "night": "темно",
}
SKY_LIGHT_RISING = {
    "day": "светло",
    "civil": "светает",
    "nautical": "начинает светать",
    "astronomical": "почти темно",
    "night": "темно",
}
SKY_NEXT_EVENT = {"sunrise": "рассвет", "sunset": "закат"}
SKY_LOW_SUN = 7.0      # ниже — солнце заметно у горизонта
SKY_FULL_MOON = 0.92   # единственная фаза, которую видно как событие

# Коды WMO -> слова. Тот же принцип, что с кодами неба: сервис отдаёт
# числа, называет их слой рендера.
WEATHER_WORDS = {
    0: "ясно", 1: "почти ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь",
    51: "морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "ливень", 81: "ливень", 82: "сильный ливень",
    85: "снежный заряд", 86: "снежный заряд",
    95: "гроза", 96: "гроза с градом", 99: "гроза с градом",
}

# Семейства: смена семейства — событие, смена кода внутри семейства — нет.
# «Дождь усилился» новостью не считаем, «начался дождь» считаем.
WEATHER_FAMILIES = {
    "clear": (0, 1),
    "clouds": (2, 3),
    "fog": (45, 48),
    "drizzle": (51, 53, 55, 56, 57),
    "rain": (61, 63, 65, 66, 67),
    "snow": (71, 73, 75, 77, 85, 86),
    "showers": (80, 81, 82),
    "thunder": (95, 96, 99),
}
_FAMILY_BY_CODE = {code: family for family, codes in WEATHER_FAMILIES.items() for code in codes}

WEATHER_STARTED = {
    "fog": "лёг туман", "drizzle": "заморосило", "rain": "начался дождь",
    "showers": "полил дождь", "snow": "пошёл снег", "thunder": "началась гроза",
}
WEATHER_ENDED = {
    "fog": "туман разошёлся", "drizzle": "морось кончилась", "rain": "дождь кончился",
    "showers": "дождь кончился", "snow": "снег кончился", "thunder": "гроза ушла",
}
_WEATHER_PHRASES = set(WEATHER_STARTED.values()) | set(WEATHER_ENDED.values())

# Насколько давнее наблюдение ещё считается «прошлым разом». Дальше —
# перемены произошли не при персонаже, и «стемнело» звучит как открытие
# очевидного. Тот же дух, что у бакетов возраста: память нечёткая.
LATCH_FRESH_HOURS = 6.0
WEATHER_APPARENT_GAP = 5.0   # ниже — «ощущается как» не стоит упоминания

# Возраст, раньше которого своих воспоминаний не бывает. Всё, что человек
# «помнит» о себе до него, ему рассказали. Число не медицинское, а смысловое:
# рендер обязан отличать пережитое от пересказанного, иначе персонаж заявит,
# будто помнит собственное рождение, — и это будет первое, что он о себе
# прочитает.
FIRST_MEMORY_AGE = 4

def build_system_prompt(
    turn: Turn,
    now=None,
    sky: dict | None = None,
    weather: dict | None = None,
    last_exchange=None,
    findings: list[dict] | None = None,
) -> str:
    """Системный промпт из снимка хода.

    Шаг 17: на входе `Turn`, а не `state`. Список объектов больше не
    приезжает отдельным параметром — он часть снимка, потому что отбор
    top-N есть работа хранилища, а не вызывающего.
    """
    name = turn.name
    mood = turn.mood

    born = timeutil.parse_ts(turn.born_at or "")
    age = timeutil.age_years(born, now)

    # Имя, возраст и происхождение — ОДНОЙ строкой, а не тремя.
    # Тремя они выглядели бы анкетой, а это первое, что модель читает о себе:
    # анкета в первой строке задаёт тон всему ответу.
    who = f"Тебя зовут {name}"
    if age is not None:
        who += f", тебе {age} {_years_word(age)}"
    if turn.birthplace:
        who += f", родом ты из {turn.birthplace}"

    parts = []

    traits_string = turn.traits_line

    parts.append(
        f"{who}. "
        f"Твои черты характера: {traits_string}. "
        f"Сейчас твое настроение - {mood}"
    )

    label = turn.place_label
    if label:
        parts.append(f"Ты в {label}.")

    if now is not None:
        line = f"Сейчас {timeutil.render_now(now)}."
        silence = _render_silence(last_exchange, now)
        if silence:
            line=f"{line} {silence}"
        parts.append(line)            

    outside_line = _render_outside(sky, weather, turn.outside_latch, now)
    if outside_line:
        parts.append(f"За окном (упоминай, только если к месту): {outside_line}.")

    picked = _pick_assertions(turn.self_assertions, SELF_ASSERTION_LIMIT)
    if picked:
        lines = "\n".join(f"- {a['key']}: {a['value']}" for a in picked)
        parts.append("Что ты знаешь о себе:\n" + lines)

    memories_block = _render_memories(turn.memories, born)
    if memories_block:
        parts.append(
            "Что тебе сейчас вспоминается "
            "(упоминай, только если к месту):\n" + memories_block
        )    

    episodes_block = _render_episodes(turn.episodes, now)
    if episodes_block:
        parts.append(
            "О чем мы говорили раньше (упоминай, только если к месту):\n" + episodes_block
        )    

    found_block = _render_findings(findings)
    if found_block:
        parts.append(found_block)

    if turn.objects:
        lines = "\n".join(f"- {_render_object(o, now)}" for o in turn.objects)
        parts.append(
            "Что сейчас всплывает в памяти "
            "(упоминай, только если к месту):\n" + lines
        )
                              

    return"\n\n".join(parts)    

def reflect_mood(turn: Turn, user_text:str, answer:str, client) -> str | None:
    current = turn.mood

    prompt = (
        f"Текущее настроение персонажа: {current}. "
        f"Последний обмен репликами.\n"
        f"Собеседник: {user_text}\n"
        f"Персонаж ответил: {answer}\n"
        f"Каким стало настроение персонажа после этого обмена? "
        f"Ответь одним словом - новым настроением, "
        f"без пояснений и знаков препинаний."
    )

    try:
        response = client.chat.completions.create(
            model = config.DEEPSEEK_MODEL_LIGHT,
            messages=[{"role":"user", "content":prompt}],
            extra_body={"thinking": {"type": "disabled"}}
        )
    except OpenAIError as err:
        logging.warning("reflect_mood: запрос упал: %s", err)
        return None

    row=response.choices[0].message.content or ""
    mood = row.strip().strip(".!?\"'").lower()

    if not mood or len(mood.split())>2:
        logging.warning("reflect_mood: невалидный ответ: %s", row)
        return None

    return mood

def reflect_self(turn: Turn, user_text: str, answer: str, client) -> list[dict]:
    prompt = _build_reflector_prompt(turn, user_text, answer)
    try:
        response = client.chat.completions.create(
            model = config.DEEPSEEK_MODEL_LIGHT,
            messages=[{"role":"user", "content":prompt}],
            extra_body={"thinking": {"type": "disabled"}}
        )
    except OpenAIError as err:
        logging.warning("extractor: запрос упал: %s", err)
        return []
    
    row=response.choices[0].message.content or ""
    return _parse_reflector_output(row)
        

def extract_objects(turn: Turn, user_text:str, answer:str, client,
                    findings: list[dict] | None = None) -> list[dict]:
    prompt = _build_extractor_prompt(turn, user_text, answer, findings)
    try:
        response = client.chat.completions.create(
            model = config.DEEPSEEK_MODEL_LIGHT,
            messages=[{"role":"user", "content":prompt}],
            extra_body={"thinking": {"type": "disabled"}}
        )
    except OpenAIError as err:
        logging.warning("extractor: запрос упал: %s", err)
        return []
    
    row=response.choices[0].message.content or ""
    return _parse_extractor_output(row)



def _extractor_web_block(findings: list[dict] | None) -> str:
    """Блок про источник для экстрактора — только когда сеть реально что-то дала.

    Пусто при `None` (поиска не было) и при `[]` (искали, не нашли): в обоих
    случаях web-факту взяться неоткуда, а лишний разговор про источник только
    склонял бы модель проставить `web` наугад. Отсюда и байт-в-байт прежний
    промпт на ходах без поиска — регрессия, которую проверяем отдельно.

    Что помечает модель, а что код: **код знает, что поиск был, и кладёт сюда
    выдачу; какие именно факты в ответе взяты из неё — судит модель**, потому
    что в одной реплике сплетены слова собеседника, память персонажа и
    справка. Пометку `web` подхватывает уже готовый провод `_parse_assertions`.
    """
    if not findings:
        return ""

    lines = []
    for item in findings:
        snippet = (item.get("snippet") or "").strip()
        if not snippet:
            continue
        if len(snippet) > FINDING_CHAR_LIMIT:
            snippet = snippet[:FINDING_CHAR_LIMIT].rstrip() + "..."
        title = (item.get("title") or "").strip()
        lines.append(f"- {title}. {snippet}" if title else f"- {snippet}")
    if not lines:
        return ""

    return (
        "На этом ходу персонаж перед ответом заглянул в сеть. Вот что нашлось:\n"
        + "\n".join(lines) + "\n\n"
        "Если факт в \"assertions\" взят ИМЕННО из этой справки - а не из слов\n"
        "собеседника и не из памяти персонажа - добавь ему \"source\":\"web\".\n"
        "Факты из слов собеседника или из памяти оставляй БЕЗ \"source\".\n"
        "Форма факта с пометкой:\n"
        '{"key":"...", "value":"...", "confidence":"high", "source":"web"}\n\n'
    )


def _build_extractor_prompt(turn: Turn, user_text:str, answer:str,
                            findings: list[dict] | None = None) -> str:
    """Промпт экстрактора. Берёт ПОЛНЫЙ реестр, а не top-N: сматчить с
    забытым объектом нельзя, если его нет в списке, а забытый — не
    удалённый. Отбор по важности живёт в системном промпте и только там."""
    roaster = turn.registry
    roaster_json = json.dumps(roaster, ensure_ascii=False) if roaster else "[]"

    web_block = _extractor_web_block(findings)

    return (
        "Ты - служебный проход экстрактор. Задача: вытащить из последнего обмена репликами\n "
        "сущности (людей, места, вещи, темы, события), о которых идет речь, и вернуть их\n"
        "строго в JSON. \n\n"

        "Не извлекай самого персонажа (ассистента) - только внешний мир диалога.\n\n"

        "Уже известные сущности (сматчи, если речь о той же самой):\n"
        f"{roaster_json}\n\n"

        "Правила:\n"
        "-Если сущность уже в списке выше - скопируй ее \"id\" один-в-один\n"
        "Если новая - поле \"id\" не указывай вовсе.\n"
        " -\"type\" - строго один из: person, place, thing, topic, event, other.\n"
        " -\"confidence\" утверждения - строго одно из: low, med, high. \n"
        "high - только явно сказанному, low - домысленному. \n"
        "\"assertions\" - факты о сущности из этого обмена, пары {key, value, confidence}. \n"
        "Фактов нет - пустой список. \n"
        "- не выдумывай того, чего нет в репликах. \n\n"

        "Ответ - ТОЛЬКО JSON-массив, без пояснений и без markdown. Форма:\n"
        '[{"id":"xxx", "type":"person", "label":"Анна", "aliases":["Аня"],'
        '"assertions":[{"key":"occupation", "value":"бэкенд-разработчик", "confidence":"high"}]}]\n\n'

        f"{web_block}"

        "Последний обмен:\n"
        f"Собеседник: {user_text}\n"
        f"Персонаж ответил: {answer}\n"
    )

def _build_reflector_prompt(turn: Turn, user_text: str, answer: str) -> str:
    """Промпт рефлектора.

    `known_json` — дословный дамп ассершенов в текст промпта, и это самое
    хрупкое место контракта: форма хранения протекает в промпт (порядок
    ключей, формат `ts`). Из JSON порядок один, из `SELECT` — другой, а
    `ts` оттуда приедет объектом `datetime`, который `json.dumps` не
    сериализует вовсе. Поэтому снимок отдаёт ассершены УЖЕ нормализованными
    (`snapshot.normalize_assertion`), а не как их вернуло хранилище.
    """
    name = turn.name
    traits = turn.traits_line
    known = turn.self_assertions
    known_json = json.dumps(known, ensure_ascii=False) if known else "[]"

    return (
        "Ты - служебный проход рефлектор. Задача: посмотреть на последний обмен\n"
        "репликами глазами САМОГО персонажа и вытащить, что этот обмен приоткрыл\n"
        "или сообщил О НЁМ САМОМ - о его личности, а не о собеседнике.\n\n"

        f"Персонаж: {name}. Его черты: {traits}.\n\n"

        "Что персонаж уже знает/заявлял о себе (не дублируй дословно):\n"
        f"{known_json}\n\n"

        "Извлекай самонаблюдения как факты о себе - пары {key, value, confidence}:\n"
        " - биографическое, если персонаж сам это сказал о себе в реплике\n"
        "   (имя, род занятий, происхождение, ценности): key вроде\n"
        "   \"name\", \"occupation\", \"origin\", \"value\".\n"
        " - поведенческое/стилевое, что проявилось в том, КАК он ответил\n"
        "   (тон, фиксация, приём): key вроде \"style\", \"focus\", \"tic\".\n"
        " - \"confidence\" - строго одно из: low, med, high.\n"
        "   high - персонаж явно и прямо сказал это о себе;\n"
        "   low - ты это домыслил по тону.\n"
        " - не выдумывай того, чего в репликах нет. Нечего сказать - пустой массив.\n\n"

        "Ответ - ТОЛЬКО JSON-массив, без пояснений и без markdown. Форма:\n"
        '[{"key":"occupation", "value":"программист", "confidence":"high"}]\n\n'

        "Последний обмен:\n"
        f"Собеседник: {user_text}\n"
        f"Персонаж ответил: {answer}\n"
    )


VALID_SOURCES = {"user", "self", "web"}


def _parse_assertions(raw_list, default_source: str = "user") -> list[dict]:
    """Разобрать список assertions в чистые {key, value, confidence, source}.
    Не-dict и пустые key/value выкидываем, невалидный confidence в low.
    Общий парсер для extractor и reflect_self.

    Происхождение факта (`source`) в большинстве случаев **ставит код, а не
    модель**: рефлектор всегда даёт `self`, экстрактор без поиска — `user`.
    Это `default_source`, который передаёт вызывающий проход. Единственное
    исключение — экстрактор на ходу с поиском: там модель помечает, какие
    факты пришли из сети, и её `"source": "web"` мы принимаем поверх дефолта —
    провод под это здесь и включён (записывающая половина 2.5c, Шаг 14). Любой
    невалидный или отсутствующий ярлык падает обратно в `default_source` — то же
    правило снисходительности, что у `confidence`.
    """
    src_default = default_source if default_source in VALID_SOURCES else "user"
    result = []
    for a in raw_list or []:
        if not isinstance(a,dict):
            continue
        key = str(a.get("key","")).strip()
        value = str(a.get("value","")).strip()
        if not key or not value:
            continue
        conf = str(a.get("confidence","")).strip().lower()
        if conf not in {"low", "med","high"}:
            conf = "low"
        source = str(a.get("source","")).strip().lower()
        if source not in VALID_SOURCES:
            source = src_default
        result.append({"key":key, "value":value, "confidence": conf, "source": source})
    return result        

def _parse_extractor_output(row: str) -> list[dict]:
    VALID_TYPES = {"person", "place", "thing", "topic", "event", "self", "other"}
    result = []
    
    try:
        data = json.loads(_strip_fences(row))
    except json.JSONDecodeError:
        logging.warning("extractor: невалидный JSON %s", row[:200])    
        return []
    if not isinstance(data, list):
        logging.warning("extractor: ожидается list, а пришел %s", type(data).__name__)
        return[]
    
    for raw_obj in data:
        if not isinstance(raw_obj, dict):
            continue
        label = str(raw_obj.get("label","")).strip()
        if not label:
            continue
        otype = raw_obj.get("type")
        if otype not in VALID_TYPES:
            otype = "other"
        aliases = [str(x).strip() for x in (raw_obj.get("aliases") or []) if str(x).strip()]
        
        assertions = _parse_assertions(raw_obj.get("assertions"), default_source="user")

        obj = {"type": otype, "label": label, "aliases": aliases, "assertions": assertions}
        oid = raw_obj.get("id")
        if isinstance(oid, str) and oid.strip():
            obj["id"] = oid.strip()
        result.append(obj)

    return result

def _parse_reflector_output(row: str) -> list[dict]:
    try:
        data = json.loads(_strip_fences(row))
    except json.JSONDecodeError:
        logging.warning("reflect_self: невалидный JSON %s", row[:200])
        return []
    if not isinstance(data,list):
        logging.warning("reflect_self: ожидается list, а пришел %s", type(data).__name__)
        return []
    return _parse_assertions(data, default_source="self")    
           

def _strip_fences(text: str) -> str:
    s = text.strip()
    if "```" not in s:
        return s
    
    start = s.find("```")
    end=s.rfind("```")
    if start == end:
        return s
    
    inner = s[start + 3: end]

    newline = inner.find("\n")
    if newline != -1:
        first_line = inner[:newline].strip()
        if first_line == "" or first_line.isalpha():
            inner = inner[newline+1:]
    return inner.strip()

def _pick_assertions(assertions: list[dict], limit:int) -> list[dict]:
    trusted = [a for a in assertions or [] if a.get("confidence") in _TRUSTED_CONF]
    if limit < 0 or len(trusted) <= limit:
        return trusted
    ranked = sorted(enumerate(trusted), key = lambda p: _assertion_rank(p[1]), reverse=True)
    keep = sorted(i for i, _ in ranked[:limit])
    return [trusted[i] for i in keep]


def _assertion_rank(a: dict) -> tuple:
    # Порядок осей намеренный: confidence сверху (не переворачиваем прежний
    # порядок без нужды), затем подтверждённость и происхождение — они важнее
    # частоты, — и лишь потом hits/ts как тонкий разрешитель ничьих.
    return(
        _CONF_WEIGHT.get(a.get("confidence"),0),
        1 if a.get("confirmed") else 0,
        _SOURCE_WEIGHT.get(a.get("source"), 0),
        int(a.get("hits", 1)),
        a.get("ts","")
    )


# Ярлык происхождения для промпта. `user` НЕ маркируется сознательно: это
# дефолт и подавляющее большинство, а помечать каждый факт «из разговора»
# значило бы раздуть блок ради нуля информации — асимметрия намеренная.
# Код регистрирует происхождение; что оно значит, толкует модель.
# `self` тут не появляется: self-ассершены рендерит отдельный блок, где ось
# источника вырождена (всё пришло от рефлектора) — см. build_system_prompt.
_SOURCE_MARK = {
    "web": "из сети",
}


def _source_mark(a: dict) -> str:
    """Суффикс происхождения в скобках или пусто. `confirmed` — если факт
    подтверждён вторым источником (промоушен из `_merge_assertions`): это
    отдельная ось, честнее двигания `confidence` на схлопнутой шкале."""
    mark = _SOURCE_MARK.get(a.get("source"))
    if a.get("confirmed"):
        return f" ({mark}, подтверждено)" if mark else " (подтверждено)"
    return f" ({mark})" if mark else ""

def _weather_family(code) -> str | None:
    return _FAMILY_BY_CODE.get(code) if code is not None else None


def _render_weather(wx: dict | None, with_word: bool = True) -> str | None:
    """Погода -> «+14°, дождь». Ветер, облачность и осадки в миллиметрах
    остаются в слепке: за окном видно погоду, а не метеосводку.

    `with_word=False` — состояние уже объявлено новостью («начался дождь»),
    и повторять его тут же словом «дождь» незачем: температура остаётся.
    """
    if not wx:
        return None

    bits = []
    temp = wx.get("temperature")
    if temp is not None:
        value = round(temp)
        bits.append(f"{value:+.0f}°" if value else "0°")

    word = WEATHER_WORDS.get(wx.get("code")) if with_word else None
    if word:
        bits.append(word)

    apparent = wx.get("apparent")
    if temp is not None and apparent is not None and abs(apparent - temp) >= WEATHER_APPARENT_GAP:
        bits.append(f"ощущается как {round(apparent):+.0f}°")

    return ", ".join(bits) or None


def _outside_changes(sky: dict | None, wx: dict | None, prev: dict | None, now) -> list[str]:
    """Что изменилось с прошлого наблюдения. Сравнивать нечего -> пусто.

    Три условия, и все обязательны: прошлое наблюдение есть, оно свежее
    `LATCH_FRESH_HOURS`, и код действительно другой. Отсутствие любого
    из них — не повод домысливать перемену: за окном молча остаётся то,
    что там есть.
    """
    if not prev or now is None:
        return []
    then = timeutil.parse_ts(prev.get("ts", ""))
    if then is None:
        return []
    hours = (now - then).total_seconds() / 3600.0
    if not 0.0 <= hours <= LATCH_FRESH_HOURS:
        return []

    changes = []

    was, became = prev.get("light"), (sky or {}).get("light")
    if was and became and was != became:
        if became == "day":
            changes.append("рассвело")
        elif was == "day":
            changes.append("солнце село")
        elif became == "night":
            changes.append("стемнело")

    was_wx = prev.get("weather")
    became_wx = _weather_family((wx or {}).get("code"))
    if was_wx and became_wx and was_wx != became_wx:
        started = WEATHER_STARTED.get(became_wx)
        ended = WEATHER_ENDED.get(was_wx)
        if started:
            changes.append(started)
        elif ended:
            changes.append(ended)

    return changes


def _render_outside(sky: dict | None, wx: dict | None, prev: dict | None, now) -> str | None:
    """Небо, погода и перемены — одной строкой. Порядок: сперва новость."""
    bits = _outside_changes(sky, wx, prev, now)
    announced = any(change in _WEATHER_PHRASES for change in bits)

    for piece in (_render_sky(sky), _render_weather(wx, with_word=not announced)):
        if piece:
            bits.append(piece)

    return ", ".join(bits) or None


def weather_family(wx: dict | None) -> str | None:
    """Погода -> код семейства для латча, или None.

    Публичная половина `_weather_family`: латч пишет `store`, а таблица
    кодов WMO живёт здесь, потому что она про слова («начался дождь»), а
    не про хранение. `main` считает семейство и передаёт готовым — тот же
    приём, что с `now`: нечистое на краю, знание в середине.
    """
    return _weather_family((wx or {}).get("code"))


def _render_sky(snap: dict | None) -> str | None:
    """Слепок неба -> одна строка. `None` — сказать нечего, блока не будет.

    Дисциплина рендера: коротко и только то, что видно из окна. Не сводка:
    длина дня, тренд и точная высота солнца остаются в слепке для Фазы 4,
    в текст не едут. Метки времени приходят **уже локальными** — конвертация
    живёт на краю, в `main` (то же правило, что у `now` с Шага 10).
    """
    if not snap:
        return None
    words = SKY_LIGHT_RISING if snap.get("rising") else SKY_LIGHT_FALLING
    word = words.get(snap.get("light"))
    if word is None:
        return None

    bits = [word]

    altitude = snap.get("altitude")
    if snap.get("light") == "day" and altitude is not None and altitude < SKY_LOW_SUN:
        bits.append("солнце у горизонта")

    event = snap.get("next_event")
    if event and event.get("when") is not None:
        name = SKY_NEXT_EVENT.get(event.get("kind"))
        if name:
            bits.append(f"{name} в {event['when']:%H:%M}")
    else:
        # Событий нет вовсе — полярный день или ночь, штатный случай.
        never = (snap.get("events") or {}).get("never", {}).get("sun")
        if never == "above":
            bits.append("солнце не сядет")
        elif never == "below":
            bits.append("солнце не взойдёт")

    moon = snap.get("moon") or {}
    if snap.get("light") != "day" and moon.get("illumination", 0.0) >= SKY_FULL_MOON:
        bits.append("луна почти полная")

    return ", ".join(bits)


def _render_object(o: dict, now: datetime | None) -> str:
    label = o.get("label", "")
    otype = o.get("type", "other")
    picked = _pick_assertions(o.get("assertions",[]), OBJECTS_ASSERTIONS_LIMIT)

    head = f"{label} ({otype})"
    age = timeutil.humanize_age(timeutil.parse_ts(o.get("last_seen","")), now) if now else None
    if age:
        head = f"{head} [{age}]"
    if not picked:
        return head
    facts ="; ".join(f"{a['key']} = {a['value']}{_source_mark(a)}" for a in picked)
    return f"{head}: {facts}"

def _render_silence(previous: datetime | None, now) -> str | None:
    if previous is None or now is None:
        return None
    if(now - previous).total_seconds() /3600 < SILENCE_MIN_HOURS:
        return None
    age = timeutil.humanize_age(previous, now)
    return None if age is None else f"Прошлый разговор был {age}."

def summarize_session(turn: Turn, buffer: dict, client) -> str | None:
    """Буфер сессии -> воспоминание о разговоре, от лица персонажа.

    Четвёртый служебный проход по лекалу `reflect_mood`: узкий `try`,
    валидация ответа, а решение записывать принимает `main`. Разница в
    жанре: `extractor` и `reflector` **регистрируют** сказанное, а этот
    **интерпретирует** — первый в проекте текст, который система домысливает
    о прошлом. По духу ближе к `source: dream` Фазы 5, чем к экстрактору,
    и это принято сознательно: граф уже знает, ЧТО обсуждали, а эпизод
    должен помнить, КАКИМ был разговор.

    Сбой -> `None`. Эпизод всё равно запишется, просто без слов: метки
    времени и число обменов — тоже память, пусть и тусклая.
    """
    prompt = _build_summarizer_prompt(turn, buffer)
    if prompt is None:
        return None

    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL_LIGHT,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        )
    except OpenAIError as err:
        logging.warning("summarize_session: запрос упал: %s", err)
        return None

    row = response.choices[0].message.content or ""
    return _clean_summary(row)


def _build_summarizer_prompt(turn: Turn, buffer: dict) -> str | None:
    """Промпт выжимки. Пустой буфер -> `None`: сжимать нечего, и незачем
    ходить в сеть, чтобы это выяснить.

    **Транскрипт собирается из ЛЕНТЫ ролей, а не из пар (Шаг 35).** Пары
    предполагали строгое чередование «спросили — ответил», и предположение
    держалось ровно до тех пор, пока персонаж только отвечал. Реплика,
    сказанная по своей воле, чередование ломает, и на парах транскрипт
    съезжал: слова человека приписывались персонажу и наоборот.

    Заговорившая сама реплика помечена в транскрипте. Пометка нужна не для
    красоты: без неё выжимка описывает разговор, в котором собеседник
    спрашивал то, о чём не спрашивал, — а выжимка становится эпизодом,
    то есть памятью, и соврать здесь дороже, чем в ответе.
    """
    items = (buffer or {}).get("messages") or []
    if not items:
        return None

    name = turn.name
    traits = turn.traits_line

    # Обрезка длинных реплик стоит ЗДЕСЬ, а не в хранилище (Шаг 24). Предел
    # мотивирован этим промптом — простыня в транскрипте вытесняет дугу
    # разговора, ради которой проход и существует, — а хранилищу платить за
    # него было нечем, кроме потерянного хвоста реплики. Место одно на оба
    # движка, поэтому разъехаться транскриптам нечем.
    #
    # Предел в ОБМЕНАХ, лента в строках: множитель тот же, что в хранилище,
    # и по той же причине — величина про место в промпте, а не про единицу
    # счёта разговора.
    lines = []
    for item in items[-SUMMARY_EXCHANGES_LIMIT * 2:]:
        if item.get("role") == "user":
            who = "Собеседник"
        elif item.get("spontaneous"):
            who = f"{name} (заговорил сам)"
        else:
            who = name
        lines.append(f"{who}: {clip_text(item.get('text', ''))}")
    transcript = "\n".join(lines)

    # Отрезанная голова названа честно: иначе выжимка уверенно соврёт про
    # начало разговора, которого не видела. Ровно ради этого копится `dropped`.
    cut = ""
    if int((buffer or {}).get("dropped", 0)) > 0:
        cut = (
            "\nВАЖНО: начало разговора не сохранилось — ты видишь только его "
            "последнюю часть. Не пиши, с чего всё началось.\n"
        )

    return (
        "Ты - служебный проход, который сжимает состоявшийся разговор в короткое\n"
        "воспоминание. Пиши ОТ ЛИЦА ПЕРСОНАЖА, в прошедшем времени, «я».\n\n"

        f"Персонаж: {name}. Его черты: {traits}.\n"
        f"{cut}\n"

        "Что нужно:\n"
        " - 2-3 фразы, одним абзацем, без списков и заголовков.\n"
        " - НЕ пересказ тем по пунктам: темы система помнит и без тебя.\n"
        "   Нужна дуга разговора и отношение: с чего начали, куда свернули,\n"
        "   что зацепило, каким разговор был для персонажа.\n"
        " - только то, что было в репликах; не выдумывай событий и деталей.\n"
        " - без вводных вроде «в этом разговоре мы обсудили».\n\n"

        "Пример тона: «Начали с погоды, а съехали на Набокова, и я увлёкся так,\n"
        "что перебивал. Собеседник читал „Дар“ и спорил со мной про Чернышевского.»\n\n"

        "Разговор:\n"
        f"{transcript}\n"
    )


def _clean_summary(row: str) -> str | None:
    """Проза, а не JSON: чистим до одного абзаца.
    Переводы строк схлопываем в пробелы — модель любит вернуть список,
    даже когда просили абзац, а в промпте 12d многострочная выжимка
    развалила бы блок эпизодов.
    """
    text = " ".join(_strip_fences(row).split())
    if not text:
        logging.warning("summarize_session: пустой ответ")
        return None
    if len(text) > SUMMARY_CHAR_LIMIT:
        text = text[:SUMMARY_CHAR_LIMIT].rstrip() + "..."
    return text

def _pick_episodes(episodes, now, limit: int = EPISODES_LIMIT) -> list[tuple]:
    """Последние `limit` разговоров, о которых есть что сказать.

    `now is None` -> пусто: без часов не посчитать ни возраст, ни отсечку.
    Эпизоды без `summary` пропускаются: они добавили бы только «разговор
    был тогда-то», а это уже сказано строкой про паузу, и точнее.

    Идём с конца и фильтруем каждый, а не режем список: порядок `episodes`
    гарантирован не строже, чем порядок записи, и `break` по первому
    старому эпизоду однажды съел бы свежий.
    """
    if now is None:
        return []

    picked = []
    for ep in reversed(episodes or []):
        if not (ep.get("summary") or "").strip():
            continue
        ended = timeutil.parse_ts(ep.get("ended_at", ""))
        if ended is None:
            continue
        if (now - ended).total_seconds() / 3600.0 >= EPISODE_MAX_AGE_HOURS:
            continue
        picked.append((ended, ep))
        if len(picked) >= limit:
            break

    picked.reverse()   # в промпт — от старого к новому, как читается лента
    return picked


def _render_episodes(episodes, now) -> str | None:
    """Блок эпизодов. Ярлык возраста — тот же `humanize_age`, что у объектов
    и у строки про паузу: три шкалы времени в промпте обязаны называть одно
    и то же одинаково."""
    picked = _pick_episodes(episodes, now)
    if not picked:
        return None

    lines = []
    for ended, ep in picked:
        age = timeutil.humanize_age(ended, now)
        head = f"[{age}] " if age else ""
        lines.append(f"- {head}{ep['summary'].strip()}")
    return "\n".join(lines)

# =============================================================================
# Биография (Шаг 36)
# =============================================================================
# Григорианский год со всеми високосными. Точность тут избыточна на глаз, но
# `days // 365` даёт лишний год у всякого, кто прожил больше сорока, и
# промахивается именно на круглых датах — там, где ошибку заметит человек.

def _years_word(n: int) -> str:
    """год / года / лет. Живёт здесь, потому что читателей два: строка про
    самого персонажа и строка про его возраст в момент воспоминания."""
    if 11 <= n % 100 <= 14:
        return "лет"
    last = n % 10
    if last == 1:
        return "год"
    if last in (2, 3, 4):
        return "года"
    return "лет"


def _memory_when(m: dict, born: datetime | None) -> str:
    """Когда это было — так, как об этом сказал бы человек.

    **Датой почти никогда, возрастом почти всегда.** «1 сентября 2000 года»
    — форма записи в личном деле; свою жизнь помнят как «мне было семь».
    Точная дата остаётся только там, где `precision` прямо говорит, что она
    известна до дня: такое воспоминание есть, но оно редкость, и именно
    поэтому дата в нём значима.

    Заодно это обходит падежи: `timeutil.MONTHS` родительные («марта»), и
    «в марте» из них не собрать. Заводить вторую таблицу месяцев ради одного
    читателя дороже, чем не называть месяц вовсе.
    """
    at = timeutil.parse_ts(m.get("happened_at") or "")
    age = timeutil.age_years(born, at)
    if age is None or age < FIRST_MEMORY_AGE:
        return "ещё до всякой твоей памяти, с чужих слов"
    grain = m.get("precision")
    if grain == "day" and at is not None:
        return f"{at.day} {timeutil.MONTHS[at.month - 1]} {at.year}-го, тебе было {age}"
    if grain == "era":
        # Единственная нечёткая ветка. «Лет семь» — не то же, что «семь»:
        # `era` означает, что дата поставлена приблизительно, и промпт не
        # должен выдавать приблизительное за точное.
        return f"тебе было лет {age}"
    return f"тебе было {age}"


def _render_memories(memories: list[dict] | None, born: datetime | None) -> str | None:
    """Блок воспоминаний. Порядок — по жизни, а не по весу.

    Хранилище отдаёт их отсортированными по весу: это порядок ОТБОРА, и он
    отвечает на «какие всплыли». В промпте он читался бы как порядок
    важности, а важность у воспоминаний не та ось. Сортируем по времени, как
    `_pick_episodes` разворачивает ленту, — от раннего к позднему.

    Без `born` блока нет: без точки отсчёта каждая строка получила бы
    «ещё до всякой твоей памяти», то есть блок, который врёт целиком.
    """
    if not memories or born is None:
        return None

    ordered = sorted(
        memories,
        key=lambda m: timeutil.parse_ts(m.get("happened_at") or "")
        or datetime.max.replace(tzinfo=timezone.utc),
    )

    lines = []
    for m in ordered:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"- [{_memory_when(m, born)}] {text}")
    return "\n".join(lines) if lines else None


def decide_query(user_text: str, objects: list[dict], client) -> str | None:
    """Нужно ли лезть в веб — и с каким запросом. Не нужно -> `None`.

    Пятый служебный проход, и первый, работающий **до** ответа: его
    латентность человек ждёт напрямую. Отсюда `thinking: disabled` и
    короткий промпт — экономия здесь не про деньги, а про паузу перед
    репликой.

    Решение логируется **всегда**, включая отказ: без этого частоту
    срабатывания не с чем сверять, и пороги придётся подгонять вслепую.
    Ориентир — не чаще одного раза на десять обменов.

    Сбой -> `None`, как у остальных проходов: поиска сегодня не будет,
    персонаж ответит из памяти и характера.
    """
    text = (user_text or "").strip()
    if not text:
        return None

    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL_LIGHT,
            messages=[{"role": "user", "content": _build_retriever_prompt(text, objects)}],
            extra_body={"thinking": {"type": "disabled"}},
        )
    except OpenAIError as err:
        logging.warning("decide_query: запрос упал: %s", err)
        return None

    query = _clean_query(response.choices[0].message.content or "")
    logging.info("decide_query: %s", f"ищем «{query}»" if query else "не ищем")
    return query


def _build_retriever_prompt(user_text: str, objects: list[dict] | None) -> str:
    """Промпт ретривера.

    Устроен вокруг **отрицательной** проверки: не «поможет ли поиск»
    (поможет почти всегда), а «соврёт ли персонаж уверенно без него».
    Список запретов длиннее списка поводов сознательно: лишнее
    срабатывание стоит дороже пропущенного, и не секундами — блок
    найденного в промпте толкает модель пересказывать сниппеты вместо
    того, чтобы говорить своим голосом. Ретривер — единственный проход,
    способный перебить характер, потому что подсовывает готовый текст
    прямо перед ответом.

    Метки памяти показываются, чтобы критерий «система не знает эту
    сущность» был проверяемым, а не воображаемым: иначе проход пойдёт
    искать то, что уже лежит в `objects`.
    """
    known = ", ".join(
        o.get("label", "") for o in (objects or [])[:RETRIEVER_MEMORY_LIMIT] if o.get("label")
    )
    known_block = f"Система уже помнит: {known}.\n" if known else ""

    return (
        "Ты - служебный проход. Решаешь одно: нужно ли лезть в интернет,\n"
        "прежде чем персонаж ответит на реплику собеседника.\n\n"

        "Проверка ровно одна: СОВРЁТ ЛИ ПЕРСОНАЖ УВЕРЕННО, если не поискать.\n"
        "Не «пригодится ли», не «можно ли уточнить» - уточнить можно что угодно.\n\n"

        "Искать, если:\n"
        " - спрашивают про СЕЙЧАС: свежие события, текущие числа, чем\n"
        "   кончилось то, что ещё шло; ключ - время в вопросе, а не тема;\n"
        " - собеседник упомянул как известное что-то, чего система не знает\n"
        "   (новое имя, релиз, событие) - иначе персонаж вежливо подыграет\n"
        "   и придумает подробности.\n\n"

        "НЕ искать (это большинство реплик):\n"
        " - мнение, вкус, спор, оценка - у персонажа есть характер;\n"
        " - личное про собеседника - это в памяти или нигде;\n"
        " - про самого персонажа;\n"
        " - погода, свет, время суток - для этого есть датчики;\n"
        " - известная вещь просто упомянута в разговоре;\n"
        " - болтовня, эмоции, продолжение начатой темы.\n\n"

        f"{known_block}"
        f"Реплика собеседника: {user_text}\n\n"

        "Ответь ОДНОЙ СТРОКОЙ:\n"
        " - поисковый запрос (несколько слов, без кавычек и пояснений), либо\n"
        " - null\n\n"
        "В большинстве обменов правильный ответ - null. Это нормальный ответ,\n"
        "а не признание бессилия.\n"
    )


def _clean_query(row: str) -> str | None:
    """Ответ ретривера -> запрос или `None`.

    Отказы приходят по-разному: `null`, `none`, `нет`, пустая строка,
    иногда в кавычках или с точкой — всё это отказ.

    Многострочный ответ значит, что модель нарушила «одной строкой» и
    написала присказку. Берём **последнюю** непустую строку, а не первую:
    присказка почти всегда идёт перед ответом, и на первой строке оседает
    «Думаю, стоит поискать» — то есть в веб уехал бы мусор вместо запроса.

    Строка длиннее потолка — отказ, а не обрезка: обрезанное рассуждение
    стало бы поисковым запросом-бессмыслицей.
    """
    text = _strip_fences(row).strip()
    if not text:
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    query = lines[-1].strip("\"'«»").rstrip(".").strip()
    if not query:
        return None
    if query.lower() in {"null", "none", "нет", "нету", "no", "-"}:
        return None
    if len(query) > QUERY_CHAR_LIMIT:
        logging.info("decide_query: ответ длиннее запроса, считаем отказом")
        return None
    return query


def _render_findings(findings: list[dict] | None) -> str | None:
    """Блок «что нашлось в сети». Временный: в `state` не оседает.

    Различение из `web.search` доезжает до слов:
        `None` — поиска не было (нет ключа, упала сеть, заслонка). Молчим:
                 персонажу незачем знать про несостоявшийся запрос.
        `[]`   — искали, не нашли. Это персонаж вправе произнести.

    Формулировка несёт три обязанности, и каждая проверяется на живом:
    «только что глянул» — иначе модель примет справку за воспоминание и
    скажет «я всегда это знал»; «своими словами» — противовес соблазну
    зачитать сниппет; «это не твоя память» — граница, без которой
    сегодняшняя выдача завтра станет убеждением персонажа.

    **Ссылок здесь нет намеренно.** Персонаж, роняющий URL в разговоре,
    звучит как поисковая выдача с характером; ссылки остаются в логе, где
    они и нужны — для разбора, откуда пришёл факт.
    """
    if findings is None:
        return None

    if not findings:
        return "Ты глянул в сеть и не нашёл ничего внятного."

    lines = []
    for item in findings:
        snippet = (item.get("snippet") or "").strip()
        if not snippet:
            continue
        if len(snippet) > FINDING_CHAR_LIMIT:
            snippet = snippet[:FINDING_CHAR_LIMIT].rstrip() + "..."
        title = (item.get("title") or "").strip()
        lines.append(f"- {title}. {snippet}" if title else f"- {snippet}")

    if not lines:
        return None

    return (
        "Ты только что глянул в сеть - вот что нашлось. Перескажи своими\n"
        "словами, если к месту; это не твоя память, а сегодняшняя справка:\n"
        + "\n".join(lines)
    )


# =============================================================================
# Инициатива (Шаг 35): персонаж заговаривает сам
# =============================================================================
# Сколько символов оставляем сказанному по своей воле. Короче ответа
# намеренно: реплика, которой никто не просил, обязана быть репликой, а не
# монологом. Длинное непрошеное сообщение читается как навязчивость даже
# тогда, когда написано хорошо.
UTTERANCE_CHAR_LIMIT = 300

# Как назвать повод персонажу. Кодов он не видит — их называет слой рендера,
# как SKY_LIGHT_* и WEATHER_WORDS. Формулировки НЕ приказывают, о чём
# говорить: они описывают, что персонаж заметил, а решает он.
IMPULSE_REASONS = {
    "silence": "Вы давно не разговаривали.",
    "weather": "За окном переменилось.",
    "curiosity": "С прошлого разговора остался незакрытый вопрос.",
    "anniversary": "Сегодня годовщина того, что вы когда-то обсуждали.",
    "dream": "Тебе приснилось, и это не отпускает.",
}


def _render_impulse(impulse: dict | None) -> str:
    """Повод -> строка для промпта. Незнакомый род не роняет ход.

    Незнакомый род попасть сюда может: `impulses_kind_ck` перечисляет и те,
    что названы под будущее. Пропущенный ключ дал бы `KeyError` в фоновом
    проходе, то есть падение в единственном месте, где его никто не ждёт.
    """
    if not impulse:
        return ""
    reason = IMPULSE_REASONS.get(impulse.get("kind"), "Есть о чём сказать.")
    subject = (impulse.get("subject") or "").strip()
    return f"{reason} ({subject})" if subject else reason


def render_initiative(impulse: dict | None) -> str:
    """Указание заговорить. Уезжает ПОСЛЕДНЕЙ репликой, а не в системный блок.

    **Почему не в системный промпт.** Системный блок описывает, кто персонаж
    и что вокруг, — он одинаков и когда отвечают, и когда заговаривают сами.
    Дописав туда «сейчас говори», мы получили бы второй промпт персонажа,
    обязанный совпадать с первым во всём остальном; расходиться такие копии
    начинают незаметно, и разошлись бы они в характере.

    **Почему не поддельной репликой человека.** Первый набросок посылал
    «(никто ничего не написал)» ролью `user`. Это ложь ровно того рода, от
    которой отдельный канал записи и заводился: в разговор попадают слова,
    которых никто не говорил. Здесь текст ролью `user` тоже уезжает — иначе
    последним сообщением оказался бы `assistant`, а на это провайдеры
    отвечают по-разному, — но он и не притворяется репликой: это ремарка,
    и написана она как ремарка.
    """
    return (
        "[Тебе никто ничего не написал. Ты пишешь первым, потому что "
        "захотелось.]\n"
        f"Что тебя толкнуло: {_render_impulse(impulse)}\n\n"
        "Скажи одну-две фразы живым голосом, как пишут человеку, которого\n"
        "знают. Без приветствий и без «просто хотел сказать».\n"
        "Повод — это то, что тебя толкнуло, а не тема доклада: можешь начать\n"
        "с него, можешь с чего угодно, до чего он тебя довёл.\n"
        "Не задавай вопрос ради того, чтобы получить ответ — молчание в\n"
        "ответ нормально. Не пересказывай, что помнишь: собеседник помнит\n"
        "тоже. И не извиняйся за то, что пишешь первым."
    )


def speak_first(turn: Turn, impulse: dict | None, client, memory=None,
                now=None, sky: dict | None = None, weather: dict | None = None,
                last_exchange=None) -> str | None:
    """Реплика по своей воле или `None`. Любая неудача — молчание.

    Молчание, а не заглушка: не сказать вообще ничего — законное поведение
    персонажа, и отличить «сеть не ответила» от «нечего сказать» человеку по
    ту сторону нечем. Тот же принцип, что у краёв (`sky`, `web`): неудача не
    роняет ход, а убавляет то, что он может.

    Рабочая память передаётся, и это не мелочь: реплика, сказанная в тишину
    после разговора, обязана быть его продолжением, а не началом с чистого
    листа. Персонаж, заговоривший о том, что уже обсудили полчаса назад,
    выглядит не живым, а забывчивым.

    `findings` здесь нет и не будет: находки принадлежат реплике человека,
    которой в этом проходе нет. Фоновый проход в сеть не ходит — край в
    фоне означал бы, что персонаж тратит деньги, пока никто не смотрит.
    """
    messages = (
        [{"role": "system",
          "content": build_system_prompt(turn, now, sky, weather, last_exchange, None)}]
        + list(memory or [])
        + [{"role": "user", "content": render_initiative(impulse)}]
    )
    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=messages,
        )
    except OpenAIError as err:
        logging.warning("speak_first: запрос упал: %s", err)
        return None

    text = " ".join((response.choices[0].message.content or "").split())
    if not text:
        logging.warning("speak_first: пустой ответ")
        return None
    if len(text) > UTTERANCE_CHAR_LIMIT:
        text = text[:UTTERANCE_CHAR_LIMIT].rstrip() + "..."
    return text
