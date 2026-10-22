"""Точка входа."""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .context import build_context
from .core.config import AppConfig
from .core.logging_setup import install_qt_message_handler, level_from_name, setup_logging
from .core.autostart import BACKGROUND_ARG
from .core.paths import APP_TITLE
from .ui.main_window import MainWindow

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    # Настройки читаем до журнала: уровень подробности логов задаётся в них.
    config = AppConfig.load()
    log_file = setup_logging(level=level_from_name(config.log_level))
    install_qt_message_handler()
    log.info("Запуск %s", APP_TITLE)
    log.info("Журнал: %s", log_file)

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

    # NoukoCloud.exe --open https://disk.yandex.ru/d/KEY
    start_url = ""
    if "--open" in argv:
        index = argv.index("--open")
        if index + 1 < len(argv):
            start_url = argv[index + 1]

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

    # NoukoCloud.exe --quit — закрыть запущенную копию (как «Выход» в меню
    # значка у часов). Нужен для обновления программы: файлы заняты, пока она работает.
    if "--quit" in argv:
        from PySide6.QtCore import QCoreApplication

        from .core.single_instance import send_to_running

        _core = QCoreApplication.instance() or QCoreApplication(argv)
        sent = send_to_running("quit", 2000)
        log.info("Команда выхода %s", "отправлена" if sent else "не нужна: программа не запущена")
        return 0

    app = QApplication(argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("NoukoCloud")

    # Программа уже запущена (возможно, в фоне) — показываем её окно.
    from .core.single_instance import InstanceServer, send_to_running

    if send_to_running(f"open {start_url}" if start_url else "show"):
        log.info("Программа уже запущена — открыто её окно")
        return 0
    instance = InstanceServer(app)
    # Окно спрятано в трей — это не повод выходить; выход — из меню значка.
    app.setQuitOnLastWindowClosed(False)

    ctx = build_context(config)
    window = MainWindow(ctx)
    instance.message.connect(window.handle_instance_message)
    background = BACKGROUND_ARG in argv and window.tray is not None
    if background:
        log.info("Запуск в фоне (значок у часов)")
    else:
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
