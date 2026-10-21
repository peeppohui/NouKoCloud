"""Оформление «НоуКо Cloud»: палитры, шрифты, иконки, таблица стилей.

Всё взято из айдентики продакшена «НоуКо» (холст «НоуКо Cloud — дизайн
программы»): тёмная тема по умолчанию и светлая, шрифты Soyuz Grotesk
(заголовки, только строчными), Actay (текст), Actay Wide (кнопки, имена
файлов, цифры) и Actay Condensed Thin (крупные цифры).

Стеклянные панели в макете размывают фон (backdrop-filter). В Qt Widgets
такого размытия нет — панели полупрозрачные, сквозь них видна сетка звёзд.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap

log = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICONS_DIR = Path(__file__).resolve().parent / "icons"

# --- Шрифты --------------------------------------------------------------
BODY = "Actay"
WIDE = "Actay Wide"
HEAD = "Soyuz Grotesk"
THIN = "Actay Condensed"
FALLBACK = "Segoe UI"

_FONT_FILES = (
    "Actay-Regular.otf",
    "ActayWide-Bold.otf",
    "SoyuzGrotesk-Bold.otf",
    "ActayCondensed-Thin.otf",
)
_fonts_loaded = False


def load_fonts() -> None:
    """Регистрирует шрифты программы (один раз; нужен QGuiApplication)."""
    global _fonts_loaded
    if _fonts_loaded:
        return
    _fonts_loaded = True
    for name in _FONT_FILES:
        path = ASSETS_DIR / "fonts" / name
        if QFontDatabase.addApplicationFont(str(path)) < 0:
            log.warning("Шрифт не загрузился: %s", path)


def font(family: str = BODY, size: float = 14, bold: bool = False) -> QFont:
    f = QFont([family, FALLBACK])
    f.setPointSizeF(size * 0.75)          # px из макета -> pt (96 dpi)
    if bold:
        f.setWeight(QFont.Weight.Bold)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


# --- Палитры -------------------------------------------------------------
@dataclass(frozen=True)
class Palette:
    name: str
    bg: str          # фон окна
    panel: str       # непрозрачная панель
    glass: str       # полупрозрачная панель поверх звёзд
    glass_solid: str  # окна поверх затемнения: плотнее, т.к. размытия нет
    edge: str        # тонкая обводка стекла
    raised: str      # выделение, активный раздел, дорожки прогресса
    line: str        # разделители
    strong: str      # обводка кнопок и полей
    text: str
    muted: str
    pri: str         # главная кнопка
    pri_text: str
    field: str       # фон полей ввода
    ok: str
    err: str
    scrim: str       # затемнение под окнами
    stage: str       # фон под видео
    stars: str
    stars_opacity: float
    logo: str


DARK = Palette(
    name="dark",
    bg="#06141B", panel="#11212D",
    glass="rgba(17, 33, 45, 0.80)", glass_solid="rgba(17, 33, 45, 0.94)",
    edge="rgba(204, 208, 207, 0.10)", raised="rgba(37, 55, 69, 0.60)",
    line="#253745", strong="#4A5C6A", text="#CCD0CF", muted="#9BA8AB",
    pri="#CCD0CF", pri_text="#06141B", field="rgba(6, 20, 27, 0.45)",
    ok="#7FB89A", err="#E08A7F", scrim="rgba(6, 20, 27, 0.62)",
    stage="rgba(6, 20, 27, 0.80)",
    stars="stars_dark.png", stars_opacity=0.35, logo="logo_dark.png",
)

LIGHT = Palette(
    name="light",
    bg="#FFFFFF", panel="#F2F4F3",
    glass="rgba(242, 244, 243, 0.88)", glass_solid="rgba(242, 244, 243, 0.96)",
    edge="rgba(255, 255, 255, 0.70)", raised="rgba(205, 211, 210, 0.55)",
    line="#CCD0CF", strong="#9BA8AB", text="#06141B", muted="#4A5C6A",
    pri="#06141B", pri_text="#F2F4F3", field="rgba(255, 255, 255, 0.55)",
    ok="#2E6B4F", err="#A33A2E", scrim="rgba(37, 55, 69, 0.22)",
    stage="#0B1A22",
    stars="stars_light.png", stars_opacity=0.75, logo="logo_light.png",
)

PALETTES = {"dark": DARK, "light": LIGHT}


def qcolor(value: str) -> QColor:
    """'#RRGGBB' или 'rgba(r, g, b, a)' -> QColor."""
    value = value.strip()
    if value.startswith("rgba"):
        parts = [p.strip() for p in value[value.index("(") + 1:value.index(")")].split(",")]
        r, g, b = (int(float(p)) for p in parts[:3])
        a = float(parts[3]) if len(parts) > 3 else 1.0
        return QColor(r, g, b, int(round(a * 255)))
    return QColor(value)


def qt_palette(p: Palette):
    """Системные цвета Qt под тему: иначе выделение и подсветка берут
    акцентный цвет Windows (сиреневый, синий — какой выбран у пользователя)."""
    from PySide6.QtGui import QPalette

    pal = QPalette()
    solid_raised = theme_raised_solid(p)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        pal.setColor(group, QPalette.ColorRole.Window, QColor(p.bg))
        pal.setColor(group, QPalette.ColorRole.Base, QColor(p.panel))
        pal.setColor(group, QPalette.ColorRole.AlternateBase, QColor(p.panel))
        pal.setColor(group, QPalette.ColorRole.Button, QColor(p.panel))
        pal.setColor(group, QPalette.ColorRole.Text, QColor(p.text))
        pal.setColor(group, QPalette.ColorRole.WindowText, QColor(p.text))
        pal.setColor(group, QPalette.ColorRole.ButtonText, QColor(p.text))
        pal.setColor(group, QPalette.ColorRole.PlaceholderText, QColor(p.muted))
        pal.setColor(group, QPalette.ColorRole.Highlight, solid_raised)
        pal.setColor(group, QPalette.ColorRole.HighlightedText, QColor(p.text))
        pal.setColor(group, QPalette.ColorRole.Link, QColor(p.text))
        pal.setColor(group, QPalette.ColorRole.ToolTipBase, QColor(p.panel))
        pal.setColor(group, QPalette.ColorRole.ToolTipText, QColor(p.text))
        if hasattr(QPalette.ColorRole, "Accent"):
            pal.setColor(group, QPalette.ColorRole.Accent, QColor(p.text))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p.strong))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p.strong))
    return pal


def theme_raised_solid(p: Palette) -> QColor:
    """Цвет «raised» без прозрачности (поверх фона панели)."""
    over = qcolor(p.raised)
    base = QColor(p.panel)
    a = over.alphaF()
    return QColor(int(over.red() * a + base.red() * (1 - a)),
                  int(over.green() * a + base.green() * (1 - a)),
                  int(over.blue() * a + base.blue() * (1 - a)))


class ThemeManager(QObject):
    """Текущая тема. Виджеты с картинками и иконками слушают `changed`."""

    changed = Signal(object)    # Palette

    def __init__(self) -> None:
        super().__init__()
        self.palette: Palette = DARK
        self.motion = False     # кнопки подсвечивает анимация (motion.py)

    def set_theme(self, name: str) -> None:
        palette = PALETTES.get(name, DARK)
        self.palette = palette
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            load_fonts()
            # Стиль Windows 11 дорисовывает своё поверх наших стилей: например,
            # вертикальные «палки» акцентного цвета у выделенной строки дерева
            # на каждом уровне вложенности. Fusion ничего своего не добавляет.
            if app.style().name().lower() != "fusion":
                app.setStyle("Fusion")
            app.setPalette(qt_palette(palette))
            app.setStyleSheet(stylesheet(palette, motion=self.motion))
        self.changed.emit(palette)

    def toggle(self) -> str:
        name = "light" if self.palette.name == "dark" else "dark"
        self.set_theme(name)
        return name


_manager: ThemeManager | None = None


def manager() -> ThemeManager:
    global _manager
    if _manager is None:
        _manager = ThemeManager()
    return _manager


def current() -> Palette:
    return manager().palette


# --- Иконки --------------------------------------------------------------
# Контурные иконки из макета (viewBox 0 0 24 24, цвет — currentColor).
ICONS: dict[str, str] = {
    "link": '<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1"/>'
            '<path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/>',
    "cloud": '<path d="M7 18h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6.1 9.6 4.2 4.2 0 0 0 7 18z"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3'
            'M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2'
           'M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    "moon": '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
    "minimize": '<path d="M6 12h12"/>',
    "maximize": '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    "restore": '<rect x="5" y="8" width="11" height="11" rx="2"/><path d="M9 5h8a2 2 0 0 1 2 2v8"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "video": '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10 9.5v5l4.5-2.5z"/>',
    "audio": '<path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/>'
             '<path d="M21 16l-5-5-9 9"/>',
    "doc": '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
    "download": '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    "chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7"/>',
    "dash": '<path d="M6 12h12"/>',
    "clock": '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
    "alert": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5v.01"/>',
    "volume": '<path d="M4 9.5h3.5L12 6v12l-4.5-3.5H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
    "mute": '<path d="M4 9.5h3.5L12 6v12l-4.5-3.5H4z"/><path d="M16 9.5l5 5M21 9.5l-5 5"/>',
    "drive": '<rect x="3" y="6" width="18" height="12" rx="3"/><path d="M7 14h.01M11 14h6"/>',
    "log": '<path d="M7 3h7l5 5v13H7z"/><path d="M10 12h6M10 16h6"/>',
    "social": '<rect x="3" y="5" width="18" height="14" rx="4"/><path d="M10.5 9.5v5l4-2.5z"/>'
              '<path d="M8 2.5h8"/>',
    "music": '<path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/>',
}
# Залитые фигуры (без обводки).
FILLED: dict[str, str] = {
    "play": '<path d="M8 5v14l11-7z"/>',
    "pause": '<rect x="6.5" y="5" width="4" height="14" rx="1.2"/><rect x="13.5" y="5" width="4" height="14" rx="1.2"/>',
    # Знак-«мишень» из логотипа — индикатор «качается».
    "target": None,  # см. svg_markup
}


def svg_markup(name: str, color: str, stroke: float = 1.8) -> str:
    if name == "target":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            f'<circle cx="12" cy="12" r="9" fill="none" stroke="{color}" stroke-width="2.2"/>'
            f'<circle cx="12" cy="12" r="4.8" fill="{color}"/></svg>'
        )
    if name in FILLED:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="{color}">'
                f'{FILLED[name]}</svg>')
    body = ICONS[name]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )


@lru_cache(maxsize=512)
def _pixmap(name: str, color: str, size: int, stroke: float, ratio: float) -> QPixmap:
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(svg_markup(name, color, stroke).encode()))
    px = int(round(size * ratio))
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def _ratio() -> float:
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    return max(1.0, screen.devicePixelRatio() if screen is not None else 1.0) * 2


def pixmap(name: str, color: str | None = None, size: int = 18, stroke: float = 1.8) -> QPixmap:
    return _pixmap(name, color or current().text, size, stroke, _ratio())


def icon(name: str, color: str | None = None, size: int = 18, stroke: float = 1.8) -> QIcon:
    """Иконка заданного цвета (по умолчанию — цвет текста темы)."""
    ic = QIcon()
    ic.addPixmap(pixmap(name, color, size, stroke))
    return ic


def asset(name: str) -> Path:
    return ASSETS_DIR / name


@lru_cache(maxsize=16)
def asset_pixmap(name: str) -> QPixmap:
    return QPixmap(str(asset(name)))


def logo_pixmap(height: int, palette: Palette | None = None) -> QPixmap:
    """Логотип «НоуКо» нужной высоты с учётом масштаба экрана."""
    palette = palette or current()
    ratio = _ratio() / 2
    src = asset_pixmap(palette.logo)
    scaled = src.scaledToHeight(int(height * ratio), Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(ratio)
    return scaled


# --- SVG для таблицы стилей (QSS принимает только файлы) ----------------
def _qss_icon_dir() -> Path:
    from ..core.paths import app_data_dir

    path = app_data_dir() / "ui"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _qss_icon(name: str, color: str, stroke: float = 2.4) -> str:
    """Файл с иконкой нужного цвета для url(...) в стилях."""
    safe = color.replace("#", "").replace(" ", "").replace("(", "").replace(")", "").replace(",", "-")
    path = _qss_icon_dir() / f"{name}-{safe}.svg"
    if not path.exists():
        path.write_text(svg_markup(name, color, stroke), encoding="utf-8")
    return path.as_posix()


# --- Таблица стилей ------------------------------------------------------
#: Мгновенная подсветка кнопок; с анимацией её рисует motion.ButtonAnimator.
_INSTANT_HOVER = (
    "QPushButton:hover, QToolButton:hover {{ background: {p.raised}; }}",
    'QPushButton[kind="primary"]:hover {{ background: {p.muted}; }}',
    'QToolButton[kind="iconFramed"]:hover {{ background: {p.raised}; }}',
    'QToolButton[kind="play"]:hover {{ background: {p.muted}; }}',
)


def stylesheet(p: Palette, motion: bool = False) -> str:
    css = _stylesheet(p)
    if motion:
        for rule in _INSTANT_HOVER:
            css = css.replace(rule.format(p=p) + "\n", "")
    return css


def _stylesheet(p: Palette) -> str:
    check = _qss_icon("check", p.pri_text, 3.0)
    dash = _qss_icon("dash", p.pri_text, 3.0)
    branch_closed = _qss_icon("chevron-right", p.muted, 2.0)
    branch_open = _qss_icon("chevron-down", p.muted, 2.0)
    sort_up = _qss_icon("chevron-down", p.muted, 2.0)
    combo_arrow = _qss_icon("chevron-down", p.text, 2.2)
    return f"""
