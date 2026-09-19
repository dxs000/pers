"""Полка: то, что персонаж читает, и то место, откуда он читает.

Четвёртый модуль-край после `sky`, `outside` и `web`, и правило то же:
**любая неудача — `None` или пустой список, а не исключение.** Файл пропал,
права не те, текст короче, чем помнит база, — книги сегодня нет, а демон
живёт. Персонаж, у которого сломалось чтение, должен молчать про книгу, а не
падать.

## Чего здесь нет

**Базы.** Ни `psycopg`, ни `store_pg`, ни `engine`. Наружу — словари и строки;
что из этого станет строкой в `books`, решает хранилище. То же разделение, что
у `outside`: край добывает, хранилище хранит.

**Модели.** Ни одного вызова LLM. Что прочитанное значит — забота `mind`.

**Часов.** `now` этому модулю не нужен ни разу, и это признак того, что
разделение проведено верно: у полки нет времени, время есть у чтения.

**Рендера.** Ни «осталось столько-то страниц», ни «глава пятая» словами.
Наружу — числа и заголовок как он записан; слова живут в `mind`.

## Позиция

Смещение в символах файла `text/*.md`, как его написал `convert.py`. Отсюда
два следствия, и оба важнее, чем кажутся.

**Перегнал книгу заново — позиция поехала.** Не приблизительно, а совсем:
другая версия чистки даёт другую длину, и записанное «дочитал до 140 000»
начинает указывать не туда. Поэтому `catalog` возвращает ДЕЙСТВИТЕЛЬНУЮ длину
файла, а не ту, что записана в спутнике: расхождение обязано быть видно
хранилищу, а не всплыть посреди чтения.

**Порция режется по границе абзаца, а не по числу.** Ровно `PORTION_CHARS`
оборвали бы фразу, и модель получила бы на вход полпредложения в начале и
полпредложения в конце. Число здесь — цель, а не мера.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import config

log = logging.getLogger("library")

TEXT_DIR = "text"

# =============================================================================
# Порция
# =============================================================================
# Сколько он прочитывает за один заход. Двадцать тысяч знаков — это примерно
# десять книжных страниц, то есть вечер обычного чтения.
#
# Число выбрано ОТ ЖИЗНИ, а не от контекста модели. В контекст влезло бы
# впятеро больше, и соблазн велик: меньше заходов, дешевле. Но тогда роман
# прочитывается за неделю, а хочется, чтобы книга занимала месяц, — потому что
# она и должна занимать месяц. Из этого числа складывается его ритм:
# «Лолита» в 730 000 знаков — это тридцать семь вечеров.
#
# Правится на живом, как `urge` и `RETRIEVER_COOLDOWN`. Если окажется, что
# конспекты выходят пустыми — порция велика, модель не удержала; если что
# заметки мельчают — мала.
PORTION_CHARS = 20_000

# Насколько можно перелететь цель в поисках конца абзаца. Абзац у Набокова
# бывает в несколько тысяч знаков, и жёсткий предел резал бы ровно его.
PORTION_SLACK = 4_000

# Остаток короче этого дочитывается вместе с предыдущей порцией. Иначе книга
# кончается заходом на две тысячи знаков, и последний конспект получается
# обрывком — а последний конспект как раз тот, по которому персонаж скажет,
# чем книга кончилась.
PORTION_TAIL = 8_000

# Хвост предыдущей порции, который едет в промпт как НАПОМИНАНИЕ, а не как
# чтение. Без него заход начинается с середины сцены: конспект помнит, о чём
# книга, но не помнит, на какой фразе остановились.
LEAD_CHARS = 400

# Конец предложения, если границы абзаца в окне не нашлось.
_SENTENCE_ENDS = (". ", "! ", "? ", "… ", ".\n", "!\n", "?\n", "…\n")


def _root() -> Path:
    return Path(config.LIBRARY_DIR)


def _read(path: Path) -> str | None:
    """Текст книги.

    `newline=""` — перевод строк НЕ транслируется. Универсальный режим Python
    склеил бы `\\r\\n` в `\\n`, файл стал бы короче прочитанного с диска, и
    позиция разошлась бы с длиной — молча и ровно на числе строк.
    """
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except OSError as err:
        log.warning("library: не прочитался %s: %s", path, err)
        return None


# =============================================================================
# Каталог
# =============================================================================
def catalog(root: Path | None = None) -> list[dict]:
    """Что лежит на полке. Пустой список — полки нет или она пуста.

    Читает спутники `*.json`, которые оставил `convert.py`, и сверяет их с
    файлами. Спутника нет — книги нет: файл в `text/` без спутника означает,
    что его положили руками мимо ворот качества, и брать его нельзя по тому же
    правилу, по которому ворота вообще заведены.
    """
    root = root or _root()
    text_dir = root / TEXT_DIR
    if not text_dir.is_dir():
        log.info("library: нет каталога %s", text_dir)
        return []

    books = []
    for meta_path in sorted(text_dir.glob("*.json")):
        md = meta_path.with_suffix(".md")
        if not md.is_file():
            log.warning("library: спутник без книги: %s", meta_path.name)
            continue
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            log.warning("library: спутник не разобрался (%s): %s",
                        meta_path.name, err)
            continue
        text = _read(md)
        if text is None:
            continue

        actual = len(text)
        declared = meta.get("length")
        if declared is not None and declared != actual:
            # Не ошибка и не повод пропустить книгу: так выглядит перегнанный
            # заново файл. Ошибкой это станет у хранилища, которое помнит
            # позицию, — и заметить обязано оно, а не чтение.
            log.warning(
                "library: %s изменилась: в спутнике %s знаков, в файле %s",
                md.name, declared, actual)

        books.append({
            "text_path": str(md.relative_to(root)),
            "title": (meta.get("title") or md.stem).strip(),
            "author": (meta.get("author") or None),
            "length": actual,
            "source_path": meta.get("source_path"),
            "source_kind": meta.get("source_kind"),
            "quality": meta.get("quality"),
        })
    return books


def find(text_path: str, root: Path | None = None) -> dict | None:
    """Одна книга по пути из базы. `None` — файла больше нет.

    Отдельная функция, а не поиск в `catalog()`: читающий проход спрашивает про
    ОДНУ книгу на каждом заходе, и разбирать ради этого всю полку значило бы
    читать с диска десяток романов, чтобы взять из одного двадцать тысяч
    знаков.
    """
    root = root or _root()
    md = root / text_path
    if not md.is_file():
        log.warning("library: книга пропала с полки: %s", text_path)
        return None
    text = _read(md)
    if text is None:
        return None
    meta = {}
    meta_path = md.with_suffix(".json")
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    return {
        "text_path": text_path,
        "title": (meta.get("title") or md.stem).strip(),
        "author": meta.get("author") or None,
        "length": len(text),
    }


# =============================================================================
# Порция
# =============================================================================
def portion(text_path: str, from_pos: int, size: int = PORTION_CHARS,
            root: Path | None = None) -> dict | None:
    """Кусок книги с позиции. `None` — читать нечего или файл недоступен.

    Наружу:

        from_pos, to_pos — полуинтервал [from_pos, to_pos); `to_pos` — начало
                           СЛЕДУЮЩЕЙ порции, поэтому порции, сложенные подряд,
                           дают книгу без пропусков и без повторов;
        text             — сам кусок;
        lead             — хвост прочитанного до него, для связности;
        chapter          — ближайший заголовок выше `from_pos`, если есть;
        done             — дочитано до конца;
        progress         — доля книги, пройденная к `to_pos`.

    Позиция за концом книги — не ошибка, а «дочитал»: возвращается `None`, и
    решает это вызывающий.
    """
    root = root or _root()
    md = root / text_path
    text = _read(md)
    if text is None:
        return None

    total = len(text)
    from_pos = max(0, int(from_pos))
    if from_pos >= total:
        return None

    to_pos = _cut(text, from_pos, size, total)
    body = text[from_pos:to_pos]
    if not body.strip():
        log.warning("library: пустая порция в %s с %s", text_path, from_pos)
        return None

    return {
        "text_path": text_path,
        "from_pos": from_pos,
        "to_pos": to_pos,
        "text": body.strip(),
        "lead": _lead(text, from_pos),
        "chapter": chapter_at(text, from_pos),
        "done": to_pos >= total,
        "progress": round(to_pos / total, 4) if total else 1.0,
        "length": total,
    }


def _cut(text: str, from_pos: int, size: int, total: int) -> int:
    """Где кончить порцию. Всегда на границе, и всегда СТРОГО больше `from_pos`.

    Порядок предпочтений — от самой чистой границы к самой грубой:

        1. ближайшая граница абзаца ВПЕРЁД, в пределах `PORTION_SLACK`;
        2. ближайшая граница абзаца НАЗАД, но не раньше начала порции;
        3. конец предложения вперёд;
        4. просто цель.

    Четвёртый случай — книга без абзацев и без точек, то есть сломанная. Он
    оставлен работающим намеренно: отказаться читать было бы хуже, а увидеть
    это можно по `avg_paragraph` в воротах, где такая книга и должна была
    отсеяться.
    """
    target = min(from_pos + max(size, 1), total)

    # Хвост короче `PORTION_TAIL` дочитывается сразу.
    if total - target < PORTION_TAIL:
        return total

    limit = min(total, target + PORTION_SLACK)
    ahead = text.find("\n\n", target)
    if ahead != -1 and ahead + 2 <= limit:
        return ahead + 2

    back = text.rfind("\n\n", from_pos, target)
    if back != -1 and back + 2 > from_pos:
        return back + 2

    for end in _SENTENCE_ENDS:
        found = text.find(end, target)
        if found != -1 and found + len(end) <= limit:
            return found + len(end)

    return target


def _lead(text: str, from_pos: int) -> str:
    """Хвост прочитанного перед порцией, начиная с целой фразы.

    Обрезанный по символу хвост начинался бы с середины слова, и модель
    честно приняла бы обрубок за слово. Ищем начало предложения; не нашли —
    отдаём как есть, это всего лишь напоминание.
    """
    if from_pos <= 0:
        return ""
    start = max(0, from_pos - LEAD_CHARS)
    tail = text[start:from_pos].strip()
    if not tail:
        return ""
    for end in (". ", "! ", "? ", "… "):
        found = tail.find(end)
        if found != -1 and found < len(tail) - 40:
            return tail[found + len(end):].strip()
    return tail


def chapter_at(text: str, pos: int) -> str | None:
    """Ближайший заголовок выше позиции.

    Заголовки ставит `convert.py` — из разметки fb2 и epub, где они настоящие.
    У PDF их нет, и `None` здесь законный ответ, а не сбой: у половины книг
    структуры просто не существует, и притворяться, что она есть, незачем.
    """
    at = text.rfind("\n## ", 0, pos + 1)
    if at == -1:
        at = 0 if text.startswith("## ") else -1
        if at == -1:
            return None
        line_end = text.find("\n", 0)
        return text[3:line_end if line_end != -1 else len(text)].strip() or None
    line_end = text.find("\n", at + 1)
    head = text[at + 4:line_end if line_end != -1 else len(text)]
    return head.strip() or None


def excerpt(text_path: str, pos: int, span: int = 300,
            root: Path | None = None) -> str | None:
    """Кусок книги вокруг позиции. Для заметки, у которой есть `at_pos`.

    Нужен потому, что цитат в базе нет (`0011_reading.sql`): заметка помнит
    место, а слова читаются с полки в тот момент, когда понадобились.
    """
    root = root or _root()
    text = _read(root / text_path)
    if text is None:
        return None
    start = max(0, int(pos) - span // 2)
    return text[start:start + span].strip() or None


# =============================================================================
# Автономная проверка
# =============================================================================
def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    books = catalog()
    if not argv:
        if not books:
            print(f"полка пуста: {_root() / TEXT_DIR}")
            return 1
        print(f"на полке {len(books)}:")
        for b in books:
            who = f"{b['author']}. " if b["author"] else ""
            print(f"  {b['text_path']:<46} {who}{b['title']} "
                  f"({b['length']} знаков, ~{b['length'] // PORTION_CHARS + 1} "
                  f"заходов)")
        return 0

    wanted = argv[0]
    at = int(argv[1]) if len(argv) > 1 else 0
    match = [b for b in books if wanted in b["text_path"]]
    if not match:
        print(f"не нашёл на полке: {wanted}")
        return 1
    got = portion(match[0]["text_path"], at)
    if got is None:
        print("читать нечего: позиция за концом книги")
        return 1
    print(f"{match[0]['title']}: {got['from_pos']}–{got['to_pos']} "
          f"({got['to_pos'] - got['from_pos']} знаков, "
          f"{got['progress'] * 100:.1f}%"
          + (", дочитано" if got["done"] else "") + ")")
    if got["chapter"]:
        print(f"глава: {got['chapter']}")
    if got["lead"]:
        print(f"\n[до этого] …{got['lead'][-120:]}")
    print(f"\n{got['text'][:400]}\n   […]\n{got['text'][-200:]}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
