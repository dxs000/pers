"""Край эссе: файл на диске. Модели нет, часов нет, базы нет.

Правило то же, что у library: любая неудача — None, демон жив.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import config

log = logging.getLogger("outbox")

_SLUG = re.compile(r"[^\w]+", re.UNICODE)


def root() -> Path:
    return Path(config.OUTBOX_DIR)


def slug(title: str) -> str:
    s = _SLUG.sub("-", (title or "").strip().lower()).strip("-")
    return (s[:60] or "essay")


def path_for(title: str) -> Path:
    base = slug(title)
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / f"{base}.md"
    n = 2
    while candidate.exists():
        candidate = folder / f"{base}-{n}.md"
        n += 1
    return candidate


def append(path: Path, text: str, *, header: str | None = None) -> int | None:
    """Дописать кусок. Возвращает число записанных знаков или None."""
    body = (text or "").strip()
    if not body:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists() and path.stat().st_size > 0
        with open(path, "a", encoding="utf-8", newline="") as fh:
            if not existed and header:
                fh.write(header.rstrip() + "\n\n")
            if existed:
                fh.write("\n\n")
            fh.write(body)
            fh.write("\n")
        return len(body)
    except OSError as err:
        log.warning("outbox: не записался %s: %s", path, err)
        return None