* {{
    font-family: "{BODY}", "{FALLBACK}";
    font-size: 14px;
    color: {p.text};
    outline: none;
}}
QWidget {{ background: transparent; }}
QMainWindow, #windowRoot {{ background: {p.bg}; }}
QDialog {{ background: {p.panel}; }}
QDialog#overlay {{ background: transparent; }}

QToolTip {{
    background: {p.panel}; color: {p.text};
    border: 1px solid {p.line}; border-radius: 8px; padding: 6px 10px;
}}

/* --- Шапка -------------------------------------------------------- */
#titleBar {{ background: {p.bg}; border-bottom: 1px solid {p.line}; }}
#titleBar QLabel#cloudWord {{
    font-family: "{WIDE}"; font-weight: 700; font-size: 13px;
}}
#vline {{ background: {p.strong}; }}
#vlineSoft {{ background: {p.line}; }}
#linkForm {{
    background: {p.glass}; border: 1px solid {p.line}; border-radius: 22px;
}}
#linkForm QLineEdit {{ background: transparent; border: none; padding: 0; font-size: 13.5px; }}

/* --- Кнопки ------------------------------------------------------- */
/* Высота каждой кнопки задана здесь (min = max), скругление — ровно
   половина высоты: Qt рисует прямые углы, если радиус больше половины. */
