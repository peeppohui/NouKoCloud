"""Точка входа."""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .context import build_context
from .core.config import AppConfig
from .core.logging_setup import setup_logging
from .core.paths import APP_TITLE
from .ui.main_window import MainWindow

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    log_file = setup_logging()
    log.info("Запуск %s", APP_TITLE)
    log.info("Журнал: %s", log_file)

    config = AppConfig.load()
    log.info(
        "Ключи OAuth: Яндекс — %s, Google — %s",
        "заданы" if config.yandex_client_id else "не заданы",
        "заданы" if config.google_client_id else "не заданы",
    )

    if "--reset-tokens" in argv:
        from .core.secrets_store import TokenStore

        store = TokenStore()
        for provider in ("yandex", "google"):
            store.clear(provider)
        print("Сохранённые входы удалены.")
        return 0

    # svsCloud.exe --open https://disk.yandex.ru/d/KEY
    start_url = ""
    if "--open" in argv:
        index = argv.index("--open")
        if index + 1 < len(argv):
            start_url = argv[index + 1]

    # QtWebEngine требует общий OpenGL-контекст — задаём до создания QApplication.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

    app = QApplication(argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("svsCloud")

    ctx = build_context(config)
    window = MainWindow(ctx)
    window.show()
    if start_url:
        window.open_link_at_startup(start_url)

    try:
        return app.exec()
    finally:
        try:
            ctx.cache.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
