"""Эхо: где он повторяет сам себя (Шаг 65). Лист: без базы, без сети, без часов.

Шаг 63 вылечил повтор на уровне памяти: три воспоминания больше не едут в
каждый промпт. Лог разговора показал, что тот же повтор живёт уровнем выше —
там, где писатели читают друг друга:

**Мысль.** За день «вспоминал» пять раз выбрал почти одно и то же: мать,
письмо, ящик, её рука на его руке. Каждое вспоминание пишется в биографию,
его видят побуждения, сон, проход «день» — и тянут туда же. Дела дня в
промпте выбора видны, и фраза «люди не делают одно и то же весь день» там
есть: модель читала её и выбирала то же самое.

**Речь.** Семь ответов подряд начинались с «Вот те на», «брат» и «старик» —
в каждом. Модель видит свои прошлые ответы в рабочей памяти и подражает им:
чем больше тик, тем он вероятнее. Сама она его не замечает.

Оба случая лечатся одинаково — машина замечает повтор, которого модель не
видит, и не решает за неё, что делать вместо. О чём думать и как говорить —
его дело; повторять ровно то же — это не его выбор, а инерция генерации.
"""

import re

# Основа — первые четыре буквы слова от четырёх букв. Грубо, но «опускала
# письмо в ящик» и «опустить письмо в ящик» совпадают, а пять букв развели
# бы «опуск» и «опуст». «Мать» (четыре буквы) обязана считаться.
STEM = 4
_STOP = {
    "было", "была", "были", "есть", "если", "даже", "тоже", "чтоб", "когд",
    "тольк", "толь", "сейч", "потом", "пото", "очен", "всег", "здес", "тебя",
    "тебе", "меня", "мне", "себя", "свой", "свое", "своя", "свои", "этот",
    "этог", "этом", "эта", "это", "того", "тому", "там", "так", "как", "что",
    "чего", "кото", "где", "куда", "ещё", "еще", "уже", "всё", "все", "весь",
    "один", "одно", "одна", "будт", "будто", "ведь", "вот", "ну", "да", "нет",
    "него", "нему", "неё", "нее", "ними", "него", "мой", "моя", "мою", "моё",
    "наш", "ваш", "перв", "самы", "само", "сама", "сам",
}


def stems(text: str, stem: int = STEM) -> set[str]:
    words = re.findall(r"[а-яёa-z]+", (text or "").lower().replace("ё", "е"))
    out = set()
    for w in words:
        if len(w) < stem:
            continue
        s = w[:stem]
        if s not in _STOP and w not in _STOP:
            out.add(s)
    return out


# --- Мысль: одно и то же дело об одном и том же -------------------------------

SAME_SHARED = 2      # минимум общих основ
SAME_SHARE = 0.5     # и доля от меньшего из двух наборов


def same_subject(a: str, b: str) -> bool:
    """Про одно ли это. Мера — от меньшего набора: короткое «про мать и
    письмо» целиком внутри длинного — это оно и есть."""
    sa, sb = stems(a), stems(b)
    if not sa or not sb:
        return False
    shared = len(sa & sb)
    return shared >= SAME_SHARED and shared / min(len(sa), len(sb)) >= SAME_SHARE


def times_today(action: str, about: str | None, today: list[dict]) -> int:
    """Сколько раз сегодня он уже делал это дело об этом же."""
    if not about:
        return 0
    return sum(1 for p in today or ()
               if p.get("action") == action and p.get("about")
               and same_subject(about, p["about"]))


# --- Речь: привычки, которых он не слышит ------------------------------------

HABIT_WINDOW = 6         # сколько последних своих ответов смотреть
HABIT_OPENING = 3        # одно начало в стольких из них — привычка
HABIT_WORD = 4           # одно слово в стольких — привычка
HABIT_WORDS_SHOWN = 3
# Начало — первые три слова: «Вот те на» из двух было бы «Вот те».
_OPEN_RE = re.compile(r"^[\s«\"'—–-]*((?:[а-яёa-z]+[\s,.…!?-]*){1,3})", re.I)
WORD_STEM = 5   # «старик», «старика», «старик-то» — одно слово


def _opening(text: str) -> str | None:
    m = _OPEN_RE.match(text or "")
    if not m:
        return None
    words = re.findall(r"[а-яёa-z]+", m.group(1).lower())
    return " ".join(words) if len(words) >= 2 else None


def _word_forms(text: str) -> dict[str, str]:
    """{основа: форма} по ответу. Форма — чтобы назвать слово по-человечески."""
    out = {}
    for w in re.findall(r"[а-яёa-z]+", (text or "").lower()):
        if len(w) < 4 or w in _STOP or w[:STEM] in _STOP:
            continue
        out.setdefault(w[:WORD_STEM], w)
    return out


def speech_habits(replies: list[str]) -> str | None:
    """Строка для промпта, если в последних ответах завелась привычка.

    Называет её и ничего не предлагает взамен: не «говори иначе вот так», а
    «ты это делаешь, заметь». Порог высокий — одно слово в четырёх из шести
    ответов, — чтобы не дёргать за нормальную речь: у всякого человека есть
    любимые слова, привычка — это когда они в каждой фразе.
    """
    last = [r for r in replies if r][-HABIT_WINDOW:]
    if len(last) < HABIT_OPENING:
        return None
    notes = []

    openings = [_opening(r) for r in last]
    for op in dict.fromkeys(o for o in openings if o):
        if openings.count(op) >= HABIT_OPENING:
            notes.append(f"последние ответы ты начинал с «{op.capitalize()}»")
            break

    counts: dict[str, int] = {}
    forms: dict[str, dict[str, int]] = {}
    for r in last:
        for stem, form in _word_forms(r).items():
            counts[stem] = counts.get(stem, 0) + 1
            forms.setdefault(stem, {})
            forms[stem][form] = forms[stem].get(form, 0) + 1
    frequent = sorted((w for w, n in counts.items() if n >= HABIT_WORD),
                      key=lambda w: -counts[w])[:HABIT_WORDS_SHOWN]
    if frequent:
        shown = [min(forms[w], key=lambda f: (-forms[w][f], len(f))) for w in frequent]
        words = ", ".join(f"«{w}»" for w in shown)
        notes.append(f"{words} - почти в каждом ответе")

    if not notes:
        return None
    return ("Заметь за собой: " + "; ".join(notes) + ". Это привычка речи, "
            "а не то, что ты сейчас хочешь сказать. Говори как говорится, но "
            "не повторяй её по инерции.")