QPushButton {{
    border-radius: 18px; border: 1px solid {p.strong}; background: transparent;
    padding: 0 16px; min-height: 36px; max-height: 36px; font-size: 13px;
}}
QToolButton {{ border: none; background: transparent; padding: 0; }}
QPushButton:hover, QToolButton:hover {{ background: {p.raised}; }}
QPushButton:disabled {{ color: {p.strong}; border-color: {p.line}; }}
QPushButton[kind="primary"] {{
    background: {p.pri}; color: {p.pri_text}; border: none;
    font-family: "{WIDE}"; font-weight: 700; font-size: 14px;
    min-height: 48px; max-height: 48px; border-radius: 24px; padding: 0 24px;
}}
QPushButton[kind="primary"]:hover {{ background: {p.muted}; }}
QPushButton[kind="primary"]:disabled {{ background: {p.raised}; color: {p.muted}; }}
QPushButton[kind="primary"][variant="s"] {{
    min-height: 34px; max-height: 34px; border-radius: 17px; padding: 0 16px; font-size: 12px;
}}
QPushButton[kind="primary"][variant="m"] {{
    min-height: 44px; max-height: 44px; border-radius: 22px; padding: 0 22px; font-size: 13px;
}}
QPushButton[kind="primary"][variant="xl"] {{
    min-height: 52px; max-height: 52px; border-radius: 26px; padding: 0 28px;
}}
QPushButton[kind="outline"] {{
    font-family: "{WIDE}"; font-weight: 700; font-size: 13px;
    min-height: 44px; max-height: 44px; border-radius: 22px; padding: 0 20px;
}}
QPushButton[kind="outline"][variant="plain"] {{ font-family: "{BODY}"; font-weight: 400; font-size: 14px; }}
QPushButton[kind="outline"][variant="xl"] {{
    font-family: "{BODY}"; font-weight: 400; font-size: 14px;
    min-height: 48px; max-height: 48px; border-radius: 24px; padding: 0 24px;
}}
QPushButton[kind="soft"] {{
    min-height: 40px; max-height: 40px; border-radius: 20px; padding: 0 16px;
}}
QPushButton[kind="soft"][variant="l"] {{
    min-height: 44px; max-height: 44px; border-radius: 22px; padding: 0 18px; font-size: 14px;
}}
QPushButton[kind="nav"], QPushButton[kind="ghost"] {{
    border: none; min-height: 44px; max-height: 44px; border-radius: 22px; padding: 0 14px; font-size: 14px;
}}
QPushButton[kind="ghost"][variant="s"] {{
    min-height: 30px; max-height: 30px; border-radius: 15px; padding: 0 10px; font-size: 12.5px;
}}
QPushButton[kind="nav"][active="true"] {{ background: {p.raised}; }}
QPushButton[kind="link"] {{
    border: none; padding: 0 8px; min-height: 40px; max-height: 40px; text-decoration: underline;
}}
QPushButton[kind="link"]:hover {{ background: transparent; color: {p.muted}; }}
QPushButton[kind="row"] {{
    border: none; padding: 0; min-height: 40px; max-height: 40px; border-radius: 8px; text-align: left;
}}
QPushButton[kind="row"]:hover {{ background: transparent; }}
QToolButton[kind="icon"] {{ border-radius: 22px; }}
QToolButton[kind="icon"][dim="40"] {{ border-radius: 20px; }}
QToolButton[kind="icon"][dim="36"] {{ border-radius: 18px; }}
QToolButton[kind="iconFramed"] {{
    border: 1px solid {p.line}; background: {p.glass}; border-radius: 22px;
}}
QToolButton[kind="iconFramed"]:hover {{ background: {p.raised}; }}
QToolButton[kind="play"] {{ background: {p.pri}; border-radius: 22px; }}
QToolButton[kind="play"]:hover {{ background: {p.muted}; }}
#closeBtn:hover {{ background: #A33A2E; }}

