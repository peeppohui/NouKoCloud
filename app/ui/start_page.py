"""Стартовый экран: поле для ссылки, «Мой диск», «Аккаунты», недавние ссылки."""

from __future__ import annotations

import time
from datetime import date, datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .widgets import Breakpoint, IconLabel, Logo, StarsBackground, button, clear_layout, icon_button, label


def when_text(ts: float, today: date | None = None) -> str:
    """«сегодня», «вчера» или дата «12.10»."""
    try:
        day = datetime.fromtimestamp(float(ts)).date()
    except (TypeError, ValueError, OSError):
        return ""
    today = today or date.today()
    delta = (today - day).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "вчера"
    if day.year == today.year:
        return day.strftime("%d.%m")
    return day.strftime("%d.%m.%Y")


def short_url(url: str) -> str:
    for prefix in ("https://", "http://", "www."):
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url


class StartPage(StarsBackground):
    open_link = Signal(str)
    my_drive = Signal()
    social = Signal()
    accounts = Signal()
    forget_link = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, opacity_scale=1.3)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(96, 48, 48, 48)

        column = QWidget(self)
        column.setMaximumWidth(600)
        column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay = QVBoxLayout(column)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(28)
        outer.addWidget(column, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addStretch(1)

        hero = QVBoxLayout()
        hero.setSpacing(18)
        self._logo = Logo(56, column)
        hero.addWidget(self._logo, 0, Qt.AlignmentFlag.AlignLeft)
        title = label("облачные исходники\nдля монтажа", "h1", column)
        hero.addWidget(title)
        hero.addWidget(label(
            "Откройте папку на Яндекс.Диске или Google Диске, посмотрите превью "
            "и заберите только нужные файлы — с докачкой.", "lead", column, wrap=True))
        lay.addLayout(hero)

        form = QFrame(column)
        form.setProperty("recent", True)
        form.setStyleSheet("QFrame { border-radius: 32px; }")
        form_row = QHBoxLayout(form)
        form_row.setContentsMargins(20, 8, 8, 8)
        form_row.setSpacing(10)
        form_row.addWidget(IconLabel("link", "text", 20, form))
        self.link_edit = QLineEdit(form)
        self.link_edit.setPlaceholderText("Вставьте ссылку на папку Яндекс.Диска или Google Диска")
        self.link_edit.setAccessibleName("Ссылка на папку")
        self.link_edit.setStyleSheet("QLineEdit { background: transparent; border: none; font-size: 15px;"
                                     " min-height: 48px; padding: 0; }")
        self.link_edit.returnPressed.connect(self._emit_link)
        form_row.addWidget(self.link_edit, 1)
        open_btn = button("Открыть ссылку", "primary", form, size="xl")
        open_btn.clicked.connect(self._emit_link)
        form_row.addWidget(open_btn)
        lay.addWidget(form)

        row = QHBoxLayout()
        row.setSpacing(12)
        drive = button("Мой диск", "soft", column, icon_name="cloud", size="l")
        drive.clicked.connect(self.my_drive)
        social_btn = button("Соцсети", "soft", column, icon_name="social", size="l")
        social_btn.setToolTip("Скачать видео или звук по ссылке из YouTube, VK, TikTok и других")
        social_btn.clicked.connect(self.social)
        row.addWidget(social_btn)
        acc = button("Аккаунты", "soft", column, icon_name="user", size="l")
        acc.clicked.connect(self.accounts)
        row.addWidget(drive)
        row.addWidget(acc)
        row.addStretch(1)
        lay.addLayout(row)
        note = QHBoxLayout()
        note.setSpacing(8)
        note.addWidget(IconLabel("check", "muted", 14, column, stroke=2.2))
        note.addWidget(label("Публичные ссылки работают без входа", "muted", column, wrap=True), 1)
        lay.addLayout(note)

        self._recent_box = QWidget(column)
        recent_lay = QVBoxLayout(self._recent_box)
        recent_lay.setContentsMargins(0, 0, 0, 0)
        recent_lay.setSpacing(10)
        recent_lay.addWidget(label("НЕДАВНИЕ ССЫЛКИ", "caps", self._recent_box))
        self._recent_list = QVBoxLayout()
        self._recent_list.setSpacing(6)
        recent_lay.addLayout(self._recent_list)
        lay.addWidget(self._recent_box)
        self.set_recent([])
        # В узком окне — меньше пустого места слева.
        self._bp = Breakpoint(self, 1000, lambda narrow: outer.setContentsMargins(
            *((32, 16, 32, 16) if narrow else (96, 48, 48, 48))))
        # В низком окне большой логотип и недавние ссылки уступают место главному.
        def low(is_low: bool) -> None:
            self._logo.setVisible(not is_low)
            lay.setSpacing(16 if is_low else 28)
            self._low = is_low
            self._sync_recent_visible()

        self._low = False
        self._bp_low = Breakpoint(self, lambda _w, h: h < 600, low)

    # ------------------------------------------------------------------
    def _emit_link(self) -> None:
        text = self.link_edit.text().strip()
        if text:
            self.open_link.emit(text)

    def set_recent(self, items: list[dict]) -> None:
        clear_layout(self._recent_list)
        shown = 0
        for item in items[:6]:
            url = str(item.get("url") or "")
            if not url:
                continue
            self._recent_list.addWidget(self._recent_row(
                str(item.get("title") or short_url(url)), url, when_text(item.get("ts", time.time()))))
            shown += 1
        self._has_recent = shown > 0
        self._sync_recent_visible()

    def _sync_recent_visible(self) -> None:
        self._recent_box.setVisible(getattr(self, "_has_recent", False) and not getattr(self, "_low", False))

    def _recent_row(self, title: str, url: str, when: str) -> QFrame:
        row = QFrame(self._recent_box)
        row.setProperty("recent", True)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(16, 6, 6, 6)
        lay.setSpacing(12)
        lay.addWidget(IconLabel("folder", "text", 18, row))

        # Вся строка с названием — одна кнопка (Tab, Enter, клик).
        open_btn = QPushButton(row)
        open_btn.setProperty("kind", "row")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.setToolTip(url)
        open_btn.setAccessibleName(f"Открыть {title}")
        inner = QHBoxLayout(open_btn)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(12)
        name = label(title, "wide", open_btn)
        name.setStyleSheet("font-size: 13px;")
        inner.addWidget(name)
        address = label(short_url(url), "hint", open_btn)
        address.setMinimumWidth(40)
        address.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        inner.addWidget(address, 1)
        for child in (name, address):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        open_btn.clicked.connect(lambda _=False, u=url: self.open_link.emit(u))
        lay.addWidget(open_btn, 1)

        lay.addWidget(label(when, "hint", row))
        forget = icon_button("close", "Убрать ссылку из списка", row, size=40, icon_size=16)
        forget.clicked.connect(lambda _=False, u=url: self.forget_link.emit(u))
        lay.addWidget(forget)
        return row
