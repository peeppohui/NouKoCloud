"""Логирование: подробно в файл, ошибки — отдельно, кратко — в интерфейс.

Файлы в %APPDATA%\\NoukoCloud\\logs:

* noukocloud.log — всё подряд (что делала программа);
* errors.log     — только предупреждения и ошибки, каждая с полным путём
  по коду (traceback) и номером версии. Его стоит открыть первым, если
  «что-то пошло не так»: там видно, где и почему;
* crash.log      — аварийные вылеты самого Python/Qt (faulthandler),
  когда программа закрывается без всякого сообщения.

Ошибки ловятся отовсюду: в главном потоке (sys.excepthook), в других
потоках (threading.excepthook), в фоновом asyncio-цикле (см.
async_runtime) и предупреждения самого Qt (qInstallMessageHandler).
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import platform
import sys
import threading
from pathlib import Path

from .paths import logs_dir

_CONFIGURED = False
_crash_file = None

LOG_NAME = "noukocloud.log"
ERRORS_NAME = "errors.log"
CRASH_NAME = "crash.log"

log = logging.getLogger("app")


class _PlainFormatter(logging.Formatter):
    """Формат файла логов — без цветов, с временем и модулем."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s  %(levelname)-7s %(name)-28s %(message)s", "%Y-%m-%d %H:%M:%S")


class _ErrorFormatter(logging.Formatter):
    """Журнал ошибок: где (файл:строка, функция, поток) и что."""

    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s  %(levelname)s  [%(threadName)s] %(name)s "
            "(%(filename)s:%(lineno)d, %(funcName)s)\n    %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        # Отступ у traceback и между записями — чтобы читалось глазами.
        return text.replace("\nTraceback", "\n    Traceback") + "\n"


class _ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(levelname)s: %(message)s")


def setup_logging(level: int = logging.INFO) -> Path:
    """Настраивает журналы и перехват ошибок. Возвращает путь к основному журналу."""
    global _CONFIGURED
    folder = logs_dir()
    if _CONFIGURED:
        return folder / LOG_NAME

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    file_handler = logging.handlers.RotatingFileHandler(
        folder / LOG_NAME, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=False
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_PlainFormatter())
    root.addHandler(file_handler)

    errors_handler = logging.handlers.RotatingFileHandler(
        folder / ERRORS_NAME, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    errors_handler.setLevel(logging.WARNING)
    errors_handler.setFormatter(_ErrorFormatter())
    root.addHandler(errors_handler)

    # В собранном .exe окна консоли нет (sys.stderr is None) — туда не пишем.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(_ConsoleFormatter())
        root.addHandler(console)

    # httpx на уровне INFO печатает каждый запрос — лишний шум.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _install_hooks(folder)
    _CONFIGURED = True
    log.info("Версия: %s · Windows: %s · Python %s", _version(), platform.platform(), platform.python_version())
    return folder / LOG_NAME


def errors_log_path() -> Path:
    return logs_dir() / ERRORS_NAME


def _version() -> str:
    try:
        from .. import __version__

        return __version__
    except Exception:
        return "?"


def _install_hooks(folder: Path) -> None:
    def excepthook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("app.crash").critical(
            "Необработанная ошибка (версия %s)", _version(), exc_info=(exc_type, exc, tb))

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        logging.getLogger("app.crash").critical(
            "Необработанная ошибка в потоке %s (версия %s)", name, _version(),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook

    # Аварийный вылет (access violation в Qt/FFmpeg) Python не перехватит —
    # faulthandler хотя бы запишет, в каком месте кода это случилось.
    global _crash_file
    try:
        _crash_file = open(folder / CRASH_NAME, "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(_crash_file, all_threads=True)
    except Exception:
        _crash_file = None


def install_qt_message_handler() -> None:
    """Предупреждения и ошибки самого Qt — тоже в журнал ошибок."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return
    qt_log = logging.getLogger("qt")
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    # Шум, который не говорит ни о какой ошибке программы.
    noise = ("QFont::setPointSizeF", "propagateSizeHints", "Unknown property")

    def handler(msg_type, context, message) -> None:
        if any(part in message for part in noise):
            return
        where = ""
        if context is not None and getattr(context, "file", None):
            where = f" ({context.file}:{context.line})"
        qt_log.log(levels.get(msg_type, logging.WARNING), "%s%s", message, where)

    qInstallMessageHandler(handler)
