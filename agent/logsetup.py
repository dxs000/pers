"""Файл лога демона. Веб читает тот же путь (`AGENT_LOG`), в память не пишет."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import config


def attach() -> Path | None:
    path: Path = config.AGENT_LOG
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.info("лог пишется в %s", path)
    _log_uncaught()
    return path


def _log_uncaught() -> None:
    """Необработанное исключение — в тот же лог, а не только в stderr.

    Без этого демон, упавший на старте, оставлял в `agent.log` одни строки
    запуска: трейсбек уходил в stderr, который под супервизором читают
    редко, и перезапуск по кругу выглядел как загадка.
    """
    previous = sys.excepthook

    def hook(kind, value, tb):
        if not issubclass(kind, KeyboardInterrupt):
            logging.critical("необработанное исключение, демон остановлен",
                             exc_info=(kind, value, tb))
        previous(kind, value, tb)

    sys.excepthook = hook
