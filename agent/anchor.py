"""Тяга возраста: КОГДА вспоминается, решает не модель (Шаг 64).

Лист, как `genesis`: часов не дёргает, в базу не пишет, модель не зовёт.

## Болезнь

Три писателя биографии смотрят назад — вспомненное во сне, «вспоминал» из
дел, биограф разговора, — и двое первых спрашивали модель «сколько тебе было,
от 0 до N». Модель отвечает из приоров: детство. Там пусто, там безопасно
выдумывать, там всякая мелочь звучит значительно. `0009_lived.sql` это
заметил и завёл проход «день», но день пишет только сегодня. Между детством
и сегодня — работа, переезды, люди последних лет — не писал никто.

Человек, у которого подробное детство, пустые сорок лет и одна сегодняшняя
прогулка, — это старик, живущий прошлым. Ровно так он и читался.

## Лечение — тот же приём, что у рождения

`genesis`: «число, которое придумала модель, — число из её приоров; число,
которое вытянула машина, — число, которого не выбирал никто». Здесь то же.
Модели отдаётся данность — «тебе было около 34» — и спрашивается не «когда»,
а «что там было».

## Куда тянет

**Туда, где в жизни пусто.** Жизнь от первой памяти до сегодня режется на
отрезки по `BIN_YEARS`, вес отрезка — `1 / (1 + записанного в нём)`. Путь
развития этим не задаётся: тяга не знает, ЧТО должно быть в тридцать лет, она
знает только, что там пока ничего не записано. Когда биография выровняется,
тяга станет почти равномерной сама.

Сны и прожитые дни в счёт не идут. Сон не был, и отрезок, где только
снилось, пуст. Прожитое (`lived`) пишет сегодняшний проход «день» каждый
вечер, и засчитай его — последний отрезок «заполнился» бы прогулками за
неделю, а годы перед ним, о которых не вспомнено ничего, тяга обходила бы.

Случайность — из байтов sha256 (довод `genesis` про переносимость `Random`).
Материал — момент и размер биографии: две ночи подряд тянут разное, одна и та
же ночь в эталоне — одно и то же.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

import timeutil
from genesis import _draw

BIN_YEARS = 5
SKIP_SOURCES = ("dream", "lived")
FIRST_MEMORY_AGE = 4   # то же число, что `mind.FIRST_MEMORY_AGE`; mind импортирует тяжело


@dataclass(frozen=True)
class Anchor:
    age: int        # около скольких лет
    lo: int         # отрезок, из которого вытянуто, включительно
    hi: int
    filled: int     # сколько в этом отрезке уже записано — для журнала

    def window(self, slack: int = 1) -> tuple[int, int]:
        """Какой возраст в ответе модели ещё считается «тем временем»."""
        return self.lo - slack, self.hi + slack


def _bins(age_now: int) -> list[tuple[int, int]]:
    start = FIRST_MEMORY_AGE
    if age_now < start:
        return []
    out = []
    lo = start
    while lo <= age_now:
        hi = min(lo + BIN_YEARS - 1, age_now)
        out.append((lo, hi))
        lo = hi + 1
    # Хвост короче года — не отрезок: прилипает к предыдущему.
    if len(out) > 1 and out[-1][1] - out[-1][0] < 1:
        last = out.pop()
        out[-1] = (out[-1][0], last[1])
    return out


def coverage(canon: list[dict], born: datetime | None, age_now: int) -> list[tuple[int, int, int]]:
    """[(lo, hi, сколько вспомнено)] по отрезкам жизни. Сны и дни не считаются."""
    bins = _bins(age_now)
    counts = [0] * len(bins)
    for m in canon or ():
        if m.get("source") in SKIP_SOURCES:
            continue
        at = m.get("happened_at")
        at = timeutil.parse_ts(at) if isinstance(at, str) else at
        age = timeutil.age_years(born, at)
        if age is None:
            continue
        for i, (lo, hi) in enumerate(bins):
            if lo <= age <= hi:
                counts[i] += 1
                break
    return [(lo, hi, n) for (lo, hi), n in zip(bins, counts)]


def draw(canon: list[dict], born: datetime | None, age_now: int | None,
         now: datetime, salt: str) -> Anchor | None:
    """Вытянуть возраст. Нет рождения или жизнь короче первой памяти — `None`."""
    if born is None or age_now is None:
        return None
    cov = coverage(canon, born, age_now)
    if not cov:
        return None
    material = "|".join((salt, now.astimezone(timezone.utc).isoformat(timespec="seconds"),
                         str(len(canon or ()))))
    digest = hashlib.sha256(material.encode("utf-8")).digest()

    weights = [1.0 / (1.0 + n) for _, _, n in cov]
    total = sum(weights)
    u = _draw(digest, 0) * total
    pick = len(cov) - 1
    for i, w in enumerate(weights):
        if u < w:
            pick = i
            break
        u -= w
    lo, hi, n = cov[pick]
    age = lo + int(_draw(digest, 1) * (hi - lo + 1))
    return Anchor(age=min(age, hi), lo=lo, hi=hi, filled=n)


def render_shape(canon: list[dict], born: datetime | None, age_now: int | None) -> str:
    """Форма биографии для глаз: где жизнь записана, где пусто."""
    if born is None or age_now is None:
        return "(не родился)"
    cov = coverage(canon, born, age_now)
    dreams = sum(1 for m in canon or () if m.get("source") == "dream")
    lived = sum(1 for m in canon or () if m.get("source") == "lived")
    widest = max((n for _, _, n in cov), default=0) or 1
    lines = [f"{lo:>3}–{hi:<3} {'█' * round(20 * n / widest):<20} {n}"
             for lo, hi, n in cov]
    lines.append(f"прожитых дней (lived): {lived}, снов: {dreams}")
    return "\n".join(lines)