/* --- Панели ------------------------------------------------------- */
*[glass="true"] {{
    background: {p.glass}; border: 1px solid {p.edge}; border-radius: 20px;
}}
QFrame#overlayCard {{
    background: {p.glass_solid}; border: 1px solid {p.edge}; border-radius: 28px;
}}
QFrame[card="true"] {{
    background: transparent; border: 1px solid {p.line}; border-radius: 20px;
}}
QFrame[job="true"] {{
    background: transparent; border: 1px solid {p.line}; border-radius: 14px;
}}
QFrame[recent="true"] {{
    background: {p.glass}; border: 1px solid {p.edge}; border-radius: 16px;
}}
QFrame#stage {{ background: {p.stage}; border: 1px solid rgba(204, 208, 207, 0.10); border-radius: 14px; }}
QFrame#stage QLabel {{ color: #9BA8AB; }}
QFrame[hline="true"] {{ background: {p.line}; border: none; max-height: 1px; min-height: 1px; }}
#footerBar {{ border-top: 1px solid {p.line}; }}
#footerBar QLabel {{ font-size: 12.5px; }}

QFrame#segTabs {{ background: {p.glass}; border: 1px solid {p.edge}; border-radius: 24px; }}
QFrame#segTabs QPushButton {{
    border: none; min-height: 40px; border-radius: 20px; padding: 0 20px;
    font-family: "{WIDE}"; font-weight: 700; font-size: 13px;
}}
QFrame#segTabs QPushButton:checked {{ background: {p.raised}; }}
QLabel#badge {{
    background: {p.pri}; color: {p.pri_text}; border-radius: 11px;
    min-width: 22px; max-height: 22px; min-height: 22px; padding: 0 6px;
    font-family: "{WIDE}"; font-weight: 700; font-size: 11px;
}}

