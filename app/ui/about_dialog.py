"""Диалог «О программе»: версия, дата сборки, ссылка на релиз, лицензии."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import QTextBrowser, QWidget

from .. import __version__
from . import theme
from .widgets import OverlayDialog, button

REPO_URL = "https://github.com/peeppohui/NouKoCloud"
RELEASE_URL = f"{REPO_URL}/releases/latest"

#: Сторонние компоненты, которые попадают в поставку.
LICENSES: tuple[tuple[str, str, str], ...] = (
    ("PySide6 / Qt 6", "LGPL v3", "https://www.qt.io/licensing/open-source-lgpl-obligations"),
    ("ffmpeg", "LGPL v2.1+", "https://ffmpeg.org/legal.html"),
    ("yt-dlp", "Unlicense", "https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE"),
    ("httpx", "BSD 3-Clause", "https://github.com/encode/httpx/blob/master/LICENSE"),
)


def build_date() -> str:
    """Дата сборки.

    Сборка (packaging/build.ps1) подставляет NOUKOCLOUD_BUILD_DATE; если её
    нет — берём время изменения файла с версией.
    """
    stamp = (os.environ.get("NOUKOCLOUD_BUILD_DATE") or "").strip()
    if stamp:
        return stamp
    try:
        source = Path(__file__).resolve().parent.parent / "__init__.py"
        mtime = source.stat().st_mtime
    except OSError:
        return "неизвестно"
    return datetime.fromtimestamp(mtime).strftime("%d.%m.%Y")


def about_html(version: str = __version__, built: str = "") -> str:
    """Текст окна «О программе» (HTML)."""
    palette = theme.current()
    rows = "".join(
        f'<tr><td style="padding:3px 12px 3px 0">{name}</td>'
        f'<td style="padding:3px 12px 3px 0;color:{palette.muted}">{license_name}</td>'
        f'<td style="padding:3px 0"><a href="{link}">{link}</a></td></tr>'
        for name, license_name, link in LICENSES
    )
    built_line = f"<tr><td style='padding:3px 12px 3px 0'>Сборка</td><td>{built}</td></tr>" if built else ""
    return (
        "<div style='font-size:15px'>"
        f"<b>НоуКо Cloud</b> — версия {version}"
        "<div style='color:%s;margin-top:6px'>"
        "Настольная программа для Яндекс.Диска и Google Диска: открывает облако по "
        "ссылке, показывает превью и качает файлы к себе. В облаке ничего не "
        "переименовывается и не удаляется — программа только читает."
        "</div>"
        "<table style='margin-top:14px'>"
        f"{built_line}"
        f"<tr><td style='padding:3px 12px 3px 0'>Сайт</td>"
        f"<td><a href='{REPO_URL}'>{REPO_URL}</a></td></tr>"
        f"<tr><td style='padding:3px 12px 3px 0'>Релиз</td>"
        f"<td><a href='{RELEASE_URL}'>{RELEASE_URL}</a></td></tr>"
        "</table>"
        "<div style='margin-top:16px'><b>Сторонние компоненты</b></div>"
        "<table style='margin-top:6px'>"
        f"{rows}"
        "</table>"
        "<div style='color:%s;margin-top:12px;font-size:13px'>"
        "Программа не собирает телеметрию: наружу уходят только запросы к облаку "
        "и проверка обновлений, её можно отключить в «Настройках»."
        "</div>"
        "</div>"
    ) % (palette.muted, palette.muted)


class AboutDialog(OverlayDialog):
    """Окно «О программе»."""

    def __init__(self, parent: QWidget | None = None, *,
                 on_check_updates: Callable[[], None] | None = None,
                 on_diagnostics: Callable[[], None] | None = None) -> None:
        super().__init__("о программе", parent, max_width=760)
        self.setWindowTitle("О программе")
        self.view = QTextBrowser(self.body)
        self.view.setOpenExternalLinks(True)
        self.view.setMinimumHeight(380)
        palette = theme.current()
        self.view.document().setDefaultStyleSheet(
            f"a {{ color: {palette.text}; }} "
            f"code {{ background: {palette.raised}; }} "
            f"table {{ border-collapse: collapse; }} "
            f"td, th {{ border: 1px solid {palette.line}; padding: 6px 8px; }}"
        )
        self.view.setStyleSheet(
            f"QTextBrowser {{ background: transparent; border: none; font-size: 14px; color: {palette.text}; }}")
        self.view.setHtml(about_html(__version__, build_date()))
        self.body_layout.addWidget(self.view, 1)

        if on_check_updates is not None:
            update = button("Проверить обновления", "outline", self.card,
                            icon_name="cloud", size="m")
            update.setToolTip("Проверить, не вышла ли новая версия")
            update.clicked.connect(on_check_updates)
            self.footer.addWidget(update)
        if on_diagnostics is not None:
            diag = button("Собрать диагностику", "outline", self.card,
                          icon_name="log", size="m")
            diag.setToolTip("Архив с журналами и настройками для отправки разработчику")
            diag.clicked.connect(on_diagnostics)
            self.footer.addWidget(diag)
        close = button("Закрыть", "primary", self.card, size="m")
        close.clicked.connect(self.accept)
        self.footer.addWidget(close)
