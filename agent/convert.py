"""Конвертер библиотеки: из того, что лежит на полке, в то, что можно читать.

Запускается РУКАМИ и живёт вне демона. Это не удобство, а условие:

    - OCR книги идёт минутами, а иногда часами. Такое в цикле, который обязан
      ответить человеку за пять секунд, не стоит;
    - неудачная конвертация не должна повторяться каждый тик. Файл, который не
      разобрался, не разберётся и через минуту, а фоновый проход, честно
      пробующий снова, сожжёт вечер;
    - и главное — **результат смотрит человек.** Ворота ниже ловят битую
      кодировку и рассыпавшийся OCR, но они не поймают перепутанный порядок
      глав. Один взгляд на середину получившегося файла стоит всех эвристик,
      и он возможен ровно потому, что конвертация — отдельная команда.

## Почему это вообще отдельный шаг

Канон неизменяем (`0003_life.sql`). Кусок текста, рассыпавшийся при
извлечении, становится конспектом, конспект — заметкой, заметка — сценой,
сцена ложится в биографию НАВСЕГДА и тянет за собой все последующие. Это
единственная ошибка в проекте, которая не лечится следующим заходом: сломанный
ход можно переиграть, приснившееся — перезаписать, а записанное в канон
нельзя.

Отсюда ворота качества стоят ДО каталога, а не после. Книга, их не прошедшая,
для персонажа не существует: он не может её выбрать, потому что её нет в
`books`.

## Дисциплина модуля

**Ничего не знает о базе.** Ни `psycopg`, ни `store_pg`, ни `engine`. На
выходе — файлы: чистый текст и спутник с метаданными. Строки в `books` заводит
сканер (`library.py`), и это разделение того же рода, что между `sky`/`outside`
и `mind`: край добывает, хранилище хранит, и знать друг о друге им незачем.

**Ничего не знает о модели.** Ни одного вызова LLM. Заголовок и автор берутся
из метаданных файла или из имени файла; угадывать их моделью значило бы
поставить между вашей полкой и памятью персонажа ещё один источник выдумки.

**Любая неудача — внятный отказ, а не падение.** Правило краёв (`web`,
`outside`): не разобралось — файл уезжает в `reject/` с отчётом рядом,
остальная полка конвертируется дальше.

**Тяжёлые зависимости грузятся лениво.** `pymupdf` нужен только для PDF,
`djvutxt` и `ocrmypdf` — только для DjVu и сканов. Полка из одних fb2
конвертируется на голой стандартной библиотеке, и требовать ради неё
установки MuPDF было бы налогом на самый частый случай.

## Раскладка

    library/
        source/   ваши файлы как есть: fb2, fb2.zip, epub, pdf, djvu
        text/     то, что персонаж читает: .md в UTF-8 + .json со спутником
        reject/   то, что не прошло ворота, с отчётом рядом

## Про формат исходников

Для русских книг **fb2 лучше всего и почти всегда доступен**. Это простой XML:
абзацы размечены, главы названы, сноски лежат отдельным разделом, переносов
нет вовсе. PDF того же текста даёт в разы больше работы и худший результат.
Правило стоит держать на уровне полки, а не решать по каждой книге: есть fb2 —
берите fb2.

Автономная проверка:

    python convert.py                 # сконвертировать всё новое
    python convert.py --file x.pdf    # одну книгу
    python convert.py --check         # перемерить то, что уже лежит в text/
    python convert.py --ocr           # разрешить OCR для сканов
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

import config

# =============================================================================
# Полка
# =============================================================================
# Путь приезжает из `config`, потому что читателей у него двое: этот модуль и
# `library.py`. Пока читатель был один, переменная жила здесь — по правилу
# самого `config`, который запрещает обещать ручку, которую никто не
# спрашивает. Читателей стало двое, и правило развернулось: разъехаться двум
# копиям пути нельзя, иначе конвертер напишет в одну папку, а демон будет
# читать из другой и честно сообщит, что полка пуста.
LIBRARY_DIR = Path(config.LIBRARY_DIR)

SOURCE_DIR = "source"
TEXT_DIR = "text"
REJECT_DIR = "reject"

# Что вообще берём с полки. Всё остальное игнорируется молча: в папке с
# книгами лежат и обложки, и readme, и `.DS_Store`, и ругаться на них значит
# приучить себя не читать вывод.
KNOWN_SUFFIXES = {
    ".fb2": "fb2",
    ".fb2.zip": "fb2",
    ".zip": "fb2",        # почти всегда это fb2.zip; не он — отказ по разбору
    ".epub": "epub",
    ".pdf": "pdf",
    ".djvu": "djvu",
    ".djv": "djvu",
    ".txt": "txt",
    ".md": "txt",
}

# =============================================================================
# Пороги ворот
# =============================================================================
# Все до одного правятся на живой полке, как `RETRIEVER_COOLDOWN` и `urge`.
# Числа ниже — не теория, а то, что отделяет читаемую книгу от рассыпавшейся
# на трёх-четырёх пробных полках; менять их надо, глядя на `--check`, а не на
# рассуждение.

# Доля букв среди всех символов. Ниже — не текст, а мусор: так выглядит PDF с
# битым CMap, где каждый символ «извлёкся», но буквой не является.
MIN_LETTER_SHARE = 0.55

# Доля одного алфавита среди букв. Книга бывает русской, бывает английской, но
# ПОЛОВИНА НА ПОЛОВИНУ не бывает никогда — так выглядит подмена кодировки,
# когда часть текста уехала в латиницу. Порог про однородность, а не про
# русский язык: требовать кириллицы значило бы запретить персонажу английские
# книги, а этого решения мы не принимали.
MIN_SCRIPT_SHARE = 0.85

# Доля слов длиной один-два символа. В русской прозе это предлоги и союзы, их
# около пятой части. Треть и больше означает рассыпавшиеся пробелы: так
# выглядит плохой OCR и PDF, у которого межбуквенные интервалы приехали
# пробелами.
MAX_SHORT_WORD_SHARE = 0.33

# Символы вне разрешённого набора: иероглифы, боксы, приватная область
# шрифта. Процент — это уже не опечатки.
MAX_ALIEN_SHARE = 0.01

# `\ufffd` — символ, которым Python отмечает не разобравшийся байт. Одна
# тысячная от книги в полмиллиона знаков это пятьсот дыр; больше — кодировка
# угадана неверно.
MAX_REPLACEMENT_SHARE = 0.001

# Средняя длина абзаца. Единицы символов означают, что абзацы не собрались и
# текст приехал лесенкой, — порция тогда режется по середине фразы.
MIN_AVG_PARAGRAPH = 60

# Доля строк, повторяющихся больше десяти раз. Колонтитулы не сняты.
MAX_REPEATED_SHARE = 0.03

# Короче этого — не книга, а обложка, аннотация или обломок извлечения.
MIN_LENGTH = 3000

# Признак скана: символов на страницу меньше, чем бывает у пустой полосы с
# колонтитулом.
SCAN_CHARS_PER_PAGE = 100

# =============================================================================
# Чистка
# =============================================================================
# Доля от медианной длины строки, ниже которой строка считается КОНЦОМ абзаца.
# Классический приём для текста без разметки абзацев: последняя строка абзаца
# короче остальных, потому что абзац кончился раньше края полосы.
PARAGRAPH_SHORT_LINE = 0.80

# Сколько строк с краёв страницы считаются колонтитулом.
HEAD_EDGE_LINES = 2
# На скольких страницах строка должна повториться, чтобы стать колонтитулом.
HEAD_MIN_SHARE = 0.4
# Меньше этого числа страниц — колонтитулы не ищем: на пяти страницах любое
# совпадение случайно.
HEAD_MIN_PAGES = 5
# Доля от медианной длины строки, выше которой строка колонтитулом быть не
# может: колонтитул набирают отдельной короткой строкой, текст идёт во всю
# полосу.
HEAD_MAX_RELATIVE = 0.7

# Разрешённая пунктуация. Всё, что не буква, не цифра, не пробел и не отсюда,
# считается чужим символом.
ALLOWED_PUNCT = set(".,;:!?…«»\"'“”„‘’()[]{}—–-*/\\%№&@#$+=<>|~^§°·`_")

TERMINAL_PUNCT = set(".!?…»\"”’:;")

log = logging.getLogger("convert")


# =============================================================================
# Результаты
# =============================================================================
@dataclass
class Extracted:
    """То, что извлеклось из файла, ДО чистки.

    `pages` — по страницам, а не одной строкой, и это несущее решение: снятие
    колонтитулов возможно только пока видно, где кончилась страница. Склей их
    заранее — и четыреста повторов названия главы окажутся посреди фраз, а
    отличить их от текста будет уже нечем.

    У fb2 и epub страниц нет, и `pages` там — один элемент. Это честно: у них
    есть разметка абзацев, и чистка для них другая (`structured`), ей страницы
    и не нужны.
    """
    pages: list[str]
    kind: str
    tool: str
    title: str | None = None
    author: str | None = None
    structured: bool = False
    note: str | None = None


@dataclass
class Report:
    """Отчёт ворот. Пустые `reasons` — прошло."""
    metrics: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons


# =============================================================================
# Извлечение: fb2
# =============================================================================
FB2_NS = "{http://www.gribuser.net/xml/fictionbook/2.0}"

# Разделы, которые в основной текст НЕ идут. Сноски в fb2 лежат отдельным
# `<body name="notes">`, и это ровно та причина, по которой fb2 лучше PDF:
# там их приходится выковыривать из низа полосы, здесь они уже отделены.
FB2_SKIP_BODIES = {"notes", "comments", "footnotes"}


def _ln(tag) -> str:
    """Локальное имя тега, без пространства имён.

    Разбор НЕ привязан к `FB2_NS`, и это не запас прочности, а исправление
    после первой же встречи с живым файлом. Спецификация fb2 существует в
    версиях 2.0 и 2.1, самиздатовские файлы объявляют то одну, то другую, а
    часть не объявляет пространства имён вовсе. Поиск по полному имени тега
    находил в таких файлах ровно ничего — и сообщал «тело пустое», то есть
    обвинял книгу в том, в чём был виноват разбор.

    Локальное имя одинаково во всех трёх случаях. Цена — теоретическая
    возможность спутать `<p>` из fb2 с `<p>` из вложенного чужого namespace;
    в fb2 такого не бывает, а «тело пустое» бывает.
    """
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _kids(el, name: str) -> list:
    return [c for c in el if _ln(c.tag) == name]


def _dig(el, *path: str) -> list:
    """Спуск по дереву цепочкой локальных имён."""
    current = [el]
    for name in path:
        nxt = []
        for node in current:
            nxt.extend(_kids(node, name))
        current = nxt
    return current


def _fb2_text(el) -> str:
    """Текст элемента без ссылок на сноски.

    `itertext()` не годится: он втянет и номер сноски, который в fb2 лежит
    внутри `<a type="note">`. В тексте это выглядит как случайная цифра,
    приклеенная к слову, — и модель потом честно попытается её осмыслить.
    """
    parts = []
    if el.text:
        parts.append(el.text)
    for child in el:
        if _ln(child.tag) == "a" and child.get("type") == "note":
            pass  # номер сноски выбрасывается вместе с содержимым
        else:
            parts.append(_fb2_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _fb2_blocks(el, out: list[str]) -> None:
    """Рекурсивный обход тела fb2 в блоки текста."""
    for child in el:
        tag = _ln(child.tag)
        if tag == "title":
            title = " ".join(
                t for t in (_fb2_text(p).strip() for p in child)
                if t
            ).strip()
            if title:
                out.append("## " + title)
        elif tag == "subtitle":
            text = _fb2_text(child).strip()
            if text:
                out.append("### " + text)
        elif tag == "p":
            text = _fb2_text(child).strip()
            if text:
                out.append(text)
        elif tag == "empty-line":
            pass  # пустая строка — уже разделитель блоков, второй не нужен
        elif tag == "poem":
            # Стихи собираются со СВОИМИ переводами строк и в чистке не
            # переливаются. Прогони их общим правилом сборки абзацев — и
            # получится проза, набранная в строку.
            lines = []
            for stanza in child:
                stag = _ln(stanza.tag)
                if stag == "stanza":
                    for v in stanza:
                        text = _fb2_text(v).strip()
                        if text:
                            lines.append(text)
                    lines.append("")
                elif stag in ("title", "epigraph", "text-author"):
                    text = _fb2_text(stanza).strip()
                    if text:
                        lines.append(text)
            block = "\n".join(lines).strip()
            if block:
                out.append(block)
        elif tag in ("section", "epigraph", "cite", "annotation"):
            _fb2_blocks(child, out)
        elif tag in ("image", "binary", "table"):
            pass
        else:
            text = _fb2_text(child).strip()
            if text:
                out.append(text)


def _fb2_meta(root) -> tuple[str | None, str | None]:
    found = _dig(root, "description", "title-info")
    if not found:
        return None, None
    info = found[0]
    title_el = _kids(info, "book-title")
    title = (title_el[0].text or "").strip() if title_el else None
    author = None
    author_el = _kids(info, "author")
    if author_el:
        names = []
        for part in ("first-name", "middle-name", "last-name"):
            el = _kids(author_el[0], part)
            if el and (el[0].text or "").strip():
                names.append(el[0].text.strip())
        author = " ".join(names) or None
    return title or None, author


def _read_fb2_bytes(path: Path) -> bytes | None:
    """Байты fb2, в том числе из zip-архива."""
    if path.suffix.lower() != ".zip" and not path.name.lower().endswith(".fb2.zip"):
        return path.read_bytes()
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".fb2")]
            if not names:
                log.warning("%s: в архиве нет fb2", path.name)
                return None
            return z.read(names[0])
    except (zipfile.BadZipFile, OSError) as err:
        log.warning("%s: архив не открылся: %s", path.name, err)
        return None


def _extract_fb2(path: Path) -> Extracted | None:
    raw = _read_fb2_bytes(path)
    if raw is None:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as err:
        # Вторая попытка: у части старых fb2 объявлена одна кодировка, а
        # записана другая. Разбор в cp1251 с выброшенным объявлением —
        # единственный способ, который тут помогает; не помог — отказ.
        try:
            text = raw.decode("cp1251", errors="replace")
            text = re.sub(r"^<\?xml[^>]*\?>", "", text, count=1).strip()
            root = ET.fromstring(text)
            log.info("%s: разобран как cp1251 (объявление кодировки врало)",
                     path.name)
        except (ET.ParseError, UnicodeDecodeError):
            log.warning("%s: не разобрался как XML: %s", path.name, err)
            return None

    title, author = _fb2_meta(root)
    blocks: list[str] = []
    bodies = _kids(root, "body")
    for body in bodies:
        if (body.get("name") or "").lower() in FB2_SKIP_BODIES:
            continue
        _fb2_blocks(body, blocks)
    if not blocks:
        # Сообщение диагностическое, а не отчётное, и это осознанная трата
        # четырёх строк. Прежнее «тело пустое» было верно по факту и
        # бесполезно по существу: оно не позволяло отличить файл без текста от
        # файла, который разбор не понял. Ровно на этом и обожглись —
        # пространство имён оказалось другим, а виновата была «книга».
        log.warning(
            "%s: текста не нашлось. Корень <%s>, внутри: %s; тел: %s%s",
            path.name, _ln(root.tag),
            ", ".join(sorted({_ln(c.tag) for c in root})) or "пусто",
            len(bodies),
            "" if bodies else
            ". Похоже, это не FictionBook — проверьте, что внутри файла",
        )
        return None
    return Extracted(pages=["\n\n".join(blocks)], kind="fb2", tool="fb2/xml",
                     title=title, author=author, structured=True)


# =============================================================================
# Извлечение: epub
# =============================================================================
class _HtmlToBlocks(HTMLParser):
    """HTML -> блоки текста. Своими руками и без зависимостей.

    Полноценный парсер здесь не нужен: от главы epub требуется абзацы и
    заголовки, а таблицы, картинки и стили в чтение не идут. Зависимость ради
    этого была бы третьей после MuPDF и OCR, и самой ненужной.
    """

    BLOCK = {"p", "div", "br", "li", "tr", "blockquote", "section",
             "article", "h1", "h2", "h3", "h4", "h5", "h6", "td", "pre"}
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    SKIP = {"script", "style", "head", "title", "nav", "svg", "figure"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._buf: list[str] = []
        self._skip = 0
        self._heading = False

    def _flush(self):
        text = " ".join("".join(self._buf).split()).strip()
        self._buf = []
        if not text:
            self._heading = False
            return
        self.blocks.append(("## " + text) if self._heading else text)
        self._heading = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
            return
        if tag in self.BLOCK:
            self._flush()
            if tag in self.HEADINGS:
                self._heading = True

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _epub_spine(z: zipfile.ZipFile) -> tuple[list[str], str | None, str | None]:
    """Порядок глав и метаданные из OPF."""
    try:
        container = ET.fromstring(z.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError):
        return [], None, None
    ns = "{urn:oasis:names:tc:opendocument:xmlns:container}"
    rootfile = container.find(f"{ns}rootfiles/{ns}rootfile")
    if rootfile is None:
        return [], None, None
    opf_path = rootfile.get("full-path") or ""
    try:
        opf = ET.fromstring(z.read(opf_path))
    except (KeyError, ET.ParseError):
        return [], None, None

    base = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
    opf_ns = "{http://www.idpf.org/2007/opf}"
    dc_ns = "{http://purl.org/dc/elements/1.1/}"

    title_el = opf.find(f"{opf_ns}metadata/{dc_ns}title")
    author_el = opf.find(f"{opf_ns}metadata/{dc_ns}creator")
    title = (title_el.text or "").strip() if title_el is not None else None
    author = (author_el.text or "").strip() if author_el is not None else None

    manifest = {}
    for item in opf.findall(f"{opf_ns}manifest/{opf_ns}item"):
        manifest[item.get("id")] = (item.get("href"), item.get("media-type"))

    order = []
    for ref in opf.findall(f"{opf_ns}spine/{opf_ns}itemref"):
        href, media = manifest.get(ref.get("idref"), (None, None))
        if not href or "html" not in (media or ""):
            continue
        href = href.split("#")[0]
        order.append(f"{base}/{href}" if base else href)
    return order, title or None, author or None


def _extract_epub(path: Path) -> Extracted | None:
    try:
        with zipfile.ZipFile(path) as z:
            order, title, author = _epub_spine(z)
            if not order:
                log.warning("%s: не нашёл порядок глав (spine)", path.name)
                return None
            blocks: list[str] = []
            for name in order:
                try:
                    raw = z.read(name)
                except KeyError:
                    # Путь в OPF бывает записан с `..` или с процентами.
                    candidates = [n for n in z.namelist()
                                  if n.endswith(name.rsplit("/", 1)[-1])]
                    if not candidates:
                        continue
                    raw = z.read(candidates[0])
                parser = _HtmlToBlocks()
                parser.feed(raw.decode("utf-8", errors="replace"))
                parser.close()
                blocks.extend(parser.blocks)
    except (zipfile.BadZipFile, OSError) as err:
        log.warning("%s: архив не открылся: %s", path.name, err)
        return None
    if not blocks:
        log.warning("%s: текста не нашлось", path.name)
        return None
    return Extracted(pages=["\n\n".join(blocks)], kind="epub", tool="epub/zip",
                     title=title, author=author, structured=True)


# =============================================================================
# Извлечение: pdf
# =============================================================================
def _import_mupdf():
    """MuPDF грузится лениво и с внятным отказом.

    Полка из одних fb2 обязана конвертироваться на голой стандартной
    библиотеке; требовать установки MuPDF ради самого частого случая — налог
    на тех, кто сделал правильный выбор формата.
    """
    try:
        import pymupdf  # noqa: PLC0415
        return pymupdf
    except ImportError:
        try:
            import fitz  # noqa: PLC0415
            return fitz
        except ImportError:
            return None


def _two_columns(blocks, width: float) -> bool:
    """Двухколоночная ли полоса.

    Научные издания набирают в две колонки, и извлечение «по строкам»
    перемешивает их в кашу, которая читается как текст и потому проходит все
    ворота. Это единственная поломка, от которой числа не спасают.

    Признак: блоки собираются в два скопления по краям, а середину почти никто
    не пересекает.
    """
    if len(blocks) < 8 or width <= 0:
        return False
    mid = width / 2
    left = right = spanning = 0
    for x0, _y0, x1, *_rest in blocks:
        center = (x0 + x1) / 2
        if x0 < width * 0.40 and x1 > width * 0.60:
            spanning += 1
        elif center < mid:
            left += 1
        else:
            right += 1
    total = left + right + spanning
    if total == 0:
        return False
    return (left / total > 0.28 and right / total > 0.28
            and spanning / total < 0.15)


def _extract_pdf(path: Path, *, allow_ocr: bool = False) -> Extracted | None:
    mupdf = _import_mupdf()
    if mupdf is None:
        log.warning("%s: PDF требует pymupdf (pip install pymupdf)", path.name)
        return None
    try:
        doc = mupdf.open(path)
    except Exception as err:
        log.warning("%s: PDF не открылся: %s", path.name, err)
        return None

    pages: list[str] = []
    try:
        for page in doc:
            try:
                raw_blocks = [b for b in page.get_text("blocks")
                              if len(b) < 7 or b[6] == 0]
            except Exception:
                raw_blocks = []
            if not raw_blocks:
                pages.append("")
                continue
            width = float(page.rect.width)
            if _two_columns(raw_blocks, width):
                raw_blocks.sort(key=lambda b: (0 if (b[0] + b[2]) / 2 < width / 2 else 1,
                                               round(b[1], 1), b[0]))
            else:
                raw_blocks.sort(key=lambda b: (round(b[1], 1), b[0]))
            pages.append("\n".join((b[4] or "").strip() for b in raw_blocks))
        meta = doc.metadata or {}
    finally:
        doc.close()

    chars = sum(len(p) for p in pages)
    per_page = chars / max(1, len(pages))
    if per_page < SCAN_CHARS_PER_PAGE:
        if not allow_ocr:
            log.warning("%s: похоже на скан (%.0f знаков на страницу). "
                        "Нужен --ocr", path.name, per_page)
            return None
        return _extract_scan(path)

    return Extracted(
        pages=pages, kind="pdf", tool="pymupdf/blocks",
        title=(meta.get("title") or "").strip() or None,
        author=(meta.get("author") or "").strip() or None,
    )


def _extract_scan(path: Path) -> Extracted | None:
    """OCR через ocrmypdf: он кладёт текстовый слой в сам PDF, и дальше путь
    обычный. Свой вызов tesseract постранично дал бы то же самое хуже — без
    выравнивания страниц и без разбора колонок."""
    if shutil.which("ocrmypdf") is None:
        log.warning("%s: скан, но ocrmypdf не установлен "
                    "(apt install ocrmypdf tesseract-ocr-rus)", path.name)
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ocr.pdf"
        log.info("%s: OCR, это надолго", path.name)
        try:
            subprocess.run(
                ["ocrmypdf", "--language", "rus+eng", "--force-ocr",
                 "--quiet", str(path), str(out)],
                check=True, timeout=3 * 3600,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
            log.warning("%s: OCR не удался: %s", path.name, err)
            return None
        got = _extract_pdf(out, allow_ocr=False)
    if got is None:
        return None
    got.kind = "scan"
    got.tool = "ocrmypdf+pymupdf"
    got.note = "OCR: текст распознан, а не извлечён — ошибки вероятны"
    return got


# =============================================================================
# Извлечение: djvu и txt
# =============================================================================
def _extract_djvu(path: Path, *, allow_ocr: bool = False) -> Extracted | None:
    """У русских сканов DjVu встречается не реже PDF, и текстовый слой в нём
    бывает чаще, чем ожидаешь. Сначала пробуем его, и только потом OCR."""
    if shutil.which("djvutxt") is None:
        log.warning("%s: DjVu требует djvulibre (apt install djvulibre-bin)",
                    path.name)
        return None
    try:
        got = subprocess.run(["djvutxt", str(path)], check=True,
                             capture_output=True, timeout=600)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        log.warning("%s: djvutxt не справился: %s", path.name, err)
        return None
    text = got.stdout.decode("utf-8", errors="replace")
    pages = text.split("\f")
    per_page = len(text) / max(1, len(pages))
    if per_page < SCAN_CHARS_PER_PAGE:
        if not allow_ocr:
            log.warning("%s: DjVu без текстового слоя. Нужен --ocr "
                        "(и ddjvu для перегона в PDF)", path.name)
            return None
        return _djvu_ocr(path)
    return Extracted(pages=pages, kind="djvu", tool="djvutxt")


def _djvu_ocr(path: Path) -> Extracted | None:
    if shutil.which("ddjvu") is None:
        log.warning("%s: для OCR нужен ddjvu (djvulibre-bin)", path.name)
        return None
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "from-djvu.pdf"
        try:
            subprocess.run(["ddjvu", "-format=pdf", "-quality=85",
                            str(path), str(pdf)],
                           check=True, capture_output=True, timeout=3600)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
            log.warning("%s: ddjvu не справился: %s", path.name, err)
            return None
        return _extract_scan(pdf)


def _extract_txt(path: Path) -> Extracted | None:
    raw = path.read_bytes()
    for encoding in ("utf-8", "cp1251", "koi8-r"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    # Чистка как у потокового формата: в txt разметки абзацев нет, а
    # переносы и лесенка бывают — это часто выгрузка того же PDF.
    return Extracted(pages=[text], kind="txt", tool="txt/decode")


def extract(path: Path, *, allow_ocr: bool = False) -> Extracted | None:
    kind = _kind_of(path)
    if kind == "fb2":
        return _extract_fb2(path)
    if kind == "epub":
        return _extract_epub(path)
    if kind == "pdf":
        return _extract_pdf(path, allow_ocr=allow_ocr)
    if kind == "djvu":
        return _extract_djvu(path, allow_ocr=allow_ocr)
    if kind == "txt":
        return _extract_txt(path)
    return None


def _kind_of(path: Path) -> str | None:
    name = path.name.lower()
    for suffix, kind in KNOWN_SUFFIXES.items():
        if name.endswith(suffix):
            return kind
    return None


# =============================================================================
# Чистка
# =============================================================================
def _normalize_chars(text: str) -> str:
    """Символьная нормализация. ОДНА на проект и на все форматы.

    Позиция чтения считается в символах этого текста. Нормализуй по-разному —
    и книга, перегнанная заново, поедет относительно записанной позиции, а
    заметки станут указывать не на те места.
    """
    text = text.replace("\ufeff", "")
    text = unicodedata.normalize("NFC", text)
    # Мягкий перенос — невидимый символ, который ломает и поиск, и длину.
    text = text.replace("\u00ad", "")
    for space in ("\u00a0", "\u2007", "\u202f", "\u2009", "\u200a", "\u2002",
                  "\u2003"):
        text = text.replace(space, " ")
    text = text.replace("\u200b", "").replace("\u2060", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Лигатуры: их кладут в PDF шрифты, а ищутся они потом как отдельные слова.
    for src, dst in (("ﬁ", "fi"), ("ﬂ", "fl"), ("ﬀ", "ff"), ("ﬃ", "ffi"),
                     ("ﬄ", "ffl"), ("\u2026", "…")):
        text = text.replace(src, dst)
    # Пробелы внутри строки схлопываются, по краям снимаются. Табуляция — тоже
    # пробел: отступом абзаца она в извлечённом тексте не бывает.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text


def _head_key(line: str) -> str:
    """Ключ строки для поиска колонтитулов: без цифр и без регистра.

    Без цифр — потому что колонтитул почти всегда несёт номер страницы, и
    «Глава пятая 137» с «Глава пятая 138» обязаны считаться одной строкой.
    """
    key = re.sub(r"\d+", "", line).strip().lower()
    key = re.sub(r"\s+", " ", key)
    return key


_PAGE_NUMBER = re.compile(r"^[\s\-—–•.]*\d{1,4}[\s\-—–•.]*$")
_ROMAN = re.compile(r"^[\s\-—–]*[ivxlcdm]{1,7}[\s\-—–.]*$", re.IGNORECASE)


def _is_page_furniture(line: str, heads: set[str]) -> bool:
    return bool(_PAGE_NUMBER.match(line)
                or (len(line) <= 8 and _ROMAN.match(line))
                or _head_key(line) in heads)


def _strip_running_heads(pages: list[str]) -> tuple[list[str], int]:
    """Снять колонтитулы и номера страниц.

    Делается ДО склейки страниц и только с краёв: строка, повторяющаяся в
    середине полосы, — это припев, а не колонтитул, и трогать её нельзя.

    **Пустые строки сохраняются.** Для PDF они ничего не значат, а для txt это
    единственная разметка абзацев, какая есть, — выброси их здесь, и сборка
    абзацев ниже склеит книгу в одно полотно. Ошибка была ровно такой и
    вылезла на первом же прогоне: сорок абзацев превратились в восемнадцать.
    """
    edges = [_edge_positions(page) for page in pages]

    # Колонтитул КОРОТОК, и это второе условие после повторяемости.
    #
    # Одной повторяемости мало, и цена ошибки высока: `_head_key` снимает
    # цифры, чтобы «Глава пятая 137» и «Глава пятая 138» считались одной
    # строкой, — а вместе с ними одной строкой становятся любые две строки,
    # различающиеся только числом. На проверке это съело первую строку каждой
    # страницы, то есть кусок книги, и съело молча: ворота такого не видят,
    # потому что оставшийся текст безупречен.
    #
    # Длина разводит их надёжно. Колонтитул — название, набранное отдельной
    # строкой; строка основного текста идёт во всю полосу. Порог относительный,
    # потому что абсолютный зависел бы от кегля и формата.
    limit = _median_line("\n".join(pages)) * HEAD_MAX_RELATIVE

    heads: set[str] = set()
    if len(pages) >= HEAD_MIN_PAGES:
        counts: Counter = Counter()
        for page, (lines, edge_idx) in zip(pages, edges):
            keys = {_head_key(lines[i]) for i in edge_idx
                    if len(lines[i].strip()) <= limit}
            for key in keys:
                if key:
                    counts[key] += 1
        threshold = max(3, int(len(pages) * HEAD_MIN_SHARE))
        heads = {key for key, count in counts.items() if count >= threshold}

    removed = 0
    cleaned = []
    for page, (lines, edge_idx) in zip(pages, edges):
        keep = []
        for i, line in enumerate(page.split("\n")):
            stripped = line.strip()
            if (stripped and i in edge_idx and len(stripped) <= limit
                    and _is_page_furniture(stripped, heads)):
                removed += 1
                continue
            keep.append(stripped)
        cleaned.append("\n".join(keep))
    return cleaned, removed


def _edge_positions(page: str) -> tuple[list[str], set[int]]:
    """Строки страницы и НОМЕРА тех из них, что стоят по краям.

    Считается по непустым строкам, а применяется к исходной нумерации: иначе
    пустая строка в начале полосы сдвинула бы край и колонтитул перестал бы им
    быть.
    """
    lines = page.split("\n")
    filled = [i for i, l in enumerate(lines) if l.strip()]
    edge = set(filled[:HEAD_EDGE_LINES]) | set(filled[-HEAD_EDGE_LINES:])
    return lines, edge


# Частицы, которые пишутся через дефис ВСЕГДА. Слитной формы у них не бывает
# («чтото», «какнибудь» — не слова), поэтому решение однозначное и словаря не
# требует.
HYPHEN_SUFFIXES = frozenset({"то", "либо", "нибудь", "ка", "таки", "де"})

_HYPHEN_BREAK = re.compile(r"([^\W\d_]+)[-\u2010]\n([^\W\d_]+)")
_WORD_TOKEN = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")


def _dehyphenate(text: str) -> str:
    """Склейка переносов. Книга служит себе словарём.

    Случая два, и различать их обязательно:

        `пере-\\nнос`        -> `перенос`         (дефис был переносом)
        `как-\\nнибудь`      -> `как-нибудь`      (дефис был дефисом)
        `Санкт-\\nПетербург` -> `Санкт-Петербург` (то же, и видно по регистру)

    Регистра следующей буквы для этого МАЛО, и это выяснилось на первой же
    проверке: `как-\\nнибудь` склеилось в `какнибудь`, потому что «нибудь»
    начинается со строчной. Слово при этом выглядит правдоподобно —
    ошибка из тех, что доезжают до промпта и не замечаются.

    Словарь брать неоткуда и не нужно: **у книги он свой.** Слово, разорванное
    на одной странице, почти наверняка целиком встречается на другой, и по
    этому вхождению видно, был там дефис или нет. Приём не требует ни
    зависимостей, ни языка: он одинаково работает для русского и английского.

    Порядок проверок — от однозначного к вероятному:

        1. следующая часть с заглавной          -> дефис (имя собственное);
        2. дефисная форма есть в книге          -> дефис;
        3. слитная форма есть в книге           -> склейка;
        4. вторая часть — дефисная частица      -> дефис;
        5. иначе                                -> склейка.

    Пятый пункт — то, чем перенос бывает чаще всего, и ошибка здесь стоит
    одного слитного слова вместо дефисного. Это правильная сторона: склеенное
    слово читается, а лишний дефис в середине фразы сбивает разбор.
    """
    tokens = _WORD_TOKEN.findall(text.replace("\n", " "))
    hyphenated = {t.lower() for t in tokens if "-" in t}
    plain = {t.lower() for t in tokens if "-" not in t}

    def decide(m: re.Match) -> str:
        left, right = m.group(1), m.group(2)
        if right[:1].isupper():
            return f"{left}-{right}"
        joined = f"{left}-{right}".lower()
        if joined in hyphenated:
            return f"{left}-{right}"
        if f"{left}{right}".lower() in plain:
            return f"{left}{right}"
        if right.lower() in HYPHEN_SUFFIXES:
            return f"{left}-{right}"
        return f"{left}{right}"

    return _HYPHEN_BREAK.sub(decide, text)


def _median_line(text: str) -> float:
    lines = sorted(len(l) for l in text.split("\n") if l.strip())
    if not lines:
        return 0.0
    return float(lines[len(lines) // 2])


def _reflow(text: str) -> str:
    """Собрать абзацы из строк.

    Нужна только потоковым форматам (PDF, DjVu, txt): у fb2 и epub абзацы уже
    размечены, и прогонять их через эту функцию значило бы переверстать стихи
    в прозу.

    Правило конца абзаца: предыдущая строка кончилась точкой И оказалась
    заметно короче медианной. Короткая последняя строка — единственный
    надёжный признак конца абзаца в тексте, набранном на всю ширину полосы;
    одной только точки мало, потому что точки стоят и в середине абзаца.
    """
    median = _median_line(text)
    short = median * PARAGRAPH_SHORT_LINE if median else 0

    out: list[str] = []
    buf: list[str] = []
    prev: str | None = None

    def flush():
        if buf:
            out.append(" ".join(buf).strip())
            buf.clear()

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            flush()
            prev = None
            continue
        if line.startswith("#"):
            flush()
            out.append(line)
            prev = None
            continue
        if prev is not None and _breaks(prev, line, short):
            flush()
        buf.append(line)
        prev = line
    flush()
    return "\n\n".join(p for p in out if p)


def _breaks(prev: str, cur: str, short: float) -> bool:
    # Прямая речь начинается с тире и всегда с новой строки — это единственный
    # случай, где разрыв виден по СЛЕДУЮЩЕЙ строке, а не по предыдущей.
    if cur[:1] in ("—", "–", "―"):
        return True
    if cur.startswith("#"):
        return True
    if prev[-1:] in TERMINAL_PUNCT and len(prev) < short:
        return True
    # Строка заметно короче прочих и без знака препинания — обычно заголовок,
    # набранный отдельной строкой. Разрываем, но заголовком не помечаем:
    # угадывать разметку по длине строки — это уже сочинение.
    if len(prev) < short * 0.55 and prev[-1:] not in ",;-—":
        return True
    return False


def _collapse_blanks(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def clean(got: Extracted) -> tuple[str, dict]:
    """Извлечённое -> чистый текст. Возвращает текст и что было сделано."""
    trace: dict = {"profile": "structured" if got.structured else "flowed"}

    pages = [_normalize_chars(p) for p in got.pages]

    if got.structured:
        text = "\n\n".join(p for p in pages if p.strip())
        text = _dehyphenate(text)
        trace["heads_removed"] = 0
    else:
        pages, removed = _strip_running_heads(pages)
        trace["heads_removed"] = removed
        text = "\n".join(p for p in pages if p.strip())
        text = _dehyphenate(text)
        text = _reflow(text)

    text = _collapse_blanks(text)
    trace["length"] = len(text)
    return text, trace


# =============================================================================
# Ворота
# =============================================================================
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _script_of(ch: str) -> str | None:
    lower = ch.lower()
    if "а" <= lower <= "я" or lower in ("ё", "ѣ", "і", "ъ", "ь"):
        return "cyr"
    if "a" <= lower <= "z":
        return "lat"
    return None


# Сколько раз строка должна повториться, чтобы стать подозрительной, и какой
# длины она при этом бывает. Оба числа описывают КОЛОНТИТУЛ, а не повтор
# вообще: он повторяется примерно по разу на страницу и всегда короче абзаца.
REPEAT_MIN = 10
REPEAT_MAX_LEN = 80


def _repeated_lines(lines: list[str]) -> int:
    """Сколько строк выглядит недоснятым колонтитулом.

    Просто «повторяется часто» не годится, и это выяснилось на первой же
    проверке. В романе десятки раз повторяется реплика вроде «— Да.», и по
    голому счётчику книга с диалогами неотличима от книги с колонтитулами.

    Различает их знак в конце: колонтитул — это название, оно не кончается
    точкой; реплика — предложение, и кончается. Признак грубый, но ошибается в
    сторону пропуска колонтитула, а не выбраковки живой книги, — и это
    правильная сторона, потому что цена ошибки несимметрична: пропущенный
    колонтитул виден глазом в файле, а забракованная книга просто не доедет до
    персонажа и молча.
    """
    counts = Counter(lines)
    return sum(
        count for line, count in counts.items()
        if count >= REPEAT_MIN
        and len(line) <= REPEAT_MAX_LEN
        and line[-1:] not in TERMINAL_PUNCT
    )


def measure(text: str) -> dict:
    """Числа о тексте. Ни одного суждения — только измерения."""
    chars = len(text)
    if chars == 0:
        return {"length": 0}

    letters = 0
    scripts: Counter = Counter()
    alien = 0
    for ch in text:
        if ch.isalpha():
            letters += 1
            script = _script_of(ch)
            scripts[script or "other"] += 1
            if script is None:
                alien += 1
        elif not (ch.isdigit() or ch.isspace() or ch in ALLOWED_PUNCT):
            alien += 1

    words = _WORD.findall(text)
    short = sum(1 for w in words if len(w) <= 2)
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    repeated = _repeated_lines(lines)

    dominant, dominant_count = ("none", 0)
    for name in ("cyr", "lat"):
        if scripts[name] > dominant_count:
            dominant, dominant_count = name, scripts[name]

    return {
        "length": chars,
        "letter_share": round(letters / chars, 4),
        "script": dominant,
        "script_share": round(dominant_count / letters, 4) if letters else 0.0,
        "short_word_share": round(short / len(words), 4) if words else 1.0,
        "alien_share": round(alien / chars, 5),
        "replacement_share": round(text.count("\ufffd") / chars, 6),
        "avg_paragraph": round(
            sum(len(p) for p in paragraphs) / len(paragraphs), 1)
        if paragraphs else 0.0,
        "paragraphs": len(paragraphs),
        "repeated_share": round(repeated / len(lines), 4) if lines else 0.0,
    }


def judge(metrics: dict) -> Report:
    """Измерения -> вердикт. Каждая причина названа так, чтобы по ней было
    понятно, ЧТО чинить, а не что «книга плохая»."""
    reasons = []
    m = metrics

    if m.get("length", 0) < MIN_LENGTH:
        reasons.append(
            f"слишком коротко: {m.get('length', 0)} знаков "
            f"(порог {MIN_LENGTH}) — похоже, извлеклась не книга, а обломок")
    if m.get("letter_share", 1) < MIN_LETTER_SHARE:
        reasons.append(
            f"мало букв: {m['letter_share']:.2f} (порог {MIN_LETTER_SHARE}) — "
            "обычно это битый CMap в PDF: символы есть, буквами не являются")
    if m.get("script_share", 1) < MIN_SCRIPT_SHARE:
        reasons.append(
            f"алфавит не однороден: {m.get('script')} {m['script_share']:.2f} "
            f"(порог {MIN_SCRIPT_SHARE}) — похоже на подмену кодировки")
    if m.get("short_word_share", 0) > MAX_SHORT_WORD_SHARE:
        reasons.append(
            f"слишком много коротких слов: {m['short_word_share']:.2f} "
            f"(порог {MAX_SHORT_WORD_SHARE}) — рассыпались пробелы")
    if m.get("alien_share", 0) > MAX_ALIEN_SHARE:
        reasons.append(
            f"чужие символы: {m['alien_share']:.4f} "
            f"(порог {MAX_ALIEN_SHARE})")
    if m.get("replacement_share", 0) > MAX_REPLACEMENT_SHARE:
        reasons.append(
            f"неразобранные байты: {m['replacement_share']:.5f} "
            f"(порог {MAX_REPLACEMENT_SHARE}) — кодировка угадана неверно")
    if m.get("avg_paragraph", 0) < MIN_AVG_PARAGRAPH:
        reasons.append(
            f"абзацы не собрались: в среднем {m['avg_paragraph']:.0f} знаков "
            f"(порог {MIN_AVG_PARAGRAPH}) — текст приехал лесенкой")
    if m.get("repeated_share", 0) > MAX_REPEATED_SHARE:
        reasons.append(
            f"повторяющиеся строки: {m['repeated_share']:.3f} "
            f"(порог {MAX_REPEATED_SHARE}) — колонтитулы не сняты")
    return Report(metrics=metrics, reasons=reasons)


# =============================================================================
# Имена файлов
# =============================================================================
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slug(author: str | None, title: str | None, fallback: str) -> str:
    """Имя файла в text/. Латиницей — не из вкуса, а чтобы путь в базе не
    зависел от локали файловой системы и переживал перенос между машинами."""
    parts = [p for p in (author, title) if p]
    raw = " ".join(parts) if parts else fallback
    out = []
    for ch in raw.lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum() and ch.isascii():
            out.append(ch)
        else:
            out.append("-")
    name = re.sub(r"-+", "-", "".join(out)).strip("-")
    return (name or "book")[:80]


def _guess_meta(path: Path, got: Extracted) -> tuple[str, str | None]:
    """Заголовок и автор: из метаданных, иначе из имени файла.

    Модель здесь НЕ зовётся намеренно. Между вашей полкой и памятью персонажа
    не должно стоять ещё одного источника выдумки: книга, которую он читает,
    обязана называться так, как называется.

    Разбирается одна форма имени — `Автор - Название`, самая частая на русских
    полках. Не разобралось — заголовком становится имя файла, и это видно
    глазом в первой же строке вывода.
    """
    title = (got.title or "").strip()
    author = (got.author or "").strip() or None
    if title:
        return title, author
    stem = path.name
    for suffix in sorted(KNOWN_SUFFIXES, key=len, reverse=True):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = stem.replace("_", " ").strip()
    if " - " in stem:
        left, right = stem.split(" - ", 1)
        return right.strip(), (author or left.strip())
    return stem, author


# =============================================================================
# Конвейер
# =============================================================================
@dataclass
class Outcome:
    source: Path
    ok: bool
    slug: str | None = None
    title: str | None = None
    report: Report | None = None
    error: str | None = None


def convert_file(path: Path, root: Path, *, allow_ocr: bool = False,
                 force: bool = False, accept: bool = False) -> Outcome:
    text_dir = root / TEXT_DIR
    reject_dir = root / REJECT_DIR
    text_dir.mkdir(parents=True, exist_ok=True)
    reject_dir.mkdir(parents=True, exist_ok=True)

    got = extract(path, allow_ocr=allow_ocr)
    if got is None:
        return Outcome(path, False, error="не извлеклось")

    title, author = _guess_meta(path, got)
    base = slug(author, title, path.stem)

    text, trace = clean(got)

    # Занято ли имя, и если да — кем. Случай не выдуманный: одна и та же книга
    # лежит на полке и в fb2, и в pdf, и из обоих выходит один заголовок.
    name, state = _claim(text_dir, base, path, root, text)
    if state == "дубликат":
        return Outcome(path, True, slug=name, title=title,
                       error=f"тот же текст уже сконвертирован: {name}.md")
    if state == "занято" and not force:
        return Outcome(path, True, slug=name, title=title,
                       error="уже сконвертировано (--force чтобы заново)")

    report = judge(measure(text))

    meta = {
        "title": title,
        "author": author,
        "source_path": str(path.relative_to(root)) if _under(path, root) else str(path),
        "source_kind": got.kind,
        "tool": got.tool,
        "converted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "length": len(text),
        "clean": trace,
        "quality": report.metrics,
        "verdict": "ok" if (report.ok or accept) else "reject",
        "reasons": report.reasons,
    }
    if got.note:
        meta["note"] = got.note
    if accept and not report.ok:
        meta["accepted_anyway"] = True

    where = text_dir if (report.ok or accept) else reject_dir
    # `newline="\n"` — перевод строк НЕ транслируется под платформу. Позиция
    # чтения считается в символах этого файла, и `\r\n` на windows сдвинул бы
    # её ровно на число строк: молча, необратимо и уже после того, как
    # персонаж начал читать.
    with open(where / f"{name}.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    with open(where / f"{name}.json", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(meta, ensure_ascii=False, indent=2))

    # Отвергнутое не должно остаться в text/ с прошлого прогона: иначе
    # `--force` после правки порогов оставил бы каталогу книгу, которую сам же
    # только что забраковал.
    if where is reject_dir:
        for stale in (text_dir / f"{name}.md", text_dir / f"{name}.json"):
            if stale.exists():
                stale.unlink()

    return Outcome(path, report.ok or accept, slug=name, title=title,
                   report=report)


def _claim(text_dir: Path, base: str, source: Path, root: Path,
           text: str) -> tuple[str, str]:
    """Занять имя в `text/`. Возвращает имя и состояние.

    Три исхода, и различать их обязательно:

        'свободно'  — имени нет, берём;
        'занято'    — лежит книга ИЗ ЭТОГО ЖЕ источника: повторный прогон,
                      и без `--force` делать нечего;
        'дубликат'  — лежит тот же самый текст из ДРУГОГО источника.

    Последнее — не редкость, а норма полки: одна книга в fb2 и в pdf даёт один
    заголовок и один slug. Класть её дважды нельзя (персонаж выберет «вторую»
    и начнёт читать прочитанное), и молча перезаписывать тоже нельзя — из двух
    источников лучше тот, что чище, а какой это, решает не порядок обхода.

    Сравнение по содержимому, а не по имени источника: имя файла на полке
    меняется от переименования, текст — нет.
    """
    same = str(source.relative_to(root)) if _under(source, root) else str(source)
    candidates = [base] + [f"{base}-{i}" for i in range(2, 10)]
    for name in candidates:
        md = text_dir / f"{name}.md"
        if not md.exists():
            return name, "свободно"
        try:
            meta = json.loads((text_dir / f"{name}.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if meta.get("source_path") == same:
            return name, "занято"
        if md.read_text("utf-8") == text:
            return name, "дубликат"
    return f"{base}-{abs(hash(same)) % 10000}", "свободно"


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def scan_sources(root: Path) -> list[Path]:
    source = root / SOURCE_DIR
    if not source.exists():
        return []
    found = [p for p in sorted(source.rglob("*"))
             if p.is_file() and _kind_of(p)]
    return found


def recheck(root: Path) -> list[Outcome]:
    """Перемерить то, что уже лежит в text/.

    Нужно после правки порогов: числа меняются на живой полке, и увидеть, кого
    новый порог выбрасывает, надо ДО того, как персонаж возьмёт такую книгу.
    Файлы при этом не трогаются — только отчёт.
    """
    out = []
    for path in sorted((root / TEXT_DIR).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        report = judge(measure(text))
        out.append(Outcome(path, report.ok, slug=path.stem, report=report))
    return out


# =============================================================================
# Печать
# =============================================================================
def _print_outcome(o: Outcome) -> None:
    mark = "  ok  " if o.ok else " ОТКАЗ"
    name = o.slug or o.source.name
    print(f"[{mark}] {name}")
    if o.title:
        print(f"         {o.title}")
    if o.error:
        print(f"         {o.error}")
    if o.report:
        m = o.report.metrics
        print(f"         {m.get('length', 0)} знаков, "
              f"{m.get('paragraphs', 0)} абзацев, "
              f"алфавит {m.get('script')} {m.get('script_share', 0):.2f}, "
              f"абзац в среднем {m.get('avg_paragraph', 0):.0f}")
        for reason in o.report.reasons:
            print(f"         - {reason}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Конвертер библиотеки: fb2/epub/pdf/djvu -> чистый текст.")
    parser.add_argument("--library", default=str(LIBRARY_DIR),
                        help="корень библиотеки (по умолчанию ./library)")
    parser.add_argument("--file", help="одна книга вместо всей полки")
    parser.add_argument("--force", action="store_true",
                        help="конвертировать заново, даже если файл уже есть")
    parser.add_argument("--ocr", action="store_true",
                        help="разрешить OCR для сканов (долго)")
    parser.add_argument("--accept", action="store_true",
                        help="положить в text/ вопреки воротам качества")
    parser.add_argument("--check", action="store_true",
                        help="перемерить то, что уже сконвертировано")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = Path(args.library).expanduser().resolve()

    if args.check:
        outcomes = recheck(root)
        if not outcomes:
            print(f"в {root / TEXT_DIR} пусто")
            return 0
        for o in outcomes:
            _print_outcome(o)
        bad = sum(1 for o in outcomes if not o.ok)
        print(f"\nпроверено {len(outcomes)}, "
              f"перестало проходить ворота: {bad}")
        return 1 if bad else 0

    if args.accept:
        print("ВНИМАНИЕ: --accept кладёт книгу в каталог вопреки воротам.\n"
              "Записанное из неё ляжет в биографию навсегда. Посмотрите \n"
              "середину файла глазами, прежде чем персонаж её возьмёт.\n")

    if args.file:
        paths = [Path(args.file).expanduser().resolve()]
        if not paths[0].exists():
            print(f"нет такого файла: {paths[0]}")
            return 2
    else:
        paths = scan_sources(root)
        if not paths:
            print(f"в {root / SOURCE_DIR} нет файлов известных форматов.\n"
                  "Для русских книг лучший исходник — fb2: абзацы размечены, "
                  "сноски отделены, переносов нет.")
            return 0

    ok = failed = 0
    for path in paths:
        try:
            outcome = convert_file(path, root, allow_ocr=args.ocr,
                                   force=args.force, accept=args.accept)
        except Exception as err:          # край: одна книга не роняет полку
            log.exception("%s: неожиданная ошибка", path.name)
            outcome = Outcome(path, False, error=str(err))
        _print_outcome(outcome)
        if outcome.ok:
            ok += 1
        else:
            failed += 1

    print(f"\nготово: {ok}, отказов: {failed}")
    print(f"читать отсюда: {root / TEXT_DIR}")
    if failed:
        print(f"отчёты по отказам: {root / REJECT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