/* --- Текст -------------------------------------------------------- */
QLabel[role="h1"] {{ font-family: "{HEAD}"; font-weight: 700; font-size: 52px; }}
QLabel[role="h2"] {{ font-family: "{HEAD}"; font-weight: 700; font-size: 32px; }}
QLabel[role="h3"] {{ font-family: "{HEAD}"; font-weight: 700; font-size: 28px; }}
QLabel[role="wide"] {{ font-family: "{WIDE}"; font-weight: 700; }}
QLabel[role="name"] {{ font-family: "{WIDE}"; font-weight: 700; font-size: 16px; }}
QLabel[role="caps"] {{ font-size: 12px; color: {p.muted}; }}
QLabel[role="lead"] {{ font-size: 16px; color: {p.muted}; }}
QLabel[muted="true"], QLabel[role="muted"] {{ color: {p.muted}; font-size: 13px; }}
QLabel[role="hint"] {{ color: {p.muted}; font-size: 12.5px; }}
QLabel[heading="true"] {{ font-family: "{WIDE}"; font-weight: 700; font-size: 16px; }}
QLabel[error="true"] {{ color: {p.err}; }}
QLabel[ok="true"] {{ color: {p.ok}; }}
QLabel[chip="muted"], QLabel[chip="ok"], QLabel[chip="err"], QLabel[chip="plain"] {{
    background: {p.raised}; border-radius: 14px; padding: 0 12px;
    min-height: 28px; max-height: 28px; font-size: 12.5px;
}}
QLabel[chip="muted"] {{ color: {p.muted}; }}
QLabel[chip="ok"] {{ color: {p.ok}; }}
QLabel[chip="err"] {{ color: {p.err}; }}
QLabel[avatar="true"] {{ background: {p.raised}; border-radius: 20px; }}
QLabel[jobIcon="true"] {{ background: {p.raised}; border-radius: 18px; }}

