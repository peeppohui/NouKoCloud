"""Окно без рамки Windows, которое ведёт себя как обычное.

Шапка у программы своя (логотип, поле ссылки, разделы, кнопки окна), а
стандартная рамка Windows убрана. Чтобы окно при этом не потеряло привычное
поведение — перетаскивание с прилипанием к краям экрана (Aero Snap),
Win+стрелки, двойной клик по шапке, растягивание за края, анимации
сворачивания и тень, — Windows оставляем стили настоящего окна
(WS_CAPTION | WS_THICKFRAME), но отдаём всю его площадь под наш интерфейс:

* WM_NCCALCSIZE — «рамка нулевой толщины», вся площадь — клиентская;
* WM_NCHITTEST  — говорим системе, где края (растягивание) и где шапка
  (перетаскивание); кнопки и поле в шапке остаются обычными элементами.

На других платформах и в тестах (offscreen) — обычное окно без рамки
с перетаскиванием через startSystemMove().
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication

log = logging.getLogger(__name__)

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
HTCLIENT, HTCAPTION = 1, 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 10, 11, 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
SM_CXSIZEFRAME, SM_CYSIZEFRAME, SM_CXPADDEDBORDER = 32, 33, 92

RESIZE_BORDER = 6   # px (логические) — ширина зоны растягивания у края


class _MARGINS(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int),
                ("top", ctypes.c_int), ("bottom", ctypes.c_int)]


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _work_area(hwnd):
    """Рабочая область монитора, на котором окно (без панели задач)."""
    user32 = ctypes.windll.user32
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    monitor = user32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    return info.rcWork


def native_frameless_supported() -> bool:
    return sys.platform == "win32" and QGuiApplication.platformName() == "windows"


class FramelessWindow:
    """Примесь для QMainWindow/QWidget верхнего уровня.

    Наследник задаёт `self._title_bar` с методом is_drag_area(global_pos).
    """

    _title_bar = None
    _native_ready = False

    def init_frameless(self) -> None:
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowMinMaxButtonsHint)

    def _apply_native_styles(self) -> None:
        if self._native_ready or not native_frameless_supported():
            return
        self._native_ready = True
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            style = get_long(hwnd, GWL_STYLE)
            set_long(hwnd, GWL_STYLE,
                     style | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU)
            # Тень окна, как у обычных окон Windows.
            margins = _MARGINS(0, 0, 0, 1)
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
            # Пересчитать рамку с новыми стилями.
            SWP_FRAMECHANGED, SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER = 0x20, 0x2, 0x1, 0x4
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)
        except Exception:
            log.warning("Не удалось настроить окно без рамки", exc_info=True)

    # ------------------------------------------------------------------
    def frameless_native_event(self, event_type, message):
        """Вызывать из nativeEvent. Возвращает (обработано, результат) или None."""
        if not self._native_ready or event_type != b"windows_generic_MSG":
            return None
        try:
            msg = wintypes.MSG.from_address(int(message))
        except Exception:
            return None
        if msg.message == WM_NCCALCSIZE and msg.wParam:
            # Qt узнаёт о развёртывании позже, чем приходит это сообщение, —
            # спрашиваем саму Windows.
            if ctypes.windll.user32.IsZoomed(msg.hWnd):
                # Развёрнутое окно Windows выдвигает за край экрана на толщину
                # рамки (своя на каждом масштабе экрана). Ровно по рабочей
                # области монитора — без вылета и без щелей, панель задач видна.
                work = _work_area(msg.hWnd)
                if work is not None:
                    params = _NCCALCSIZE_PARAMS.from_address(msg.lParam)
                    params.rgrc[0] = work
            return True, 0
        if msg.message == WM_NCHITTEST:
            # Точка — из сообщения (физические пиксели экрана), а не курсор:
            # Windows спрашивает и о точках, где курсора нет.
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
            return True, self._hit_test_native(msg.hWnd, x, y)
        return None

    def _hit_test_native(self, hwnd, x: int, y: int) -> int:
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        zoomed = bool(ctypes.windll.user32.IsZoomed(hwnd))
        ratio = self.devicePixelRatioF() or 1.0
        if not zoomed and not self.isFullScreen():
            b = int(round(RESIZE_BORDER * ratio))
            left = x < rect.left + b
            right = x >= rect.right - b
            top = y < rect.top + b
            bottom = y >= rect.bottom - b
            if top and left:
                return HTTOPLEFT
            if top and right:
                return HTTOPRIGHT
            if bottom and left:
                return HTBOTTOMLEFT
            if bottom and right:
                return HTBOTTOMRIGHT
            if left:
                return HTLEFT
            if right:
                return HTRIGHT
            if top:
                return HTTOP
            if bottom:
                return HTBOTTOM
        # Физическая точка -> логическая точка Qt через начало клиентской области.
        origin = wintypes.POINT(0, 0)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(origin))
        local = QPoint(int((x - origin.x) / ratio), int((y - origin.y) / ratio))
        bar = self._title_bar
        if bar is not None and bar.isVisible() and bar.is_drag_area(self.mapToGlobal(local)):
            return HTCAPTION
        return HTCLIENT

    def _hit_test(self, pos: QPoint) -> int:
        """То же по логической точке (для тестов и не-Windows)."""
        geo = self.frameGeometry()
        if not self.isMaximized() and not self.isFullScreen():
            b = RESIZE_BORDER
            left = pos.x() < geo.left() + b
            right = pos.x() > geo.right() - b
            top = pos.y() < geo.top() + b
            bottom = pos.y() > geo.bottom() - b
            if top and left:
                return HTTOPLEFT
            if top and right:
                return HTTOPRIGHT
            if bottom and left:
                return HTBOTTOMLEFT
            if bottom and right:
                return HTBOTTOMRIGHT
            if left:
                return HTLEFT
            if right:
                return HTRIGHT
            if top:
                return HTTOP
            if bottom:
                return HTBOTTOM
        bar = self._title_bar
        if bar is not None and bar.isVisible() and bar.is_drag_area(pos):
            return HTCAPTION
        return HTCLIENT

    # ------------------------------------------------------------------
    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
