"""Окно с инструкцией «Как подключить Яндекс.Диск и Google Диск»."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QTextBrowser, QWidget

from ..core.paths import is_frozen
from . import theme
from .widgets import OverlayDialog, button

GUIDE_NAME = "ПОДКЛЮЧЕНИЕ_АККАУНТОВ.md"


def docs_dir() -> Path:
    """Папка docs: в собранной программе — внутри _internal, иначе — в проекте."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "docs"
    return Path(__file__).resolve().parent.parent.parent / "docs"


def guide_text() -> str:
    path = docs_dir() / GUIDE_NAME
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ("Инструкция не найдена. Она есть в репозитории программы: "
                "docs/ПОДКЛЮЧЕНИЕ_АККАУНТОВ.md")


class HelpDialog(OverlayDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("как подключить аккаунты", parent, max_width=860)
        self.setWindowTitle("Как подключить аккаунты")
        self.view = QTextBrowser(self.body)
        self.view.setOpenExternalLinks(True)
        self.view.setMinimumHeight(520)
        palette = theme.current()
        self.view.document().setDefaultStyleSheet(
            f"a {{ color: {palette.text}; }} "
            f"code {{ background: {palette.raised}; }} "
            f"table {{ border-collapse: collapse; }} "
            f"td, th {{ border: 1px solid {palette.line}; padding: 6px 8px; }}"
        )
        self.view.setStyleSheet(
            f"QTextBrowser {{ background: transparent; border: none; font-size: 14px; color: {palette.text}; }}")
        self.view.setMarkdown(guide_text())
        self.body_layout.addWidget(self.view, 1)
        close = button("Понятно", "primary", self.card, size="m")
        close.clicked.connect(self.accept)
        self.footer.addWidget(close)