/* --- Поля ввода --------------------------------------------------- */
QLineEdit, QSpinBox, QDoubleSpinBox {{
    background: {p.field}; border: 1px solid {p.strong}; border-radius: 21px;
    min-height: 40px; padding: 0 16px; font-size: 13.5px;
    selection-background-color: {p.strong}; selection-color: {p.text};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {p.text}; }}
QLineEdit[variant="l"] {{ min-height: 46px; border-radius: 23px; padding: 0 18px; }}
QSpinBox, QDoubleSpinBox {{
    font-family: "{WIDE}"; font-weight: 700; font-size: 13px; padding: 0 12px; min-width: 64px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}

QComboBox {{
    background: {p.field}; border: 1px solid {p.strong}; border-radius: 21px;
    min-height: 40px; padding: 0 36px 0 16px; font-size: 13.5px;
}}
QComboBox:focus {{ border-color: {p.text}; }}
QComboBox::drop-down {{ border: none; width: 32px; subcontrol-origin: padding; subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: url("{combo_arrow}"); width: 12px; height: 12px; margin-right: 12px; }}
QComboBox QAbstractItemView {{
    background: {p.panel}; border: 1px solid {p.line}; border-radius: 10px; padding: 4px;
    selection-background-color: {p.raised}; selection-color: {p.text}; outline: none;
}}

QGroupBox {{
    border: 1px solid {p.line}; border-radius: 20px; margin-top: 12px;
    padding: 22px 18px 18px 18px; background: transparent;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 18px; padding: 0 8px;
    font-family: "{WIDE}"; font-weight: 700; font-size: 13px;
}}

QCheckBox {{ spacing: 10px; font-size: 13px; }}
QCheckBox::indicator, QTreeView::indicator {{
    width: 16px; height: 16px; border-radius: 5px;
    border: 1.5px solid {p.strong}; background: {p.field};
}}
QCheckBox::indicator:hover, QTreeView::indicator:hover {{ border-color: {p.text}; }}
QCheckBox::indicator:checked, QTreeView::indicator:checked {{
    background: {p.text}; border-color: {p.text}; image: url("{check}");
}}
QTreeView::indicator:indeterminate {{
    background: {p.text}; border-color: {p.text}; image: url("{dash}");
}}

/* --- Дерево файлов ------------------------------------------------ */
QTreeView {{
    background: transparent; border: none; padding: 4px 8px;
    show-decoration-selected: 1; font-size: 14px;
}}
QTreeView::item {{ min-height: 38px; border: none; padding: 0 2px; }}
QTreeView::item:hover {{ background: {p.raised}; }}
QTreeView::item:selected {{ background: {p.raised}; color: {p.text}; }}
QTreeView::branch {{ background: transparent; }}
QTreeView::branch:selected {{ background: {p.raised}; }}
QTreeView::branch:hover {{ background: {p.raised}; }}
QTreeView::branch:has-children:!has-siblings:closed,
QTreeView::branch:closed:has-children:has-siblings {{ image: url("{branch_closed}"); }}
QTreeView::branch:open:has-children:!has-siblings,
QTreeView::branch:open:has-children:has-siblings {{ image: url("{branch_open}"); }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background: transparent; border: none; border-bottom: 1px solid {p.line};
    padding: 8px 10px; font-size: 12px; color: {p.muted};
}}
QHeaderView::down-arrow, QHeaderView::up-arrow {{ width: 10px; height: 10px; image: url("{sort_up}"); }}

