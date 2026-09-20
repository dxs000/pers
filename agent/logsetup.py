"""Файл лога демона. Веб читает тот же путь (`AGENT_LOG`), в память не пишет."""

from __future__ import annotations

import logging
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
    return path
