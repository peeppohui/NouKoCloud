"""Логирование: подробно в файл, кратко и по-человечески — в интерфейс."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .paths import logs_dir

_CONFIGURED = False


class _PlainFormatter(logging.Formatter):
    """Формат файла логов — без цветов, с временем и модулем."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s  %(levelname)-7s %(name)-28s %(message)s", "%Y-%m-%d %H:%M:%S")


class _ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(levelname)s: %(message)s")


def setup_logging(level: int = logging.INFO) -> Path:
    """Настраивает ротацию файлов. Возвращает путь к текущему логу."""
    global _CONFIGURED
    if _CONFIGURED:
        return logs_dir() / "svscloud.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    log_file = logs_dir() / "svscloud.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_PlainFormatter())
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)

    # httpx на уровне INFO печатает каждый запрос — лишний шум.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _CONFIGURED = True
    return log_file
