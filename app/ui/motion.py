"""Движение в интерфейсе: плавная подсветка кнопок.

* Кнопки: при наведении подсветка плавно проявляется (160 мс), при нажатии —
  короткая вспышка. Таблица стилей Qt умеет только мгновенный :hover, поэтому
  подсветку рисует прозрачный слой поверх кнопки (ButtonAnimator).
* Фон неподвижный (по просьбе: без анимации). Motion.tick и stars_offset
  остались на случай будущих анимаций; сейчас на них никто не подписан.

Всё выключается в «Настройках» (config.ui_motion).
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QAbstractButton, QApplication, QCheckBox, QRadioButton, QWidget

from . import theme

HOVER_MS = 160
PRESS_MS = 70
RELEASE_MS = 220
FPS = 15


class Motion(QObject):
    """Включено ли движение + общий «метроном» для фона."""

    tick = Signal(float)          # секунды с начала
    enabled_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.enabled = True
        self._start = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / FPS))
        self._timer.timeout.connect(self._emit)
        self._users = 0
        app = QGuiApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._sync)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self._sync()
        self.enabled_changed.emit(enabled)

    def subscribe(self) -> None:
        self._users += 1
        self._sync()

    def now(self) -> float:
        return time.monotonic() - self._start

    def _sync(self, *_args) -> None:
        app = QGuiApplication.instance()
        active = app is None or app.applicationState() == Qt.ApplicationState.ApplicationActive
        run = self.enabled and self._users > 0 and active
        if run and not self._timer.isActive():
            self._timer.start()
        elif not run and self._timer.isActive():
            self._timer.stop()

    def _emit(self) -> None:
        self.tick.emit(self.now())


_motion: Motion | None = None


def motion() -> Motion:
    global _motion
    if _motion is None:
        _motion = Motion()
    return _motion


def stars_offset(t: float) -> tuple[float, float, float]:
    """Сдвиг (x, y) и множитель яркости сетки звёзд в момент t.

    Медленные несовпадающие периоды — движение не выглядит «зацикленным».
    """
    dx = 7.0 * math.sin(2 * math.pi * t / 29.0)
    dy = 11.0 * math.sin(2 * math.pi * t / 23.0 + 1.3)
    glow = 0.86 + 0.14 * math.sin(2 * math.pi * t / 9.0)
    return dx, dy, glow


# ----------------------------------------------------------------------
# Подсветка кнопок
# ----------------------------------------------------------------------
class _Glow(QWidget):
    """Прозрачный слой поверх кнопки: рисует подсветку с плавной прозрачностью."""

    def __init__(self, button: QAbstractButton) -> None:
        super().__init__(button)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.hover = 0.0
        self.press = 0.0
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.valueChanged.connect(self._set_hover)
        self._press_anim = QVariantAnimation(self)
        self._press_anim.valueChanged.connect(self._set_press)
        self.setGeometry(button.rect())
        self.show()

    def _set_hover(self, value) -> None:
        self.hover = float(value)
        self.update()

    def _set_press(self, value) -> None:
        self.press = float(value)
        self.update()

    def animate(self, attr: str, target: float, ms: int, curve=QEasingCurve.Type.OutCubic) -> None:
        anim = self._hover_anim if attr == "hover" else self._press_anim
        anim.stop()
        anim.setStartValue(getattr(self, attr))
        anim.setEndValue(target)
        anim.setDuration(ms)
        anim.setEasingCurve(curve)
        anim.start()

    def paintEvent(self, _event) -> None:  # noqa: N802
        level = min(1.0, self.hover * 0.6 + self.press * 0.9)
        if level <= 0.001:
            return
        button = self.parentWidget()
        palette = theme.current()
        kind = button.property("kind") if button is not None else None
        if kind in ("primary", "play"):
            # Главная кнопка светлая на тёмной теме и тёмная на светлой —
            # подсветка противоположного тона, иначе её не видно.
            color = theme.qcolor(palette.pri_text)
            alpha = 0.16 * level
        else:
            color = theme.qcolor(palette.text)
            alpha = 0.10 * level
        color.setAlphaF(alpha)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 8.0 if kind == "row" else min(rect.height(), rect.width()) / 2
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillPath(path, color)
        painter.end()


class ButtonAnimator(QObject):
    """Фильтр событий приложения: подсветка всех кнопок (кроме флажков)."""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if not isinstance(obj, QAbstractButton) or isinstance(obj, (QCheckBox, QRadioButton)):
            return False
        etype = event.type()
        if etype not in (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.MouseButtonPress,
                         QEvent.Type.MouseButtonRelease, QEvent.Type.Resize):
            return False
        if etype == QEvent.Type.MouseButtonRelease and obj.isEnabled() \
                and event.button() == Qt.MouseButton.LeftButton and obj.rect().contains(event.position().toPoint()):
            from .sound import play

            play("click")
        glow = obj.findChild(_Glow, options=Qt.FindChildOption.FindDirectChildrenOnly)
        if etype == QEvent.Type.Resize:
            if glow is not None:
                glow.setGeometry(obj.rect())
            return False
        if not motion().enabled or not obj.isEnabled():
            if glow is not None:
                glow.hover = glow.press = 0.0
                glow.update()
            return False
        if glow is None:
            glow = _Glow(obj)
        glow.setGeometry(obj.rect())
        glow.raise_()
        if etype == QEvent.Type.Enter:
            glow.animate("hover", 1.0, HOVER_MS)
        elif etype == QEvent.Type.Leave:
            glow.animate("hover", 0.0, HOVER_MS + 80)
            glow.animate("press", 0.0, RELEASE_MS)
        elif etype == QEvent.Type.MouseButtonPress:
            glow.animate("press", 1.0, PRESS_MS, QEasingCurve.Type.OutQuad)
        elif etype == QEvent.Type.MouseButtonRelease:
            glow.animate("press", 0.0, RELEASE_MS)
        return False


_animator: ButtonAnimator | None = None


def install(app: QApplication) -> None:
    """Подключает подсветку кнопок ко всему приложению (один раз)."""
    global _animator
    if _animator is None:
        _animator = ButtonAnimator(app)
        app.installEventFilter(_animator)
