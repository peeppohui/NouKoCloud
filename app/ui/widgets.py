"""Общие элементы интерфейса «НоуКо Cloud»: шапка окна, фон со звёздами,
окна поверх затемнения, кнопки-«пилюли», переключатель вкладок."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .theme import Palette


# ----------------------------------------------------------------------
# Мелочи
# ----------------------------------------------------------------------
def set_prop(widget: QWidget, name: str, value) -> None:
    """Меняет свойство для стилей и сразу перерисовывает виджет."""
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def button(text: str, kind: str = "outline", parent: QWidget | None = None,
           icon_name: str | None = None, size: str | None = None) -> QPushButton:
    """Кнопка-«пилюля» из макета: primary | outline | soft | nav | ghost | link."""
    btn = QPushButton(text, parent)
    btn.setProperty("kind", kind)
    if size:
        btn.setProperty("variant", size)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon_name:
        IconBinding.attach(btn, icon_name, "pri_text" if kind == "primary" else "text")
    return btn


def icon_button(icon_name: str, tooltip: str, parent: QWidget | None = None,
                kind: str = "icon", size: int = 44, icon_size: int = 18,
                color_role: str = "text") -> QToolButton:
    btn = QToolButton(parent)
    btn.setProperty("kind", kind)
    btn.setProperty("dim", str(size))
    btn.setToolTip(tooltip)
    btn.setAccessibleName(tooltip)
    btn.setFixedSize(size, size)
    btn.setIconSize(QSize(icon_size, icon_size))
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    IconBinding.attach(btn, icon_name, color_role, icon_size)
    return btn


def label(text: str = "", role: str | None = None, parent: QWidget | None = None,
          wrap: bool = False) -> QLabel:
    lbl = QLabel(text, parent)
    if role:
        lbl.setProperty("role", role)
    lbl.setWordWrap(wrap)
    return lbl


class Breakpoint(QObject):
    """Следит за размером виджета и сообщает, когда он стал уже/шире порога.

    width — порог ширины, или test(ширина, высота) -> bool («узко»).
    on_change(narrow) вызывается сразу и затем при каждом переходе через порог.
    """

    def __init__(self, watched: QWidget, width, on_change) -> None:
        super().__init__(watched)
        self._test = width if callable(width) else (lambda w, _h, limit=width: w < limit)
        self._on_change = on_change
        self.narrow: bool | None = None
        watched.installEventFilter(self)
        self._apply(watched.width(), watched.height())

    def _apply(self, width: int, height: int) -> None:
        narrow = bool(self._test(width, height))
        if narrow != self.narrow:
            self.narrow = narrow
            self._on_change(narrow)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Resize:
            self._apply(obj.width(), obj.height())
        return False


def responsive_box(layout: QBoxLayout, watched: QWidget, width: int,
                   on_change=None) -> Breakpoint:
    """Ряд, который в узком месте становится столбцом."""
    def change(narrow: bool) -> None:
        layout.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
        if on_change is not None:
            on_change(narrow)

    return Breakpoint(watched, width, change)


def hline(parent: QWidget | None = None) -> QFrame:
    line = QFrame(parent)
    line.setProperty("hline", True)
    line.setFixedHeight(1)
    return line


def vline(parent: QWidget | None = None, soft: bool = False, height: int = 20) -> QFrame:
    line = QFrame(parent)
    line.setObjectName("vlineSoft" if soft else "vline")
    line.setFixedSize(1, height)
    return line


def glass(parent: QWidget | None = None) -> QFrame:
    frame = QFrame(parent)
    frame.setProperty("glass", True)
    return frame


class IconBinding:
    """Иконка кнопки, которая перекрашивается при смене темы."""

    @staticmethod
    def attach(btn, icon_name: str, color_role: str = "text", size: int = 18) -> None:
        btn._icon_spec = (icon_name, color_role, size)

        def apply(palette: Palette) -> None:
            try:
                name, role, sz = btn._icon_spec
                btn.setIcon(theme.icon(name, getattr(palette, role), sz))
            except RuntimeError:
                # Кнопку уже удалили — отписываемся от смены темы.
                try:
                    theme.manager().changed.disconnect(apply)
                except (RuntimeError, TypeError):
                    pass

        btn._icon_apply = apply
        apply(theme.current())
        theme.manager().changed.connect(apply)

    @staticmethod
    def change(btn, icon_name: str, color_role: str | None = None) -> None:
        _, role, size = btn._icon_spec
        btn._icon_spec = (icon_name, color_role or role, size)
        btn._icon_apply(theme.current())


class IconLabel(QLabel):
    """Картинка-иконка, перекрашиваемая при смене темы."""

    def __init__(self, icon_name: str, color_role: str = "text", size: int = 18,
                 parent: QWidget | None = None, stroke: float = 1.8) -> None:
        super().__init__(parent)
        self._spec = (icon_name, color_role, size, stroke)
        self.setFixedSize(size, size)
        theme.manager().changed.connect(self._apply)
        self._apply(theme.current())

    def set_icon(self, icon_name: str, color_role: str | None = None) -> None:
        _, role, size, stroke = self._spec
        self._spec = (icon_name, color_role or role, size, stroke)
        self._apply(theme.current())

    def set_color(self, color: str) -> None:
        """Цвет напрямую (например, статус задания)."""
        name, _, size, stroke = self._spec
        self.setPixmap(theme.pixmap(name, color, size, stroke))

    def _apply(self, palette: Palette) -> None:
        name, role, size, stroke = self._spec
        color = getattr(palette, role, role)
        self.setPixmap(theme.pixmap(name, color, size, stroke))


class Logo(QLabel):
    """Логотип «НоуКо»: светлый на тёмной теме, тёмный — на светлой."""

    clicked = Signal()

    def __init__(self, height: int, parent: QWidget | None = None, clickable: bool = False) -> None:
        super().__init__(parent)
        self._h = height
        self.setAccessibleName("НоуКо")
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("На главный экран")
        self._clickable = clickable
        theme.manager().changed.connect(self._apply)
        self._apply(theme.current())

    def _apply(self, palette: Palette) -> None:
        self.setPixmap(theme.logo_pixmap(self._h, palette))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


# ----------------------------------------------------------------------
# Фон: цвет темы и сетка звёзд справа
# ----------------------------------------------------------------------
class StarsBackground(QWidget):
    """Фон окна: цвет темы и неподвижная сетка звёзд справа (как в макете)."""

    def __init__(self, parent: QWidget | None = None, opacity_scale: float = 1.0) -> None:
        super().__init__(parent)
        self._scale = opacity_scale
        self._scaled = None            # (ключ, QPixmap) — масштабируем один раз
        theme.manager().changed.connect(self._on_theme)

    def _on_theme(self, _value) -> None:
        self.update()

    def _stars(self, palette, height: int):
        key = (palette.stars, height)
        if self._scaled is None or self._scaled[0] != key:
            src = theme.asset_pixmap(palette.stars)
            if src.isNull():
                return None
            ratio = self.devicePixelRatioF() or 1.0
            pix = src.scaledToHeight(int(height * ratio), Qt.TransformationMode.SmoothTransformation)
            pix.setDevicePixelRatio(ratio)
            self._scaled = (key, pix)
        return self._scaled[1]

    def paintEvent(self, _event) -> None:  # noqa: N802
        palette = theme.current()
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.qcolor(palette.bg))
        if self.height() > 0:
            stars = self._stars(palette, self.height())
            if stars is not None:
                w = stars.width() / (stars.devicePixelRatio() or 1.0)
                painter.setOpacity(palette.stars_opacity * self._scale)
                painter.drawPixmap(int(self.width() - w), 0, stars)
        painter.end()


# ----------------------------------------------------------------------
# Шапка окна
# ----------------------------------------------------------------------
class TitleBar(QWidget):
    open_link = Signal(str)
    my_drive = Signal()
    social = Signal()
    accounts = Signal()
    settings = Signal()
    toggle_theme = Signal()
    home = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(64)
        row = QHBoxLayout(self)
        row.setContentsMargins(20, 10, 8, 10)
        row.setSpacing(12)

        brand = QWidget(self)
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(0, 0, 8, 0)
        brand_row.setSpacing(12)
        self.logo = Logo(20, brand, clickable=True)
        self.logo.clicked.connect(self.home)
        brand_row.addWidget(self.logo)
        self._brand_sep = vline(brand)
        brand_row.addWidget(self._brand_sep)
        self._word = QLabel("CLOUD", brand)
        self._word.setObjectName("cloudWord")
        f = theme.font(theme.WIDE, 13, bold=True)
        f.setLetterSpacing(f.SpacingType.AbsoluteSpacing, 1.0)
        self._word.setFont(f)
        brand_row.addWidget(self._word)
        row.addWidget(brand)

        # Поле ссылки
        self._form = QFrame(self)
        self._form.setObjectName("linkForm")
        self._form.setFixedHeight(44)
        self._form.setMinimumWidth(300)
        self._form.setMaximumWidth(620)
        self._form.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        form_row = QHBoxLayout(self._form)
        form_row.setContentsMargins(16, 0, 4, 0)
        form_row.setSpacing(8)
        form_row.addWidget(IconLabel("link", "text", 18, self._form))
        self.link_edit = QLineEdit(self._form)
        self.link_edit.setPlaceholderText("Вставьте ссылку на папку Яндекс.Диска или Google Диска")
        self.link_edit.setAccessibleName("Ссылка на папку")
        self.link_edit.returnPressed.connect(self._emit_link)
        form_row.addWidget(self.link_edit, 1)
        self._btn_open = button("Открыть", "primary", self._form, size="s")
        self._btn_open.clicked.connect(self._emit_link)
        form_row.addWidget(self._btn_open)
        row.addWidget(self._form, 1)

        self._spacer = QWidget(self)
        self._spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._spacer.setVisible(False)
        row.addWidget(self._spacer, 1)

        self._nav: dict[str, QPushButton] = {}
        for key, text, icon_name, signal in (
            ("mydrive", "Мой диск", "cloud", self.my_drive),
            ("social", "Соцсети", "social", self.social),
            ("accounts", "Аккаунты", "user", self.accounts),
            ("settings", "Настройки", "gear", self.settings),
        ):
            btn = button(text, "nav", self, icon_name=icon_name)
            btn.setToolTip(text)
            btn._full_text = text
            btn.clicked.connect(signal)
            self._nav[key] = btn
            row.addWidget(btn)

        self._sep1 = vline(self, soft=True, height=24)
        row.addWidget(self._sep1)
        self._btn_theme = icon_button("sun", "Светлая тема", self, kind="iconFramed")
        self._btn_theme.clicked.connect(self.toggle_theme)
        row.addWidget(self._btn_theme)
        self._sep2 = vline(self, soft=True, height=24)
        row.addWidget(self._sep2)

        self._btn_min = icon_button("minimize", "Свернуть", self, icon_size=16)
        self._btn_max = icon_button("maximize", "Развернуть", self, icon_size=14)
        self._btn_close = icon_button("close", "Закрыть программу", self, icon_size=16)
        self._btn_close.setObjectName("closeBtn")
        self._btn_min.clicked.connect(lambda: self.window().showMinimized())
        self._btn_max.clicked.connect(self._toggle_max)
        self._btn_close.clicked.connect(lambda: self.window().close())
        for btn in (self._btn_min, self._btn_max, self._btn_close):
            row.addWidget(btn)

        self._drag_widgets = {self, brand, self.logo, self._word, self._spacer}
        self._level = -1
        theme.manager().changed.connect(self._on_theme)
        self._on_theme(theme.current())

    # ------------------------------------------------------------------
    def _emit_link(self) -> None:
        text = self.link_edit.text().strip()
        if text:
            self.open_link.emit(text)

    def set_link(self, text: str) -> None:
        self.link_edit.setText(text)
        self.link_edit.setCursorPosition(0)

    def set_field_visible(self, visible: bool) -> None:
        self._form.setVisible(visible)
        self._spacer.setVisible(not visible)
        self._fit()

    # Уровни компактности: 0 — всё полностью; 1 — без слова «CLOUD»;
    # 2 — разделы только значками; 3 — поле ссылки короче; 4 — без разделителей.
    def _set_level(self, level: int) -> None:
        if level == self._level:
            return
        self._level = level
        self._word.setVisible(level < 1)
        for btn in self._nav.values():
            btn.setText("" if level >= 2 else btn._full_text)
        # Совсем тесно: плотнее ряд, поле без кнопки «Открыть» (работает Enter).
        self.layout().setSpacing(12 if level < 3 else 4)
        self._btn_open.setVisible(level < 3)
        self._form.setMinimumWidth(300 if level < 3 else 160 if level < 4 else 110)
        # Уровень 4 — минимальное окно: без разделителей и отступов логотипа.
        for sep in (self._sep1, self._sep2, self._brand_sep):
            sep.setVisible(level < 4)
        self.layout().setContentsMargins(20 if level < 4 else 12, 10, 8, 10)
        self.link_edit.setPlaceholderText("Вставьте ссылку на папку Яндекс.Диска или Google Диска"
                                          if level < 3 else "Ссылка на папку — Enter")
        self.layout().invalidate()

    def _fit(self) -> None:
        for level in range(5):
            self._set_level(level)
            if self.layout().minimumSize().width() <= self.width():
                return

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def set_active(self, key: str | None) -> None:
        self._active = key
        for name, btn in self._nav.items():
            set_prop(btn, "active", name == key)

    def set_close_tooltip(self, text: str) -> None:
        self._btn_close.setToolTip(text)
        self._btn_close.setAccessibleName(text)

    def active(self) -> str | None:
        return getattr(self, "_active", None)

    def set_window_buttons_visible(self, visible: bool) -> None:
        for btn in (self._btn_min, self._btn_max, self._btn_close):
            btn.setVisible(visible)

    def sync_max_button(self, maximized: bool) -> None:
        IconBinding.change(self._btn_max, "restore" if maximized else "maximize")
        self._btn_max.setToolTip("Свернуть в окно" if maximized else "Развернуть")

    def _on_theme(self, palette: Palette) -> None:
        dark = palette.name == "dark"
        IconBinding.change(self._btn_theme, "sun" if dark else "moon")
        self._btn_theme.setToolTip("Светлая тема" if dark else "Тёмная тема")

    def _toggle_max(self) -> None:
        win = self.window()
        if win.isMaximized():
            win.showNormal()
        else:
            win.showMaximized()

    # --- перетаскивание -------------------------------------------------
    def is_drag_area(self, global_pos: QPoint) -> bool:
        local = self.mapFromGlobal(global_pos)
        if not self.rect().contains(local):
            return False
        child = self.childAt(local)
        return child is None or child in self._drag_widgets or (
            isinstance(child, QLabel) and child is not self.logo and child.parent() in self._drag_widgets
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # Запасной путь (не Windows): Windows сама двигает окно по HTCAPTION.
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_max()
        super().mouseDoubleClickEvent(event)


# ----------------------------------------------------------------------
# Переключатель «Превью / Очередь»
# ----------------------------------------------------------------------
class SegmentedTabs(QFrame):
    changed = Signal(int)

    def __init__(self, titles: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("segTabs")
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(4)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.buttons: list[QPushButton] = []
        for index, title in enumerate(titles):
            btn = QPushButton(title, self)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._group.addButton(btn, index)
            self.buttons.append(btn)
            row.addWidget(btn)
        self.buttons[0].setChecked(True)
        self._group.idClicked.connect(self.changed)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_current(self, index: int) -> None:
        self.buttons[index].setChecked(True)

    def add_badge(self, index: int) -> QLabel:
        """Счётчик в кнопке (например, число заданий в очереди)."""
        btn = self.buttons[index]
        title = btn.text()
        btn.setText("")
        btn.setAccessibleName(title)
        lay = QHBoxLayout(btn)
        lay.setContentsMargins(20, 0, 12, 0)
        lay.setSpacing(8)
        text = QLabel(title, btn)
        text.setProperty("role", "wide")
        text.setStyleSheet("font-size: 13px;")
        badge = QLabel("0", btn)
        badge.setObjectName("badge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for child in (text, badge):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lay.addWidget(child)
        btn._title_label = text
        btn._badge = badge
        self._fit(btn)
        return badge

    @staticmethod
    def _fit(btn: QPushButton) -> None:
        """Ширина кнопки по надписи и счётчику (у кнопки с layout нет своей)."""
        btn.setMinimumWidth(btn.layout().sizeHint().width())

    def refresh_badge(self, index: int) -> None:
        btn = self.buttons[index]
        if btn.layout() is not None:
            self._fit(btn)


# ----------------------------------------------------------------------
# Окно поверх затемнения (Настройки, Аккаунты)
# ----------------------------------------------------------------------
class OverlayDialog(QDialog):
    """Модальное окно в стиле макета: карточка по центру, под ней — затемнение.

    С родителем окно накрывает всё главное окно (затемнение), без родителя
    (тесты) — это просто карточка.
    """

    def __init__(self, title_html: str, parent: QWidget | None = None, max_width: int = 1000) -> None:
        super().__init__(parent)
        self.setObjectName("overlay")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 24, 16, 24)
        outer.addStretch(1)
        center = QHBoxLayout()
        center.addStretch(1)
        self.card = QFrame(self)
        self.card.setObjectName("overlayCard")
        self.card.setMaximumWidth(max_width)
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        center.addWidget(self.card, 100)
        center.addStretch(1)
        outer.addLayout(center)
        outer.addStretch(1)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(28, 24, 28, 24)
        card_layout.setSpacing(18)
        head = QHBoxLayout()
        head.setSpacing(16)
        self.title = QLabel(title_html, self.card)
        self.title.setProperty("role", "h2")
        self.title.setTextFormat(Qt.TextFormat.RichText)
        head.addWidget(self.title, 1, Qt.AlignmentFlag.AlignTop)
        close = icon_button("close", "Закрыть", self.card, size=40)
        close.clicked.connect(self.reject)
        head.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        card_layout.addLayout(head)

        # Содержимое прокручивается, если окно программы ниже карточки.
        self._scroll = QScrollArea(self.card)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(16)
        self._scroll.setWidget(self.body)
        card_layout.addWidget(self._scroll, 1)

        self.footer = QHBoxLayout()
        self.footer.setSpacing(8)
        self.footer.addStretch(1)
        card_layout.addLayout(self.footer)

    def showEvent(self, event) -> None:  # noqa: N802
        host = self.parentWidget().window() if self.parentWidget() is not None else None
        if host is not None:
            self.setGeometry(host.geometry())
        # Высота прокрутки — по содержимому, но не выше окна.
        self.body.adjustSize()
        want = self.body.sizeHint().height() + 4
        limit = (self.height() if host is not None else 900) - 220
        self._scroll.setMinimumHeight(min(want, max(200, limit)))
        super().showEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self.parentWidget() is None:
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.qcolor(theme.current().scrim))
        painter.end()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Enter в поле ввода не должен закрывать окно (QDialog так делает по умолчанию).
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            # deleteLater срабатывает только в цикле событий — до этого старая
            # строка ещё рисуется поверх новых, поэтому сначала прячем её.
            widget.hide()
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def app_instance() -> QApplication | None:
    return QApplication.instance()