/* --- Ползунки ----------------------------------------------------- */
QSlider::groove:horizontal {{ height: 6px; border-radius: 3px; background: {p.raised}; }}
QSlider::sub-page:horizontal {{ height: 6px; border-radius: 3px; background: {p.text}; }}
QSlider::add-page:horizontal {{ height: 6px; border-radius: 3px; background: {p.raised}; }}
QSlider::handle:horizontal {{
    width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; background: {p.text};
}}
QSlider::sub-page:horizontal:disabled {{ background: {p.strong}; }}
QSlider::handle:horizontal:disabled {{ background: {p.strong}; }}

QProgressBar {{ border: none; border-radius: 3px; background: {p.raised}; max-height: 6px; }}
QProgressBar::chunk {{ background: {p.text}; border-radius: 3px; }}

/* --- Прокрутка ---------------------------------------------------- */
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: {p.line}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.strong}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle:horizontal {{ background: {p.line}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: transparent; }}

QMenu {{
    background: {p.panel}; border: 1px solid {p.line}; border-radius: 12px; padding: 6px;
}}
QMenu::item {{ padding: 8px 24px 8px 14px; border-radius: 8px; }}
QMenu::item:selected {{ background: {p.raised}; }}
QMessageBox {{ background: {p.panel}; }}
QMessageBox QLabel {{ font-size: 14px; }}
QMessageBox QPushButton {{ min-width: 90px; }}
"""


# --- Значок программы ------------------------------------------------------
def draw_app_icon(size: int) -> QPixmap:
    """Знак-«мишень» из логотипа на тёмном скруглённом квадрате."""
    from PySide6.QtGui import QBrush, QPainterPath, QPen

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    radius = size * 0.22
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.fillPath(path, QColor("#06141B"))
    light = QColor("#CCD0CF")
    pen = QPen(light)
    pen.setWidthF(size * 0.075)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    c = size / 2
    r_outer = size * 0.30
    painter.drawEllipse(QRectF(c - r_outer, c - r_outer, 2 * r_outer, 2 * r_outer))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(light))
    r_inner = size * 0.16
    painter.drawEllipse(QRectF(c - r_inner, c - r_inner, 2 * r_inner, 2 * r_inner))
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    ic = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        ic.addPixmap(draw_app_icon(size))
    return ic
