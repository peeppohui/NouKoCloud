"""Виджет превью: видео, аудио и изображения.

Видео и звук играет QtMultimedia — внутри у него FFmpeg, поэтому
открываются исходники с камер: H.264/HEVC/ProRes/DNxHD в mp4, mov, mkv,
avi, mts, mxf. (Раньше здесь был Chromium из QtWebEngine — он собран
без H.264/AAC и почти ничего из этого не показывал.)

Файл плеер берёт с локального сервера превью (127.0.0.1), который
проксирует облако с поддержкой Range — перемотка не качает файл целиком.
"""

from __future__ import annotations

import logging
import os
import time

from PySide6.QtCore import (
    QBuffer, QByteArray, QEvent, QIODevice, QPointF, QSize, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QIcon, QImage, QImageReader, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

    MULTIMEDIA_AVAILABLE = True
except Exception as exc:  # pragma: no cover - сборка без QtMultimedia
    log.warning("QtMultimedia недоступен: %s", exc)
    MULTIMEDIA_AVAILABLE = False

SEEK_STEP_MS = 5000
FINE_STEP_MS = 1000            # Shift + стрелка
#: Звук отдельной дорожкой (YouTube). Звук — главные часы, его не трогаем
#: (каждая перемотка звука — пауза в звуке). Видео догоняет его скоростью:
#: расхождение больше AV_DRIFT_MS — скорость видео ±до AV_MAX_RATE_SHIFT;
#: больше AV_JUMP_MS — видео перематывается к звуку.
AV_DRIFT_MS = 60
AV_JUMP_MS = 1000
AV_MAX_RATE_SHIFT = 0.10
AV_START_TIMEOUT_S = 3.0      # звук так и не зазвучал — видео всё равно пускаем
# Буферизация: меньше этого запаса впереди (в секундах видео) — ставим паузу,
# а не показываем рваное видео по 3 кадра в секунду.
LOW_WATER_S = 0.8
# Если интернет быстрее видео — достаточно такого запаса, чтобы продолжить.
FAST_LINK_RESUME_S = 3.0
# Если медленнее — копим запас, которого хватит на столько секунд без остановок.
# Больше — реже остановки, но дольше первое ожидание; 10 с — разумная середина.
SLOW_LINK_HORIZON_S = 10.0
# Кнопку «Смотреть сразу» показываем, если ждать дольше этого.
OFFER_NO_WAIT_S = 3.0
BUFFER_CHECK_MS = 200
# Полный экран: через сколько секунд бездействия мыши прячем панель и курсор.
FULLSCREEN_IDLE_S = 2.5
FULLSCREEN_TICK_MS = 200
IMAGE_MAX_SIDE = 4096          # крупнее не нужно для превью, а память экономим
IMAGE_ALLOCATION_LIMIT_MB = 1024

PLACEHOLDER = "Выберите файл в списке слева, чтобы посмотреть превью"


def _ensure_localhost_bypasses_proxy() -> None:
    """FFmpeg внутри плеера читает переменные http_proxy/no_proxy.

    Если у пользователя задан прокси, запросы к нашему 127.0.0.1 ушли бы
    в него и превью не открылось бы. Исключаем localhost явно.
    """
    for name in ("no_proxy", "NO_PROXY"):
        current = os.environ.get(name, "")
        hosts = [h.strip() for h in current.split(",") if h.strip()]
        for host in ("127.0.0.1", "localhost"):
            if host not in hosts:
                hosts.append(host)
        os.environ[name] = ",".join(hosts)


def format_ms(ms: int) -> str:
    seconds = max(0, int(ms) // 1000)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


#: Известные частоты кадров (NDF). Всё, что близко к ним, округляем к ним.
FPS_PRESETS = (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0)


def normalize_fps(value) -> float:
    """Частота кадров из метаданных видео. 0 — частота неизвестна.

    Метаданные отдают разное: 23.98, 24000/1001, 25, «29.97». Приводим
    к списку известных, остальное округляем до трёх знаков.
    """
    try:
        fps = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if fps <= 0:
        return 0.0
    for preset in FPS_PRESETS:
        if abs(fps - preset) <= 0.02:
            return preset
    return round(fps, 3)


def nominal_fps(fps: float) -> int:
    """«Номинальная» частота для таймкода: 23.976 → 24, 29.97 → 30."""
    if not fps or fps <= 0:
        return 0
    return max(1, int(round(fps)))


def frame_step_ms(fps: float) -> int:
    """Длительность кадра в миллисекундах (без FPS — условные 40 мс)."""
    if fps and fps > 0:
        return max(1, int(round(1000.0 / fps)))
    return 40


def format_timecode(ms: int, fps: float) -> str:
    """Таймкод: HH:MM:SS:FF, если известна частота кадров, иначе HH:MM:SS.mmm."""
    ms = max(0, int(ms or 0))
    if fps and fps > 0:
        nominal = nominal_fps(fps)
        total_frames = int(round(ms * nominal / 1000.0))
        frames = total_frames % nominal
        total_seconds = total_frames // nominal
        secs = total_seconds % 60
        minutes = (total_seconds // 60) % 60
        hours = total_seconds // 3600
        return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"
    secs, rest = divmod(ms, 1000)
    minutes, secs = divmod(secs, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{rest:03d}"


class SeekSlider(QSlider):
    """Ползунок, который по клику прыгает в точку клика (а не на шаг)."""

    jumped = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._marks: tuple[int | None, int | None] = (None, None)

    def set_marks(self, start: int | None, end: int | None) -> None:
        """Отметки фрагмента (мс): подсвеченный участок и риски по краям."""
        self._marks = (start, end)
        self.update()

    def _x_for(self, value: int) -> int:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self
        )
        span = max(1, self.width() - handle.width())
        return QStyle.sliderPositionFromValue(self.minimum(), self.maximum(), value, span) + handle.width() // 2

    def paintEvent(self, event) -> None:  # noqa: N802
        start, end = self._marks
        if self.maximum() <= self.minimum() or (start is None and end is None):
            super().paintEvent(event)
            return
        try:
            from ..ui import theme as _theme

            accent = QColor(_theme.current().text)
        except Exception:  # pragma: no cover
            accent = QColor("#ffffff")
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        mid = self.height() // 2
        x0 = self._x_for(start if start is not None else self.minimum())
        x1 = self._x_for(end if end is not None else self.maximum())
        band = QColor(accent)
        band.setAlpha(80)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(band)
        painter.drawRoundedRect(min(x0, x1), mid - 7, abs(x1 - x0), 14, 4, 4)
        painter.setBrush(accent)
        for value in (start, end):
            if value is not None:
                x = self._x_for(value)
                painter.drawRoundedRect(x - 2, mid - 10, 4, 20, 2, 2)
        painter.end()
        super().paintEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self
            )
            point = event.position().toPoint()
            if not handle.contains(point):
                span = max(1, self.width() - handle.width())
                value = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), point.x() - handle.width() // 2, span
                )
                self.setValue(value)
                self.jumped.emit(value)
                event.accept()
                return
        super().mousePressEvent(event)


def _is_letter_key(event, key: Qt.Key, virtual_key: int) -> bool:
    """Буква по положению клавиши — работает в любой раскладке.

    В русской раскладке Qt отдаёт «А», а не F, поэтому сверяем ещё и
    код клавиши Windows (так же сделано для I и O в «Соцсетях»).
    """
    blocked = (
        Qt.KeyboardModifier.ControlModifier
        | Qt.KeyboardModifier.AltModifier
        | Qt.KeyboardModifier.MetaModifier
    )
    if event.modifiers() & blocked:
        return False
    return event.key() == key or event.nativeVirtualKey() == virtual_key


def _fullscreen_icon(color: QColor, exit_mode: bool = False, size: int = 20) -> QIcon:
    """Значок «на весь экран» (уголки наружу) или «выйти» (уголки внутрь)."""
    ratio = 2
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color)
    pen.setWidthF(1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    low, high, arm = size * 0.2, size * 0.8, size * 0.26
    for cx, cy in ((low, low), (high, low), (high, high), (low, high)):
        dx = 1 if cx == low else -1      # куда смотрит «внутрь» по горизонтали
        dy = 1 if cy == low else -1
        end_a = QPointF(cx + dx * arm, cy)
        end_b = QPointF(cx, cy + dy * arm)
        vertex = QPointF(cx + dx * arm, cy + dy * arm) if exit_mode else QPointF(cx, cy)
        path = QPainterPath(end_a)
        path.lineTo(vertex)
        path.lineTo(end_b)
        painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)


class FullscreenWindow(QWidget):
    """Окно «на весь экран».

    Видео и панель управления переезжают сюда из PreviewPlayer на время
    просмотра (см. PreviewPlayer.enter_fullscreen). Свои клавиши у окна
    только Esc и F — выход; остальные уходят плееру, поэтому пробел,
    стрелки, I и O работают как в обычном окне.
    """

    leave_requested = Signal()
    activity = Signal()

    def __init__(self, owner: QWidget) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self._owner = owner
        self.setWindowTitle("НоуКо Cloud — превью")
        self.setWindowIcon(owner.window().windowIcon())
        self.setObjectName("fullscreenStage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("#fullscreenStage { background: #000000; }")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(0)

        # Нижняя панель: тёмная, чтобы значки читались и в светлой теме.
        self.bar = QFrame(self)
        self.bar.setObjectName("fullscreenBar")
        self.bar.setStyleSheet(
            "#fullscreenBar { background: rgba(14, 14, 18, 235); }"
            "#fullscreenBar QLabel { color: #f2f2f2; }"
        )
        self.bar_layout = QHBoxLayout(self.bar)
        self.bar_layout.setContentsMargins(20, 8, 20, 12)
        self.bar_layout.setSpacing(0)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape or _is_letter_key(event, Qt.Key.Key_F, 0x46):
            event.accept()
            self.leave_requested.emit()
            return
        self.activity.emit()
        # Остальное — плееру: он обработает или отдаст выше по цепочке.
        QApplication.sendEvent(self._owner, event)
        event.accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        # Alt+F4 и подобное: не закрываем окно, а возвращаем видео на место.
        event.ignore()
        self.leave_requested.emit()


class ImageView(QLabel):
    """Изображение, вписанное в область с сохранением пропорций."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(1, 1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def set_image(self, image: QImage) -> None:
        self._pixmap = QPixmap.fromImage(image)
        self._rescale()

    def clear_image(self) -> None:
        self._pixmap = None
        self.clear()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            return
        target = self.size()
        if self._pixmap.width() <= target.width() and self._pixmap.height() <= target.height():
            self.setPixmap(self._pixmap)  # мелкие картинки не растягиваем
        else:
            self.setPixmap(self._pixmap.scaled(
                target, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))


class PreviewPlayer(QWidget):
    """Плеер превью. Работает только в GUI-потоке."""

    #: Плеер не смог воспроизвести файл: (сообщение, дело в формате файла).
    failed = Signal(str, bool)
    #: Пользователь просит сохранить кадр: (QImage или None, позиция в мс).
    frame_save_requested = Signal(object, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._kind = ""
        self._url = ""
        self._image_reply = None
        self._expect_video = False
        # Буферизация (см. _check_buffer): probe(доля_позиции) -> BufferState | None.
        self._buffer_probe = None
        self._want_play = False     # пользователь хочет, чтобы видео играло
        self._buffering = False     # мы сами поставили паузу, пока копится запас
        self._no_wait = False       # пользователь выбрал «смотреть сразу, без буфера»
        self._first_frame = False   # первый кадр уже на экране
        self._aplayer = None        # звук отдельной дорожкой (у YouTube видео и звук раздельно)
        self._stop_at: int | None = None   # «проиграть фрагмент»: пауза на этой позиции
        # Таймкод и работа по кадрам (ТЗ 4.9).
        self._fps = 0.0                    # частота кадров; 0 — неизвестна
        self._in_ms: int | None = None     # отметка in
        self._out_ms: int | None = None    # отметка out
        self._speed_index = 0              # 0 = 1x, 1 = 2x, 2 = 4x, 3 = 8x
        self._title = ""                   # имя файла (для имени сохраняемого кадра)
        self._av_timer = QTimer(self)
        self._av_timer.setInterval(100)
        self._av_start: tuple[int, float] | None = None  # старт: (позиция звука, когда запустили)
        self._av_frame = False               # после перемотки видео уже показало кадр
        self._av_wait_since = 0.0
        self._av_timer.timeout.connect(self._sync_audio)
        self._buffer_timer = QTimer(self)
        self._buffer_timer.setInterval(BUFFER_CHECK_MS)
        self._buffer_timer.timeout.connect(self._check_buffer)

        # Полный экран (см. enter_fullscreen).
        self._fs: FullscreenWindow | None = None
        self._fs_saved_message = ""
        self._fs_last_pos = None
        self._fs_idle_since = 0.0
        self._fs_timer = QTimer(self)
        self._fs_timer.setInterval(FULLSCREEN_TICK_MS)
        self._fs_timer.timeout.connect(self._fs_tick)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._layout = layout

        # Сцена: тёмная при любой теме — видео смотрят на тёмном (стиль «stage»).
        self._stack = QStackedWidget(self)
        self._stack.setObjectName("stage")
        self._stack.setMinimumHeight(200)
        layout.addWidget(self._stack, 1)

        self._message = QLabel(PLACEHOLDER, self._stack)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setMargin(24)
        self._stack.addWidget(self._message)

        self._audio_page = QLabel("", self._stack)
        self._audio_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._audio_page.setWordWrap(True)
        self._stack.addWidget(self._audio_page)

        self._image = ImageView(self._stack)
        self._stack.addWidget(self._image)

        self._video = None
        self._player = None
        self._audio = None
        self._network = None
        if MULTIMEDIA_AVAILABLE:
            _ensure_localhost_bypasses_proxy()
            QImageReader.setAllocationLimit(IMAGE_ALLOCATION_LIMIT_MB)
            self._video = QVideoWidget(self._stack)
            self._video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
            self._video.installEventFilter(self)   # двойной клик — на весь экран
            self._stack.addWidget(self._video)

            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._audio.setVolume(0.8)
            self._player.setAudioOutput(self._audio)
            self._player.setVideoOutput(self._video)
            self._player.durationChanged.connect(self._on_duration)
            self._player.positionChanged.connect(self._on_position)
            self._player.playbackStateChanged.connect(self._sync_play_button)
            self._player.mediaStatusChanged.connect(self._on_media_status)
            self._player.errorOccurred.connect(self._on_error)
            self._player.seekableChanged.connect(lambda ok: self._slider.setEnabled(ok))
            self._video.videoSink().videoFrameChanged.connect(self._on_frame)
            # Частота кадров приходит в метаданных — по ней считаем таймкод.
            if hasattr(self._player, "metaDataChanged"):
                self._player.metaDataChanged.connect(lambda *_args: self.read_fps())
            self._network = QNetworkAccessManager(self)

        # --- Панель управления ---------------------------------------
        self._controls = QWidget(self)
        row = QHBoxLayout(self._controls)
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(14)

        self._btn_play = QToolButton(self._controls)
        self._btn_play.setProperty("kind", "play")
        self._btn_play.setToolTip("Воспроизвести / пауза (пробел)")
        self._btn_play.setAccessibleName("Воспроизвести или поставить на паузу")
        self._btn_play.setFixedSize(44, 44)
        self._btn_play.setIconSize(QSize(18, 18))
        self._btn_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_play.clicked.connect(self.toggle_play)
        row.addWidget(self._btn_play)

        self._time = QLabel("0:00 / 0:00", self._controls)
        self._time.setProperty("role", "wide")
        self._time.setStyleSheet("font-size: 12px;")
        self._time.setMinimumWidth(96)
        self._time.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._time)

        # Таймкод: HH:MM:SS:FF, если известна частота кадров (ТЗ 4.9).
        self._timecode = QLabel("", self._controls)
        self._timecode.setProperty("muted", True)
        self._timecode.setStyleSheet("font-size: 12px;")
        self._timecode.setMinimumWidth(84)
        self._timecode.setToolTip("FPS не определён — таймкод с миллисекундами")
        self._timecode.setVisible(False)
        row.addWidget(self._timecode)

        self._slider = SeekSlider(self._controls)
        self._slider.setRange(0, 0)
        self._slider.setToolTip("Перемотка (← / → — на 5 секунд)")
        self._slider.sliderReleased.connect(lambda: self._seek(self._slider.value()))
        self._slider.jumped.connect(self._seek)
        self._slider.sliderMoved.connect(self._preview_time)
        row.addWidget(self._slider, 1)

        self._btn_mute = QToolButton(self._controls)
        self._btn_mute.setProperty("kind", "icon")
        self._btn_mute.setToolTip("Выключить / включить звук")
        self._btn_mute.setAccessibleName("Звук")
        self._btn_mute.setFixedSize(40, 40)
        self._btn_mute.setIconSize(QSize(20, 20))
        self._btn_mute.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mute.clicked.connect(self._toggle_mute)
        row.addWidget(self._btn_mute)

        self._volume = QSlider(Qt.Orientation.Horizontal, self._controls)
        self._volume.setRange(0, 100)
        self._volume.setValue(80)
        self._volume.setFixedWidth(80)
        self._volume.setToolTip("Громкость")
        self._volume.valueChanged.connect(self._on_volume)
        row.addWidget(self._volume)

        self._btn_full = QToolButton(self._controls)
        self._btn_full.setProperty("kind", "icon")
        self._btn_full.setAccessibleName("На весь экран")
        self._btn_full.setFixedSize(40, 40)
        self._btn_full.setIconSize(QSize(20, 20))
        self._btn_full.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_full.clicked.connect(self.toggle_fullscreen)
        self._btn_full.setVisible(False)       # только для видео
        row.addWidget(self._btn_full)
        layout.addWidget(self._controls)

        # --- Кадры, отметки in/out, экспорт кадра (только для видео) -----
        self._frame_row = QWidget(self)
        frames = QHBoxLayout(self._frame_row)
        frames.setContentsMargins(0, 0, 0, 0)
        frames.setSpacing(6)

        self._btn_frame_back = QPushButton("◀ кадр", self._frame_row)
        self._btn_frame_back.setProperty("kind", "soft")
        self._btn_frame_back.setToolTip("На кадр назад (клавиша , или J)")
        self._btn_frame_back.clicked.connect(lambda: self.step_frame(-1))
        frames.addWidget(self._btn_frame_back)

        self._btn_frame_fwd = QPushButton("кадр ▶", self._frame_row)
        self._btn_frame_fwd.setProperty("kind", "soft")
        self._btn_frame_fwd.setToolTip("На кадр вперёд (клавиша .)")
        self._btn_frame_fwd.clicked.connect(lambda: self.step_frame(1))
        frames.addWidget(self._btn_frame_fwd)

        self._btn_mark_in = QPushButton("In", self._frame_row)
        self._btn_mark_in.setProperty("kind", "soft")
        self._btn_mark_in.setToolTip("Отметить начало фрагмента (клавиша I)")
        self._btn_mark_in.clicked.connect(self.mark_in)
        frames.addWidget(self._btn_mark_in)

        self._btn_mark_out = QPushButton("Out", self._frame_row)
        self._btn_mark_out.setProperty("kind", "soft")
        self._btn_mark_out.setToolTip("Отметить конец фрагмента (клавиша O)")
        self._btn_mark_out.clicked.connect(self.mark_out)
        frames.addWidget(self._btn_mark_out)

        self._btn_play_range = QPushButton("Играть фрагмент", self._frame_row)
        self._btn_play_range.setProperty("kind", "soft")
        self._btn_play_range.setToolTip("Проиграть от in до out")
        self._btn_play_range.clicked.connect(self.play_marked_range)
        frames.addWidget(self._btn_play_range)

        self._btn_clear_marks = QPushButton("Сбросить", self._frame_row)
        self._btn_clear_marks.setProperty("kind", "soft")
        self._btn_clear_marks.setToolTip("Убрать отметки in/out")
        self._btn_clear_marks.clicked.connect(self.clear_marks)
        frames.addWidget(self._btn_clear_marks)

        frames.addStretch(1)

        self._btn_save_frame = QPushButton("Кадр PNG", self._frame_row)
        self._btn_save_frame.setProperty("kind", "soft")
        self._btn_save_frame.setToolTip("Сохранить текущий кадр в PNG")
        self._btn_save_frame.clicked.connect(self.request_save_frame)
        frames.addWidget(self._btn_save_frame)

        self._frame_row.setVisible(False)
        layout.addWidget(self._frame_row)
        # Клавиши (пробел, стрелки) обрабатывает сам плеер: ползунок с фокусом
        # перехватил бы стрелки и сдвигал бы позицию на 1 мс без перемотки.
        for control in (self._btn_play, self._slider, self._btn_mute, self._volume, self._btn_full):
            control.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for control in (self._btn_frame_back, self._btn_frame_fwd, self._btn_mark_in,
                        self._btn_mark_out, self._btn_play_range, self._btn_clear_marks,
                        self._btn_save_frame):
            control.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self._status = QLabel("", self)
        self._status.setProperty("muted", True)
        self._status.setWordWrap(True)
        status_row.addWidget(self._status, 1)
        # Скорость J/K/L (1x, 2x, 4x, 8x) — отдельной меткой, чтобы не затирать статус.
        self._speed = QLabel("", self)
        self._speed.setProperty("muted", True)
        self._speed.setVisible(False)
        status_row.addWidget(self._speed, 0, Qt.AlignmentFlag.AlignTop)
        self._btn_no_wait = QPushButton("Смотреть сразу", self)
        self._btn_no_wait.setProperty("kind", "soft")
        self._btn_no_wait.setToolTip(
            "Начать показ, не дожидаясь запаса. Видео тяжелее интернета, "
            "поэтому оно будет идти рывками, зато сразу видно, что в нём."
        )
        self._btn_no_wait.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_no_wait.clicked.connect(self._watch_now)
        self._btn_no_wait.setVisible(False)
        status_row.addWidget(self._btn_no_wait, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(status_row)

        self._sync_play_button()
        self._sync_mute_button()
        self._sync_fullscreen_button()
        self._show_page(self._message)
        try:
            from ..ui import theme as _theme

            _theme.manager().changed.connect(self._on_theme)
        except Exception:  # pragma: no cover
            pass

    def _on_theme(self, _palette) -> None:
        self._sync_play_button()
        self._sync_mute_button()
        self._sync_fullscreen_button()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return MULTIMEDIA_AVAILABLE

    def play(self, url: str, kind: str, title: str = "", autoplay: bool = True,
             buffer_probe=None, audio_url: str = "") -> None:
        """Видео или аудио с адреса url (сервер превью).

        buffer_probe(доля) -> BufferState | None — сколько файла уже скачано
        впереди позиции; по нему плеер буферизуется вместо рваного показа.
        """
        self.stop()
        if self._player is None:
            self.show_message(
                "Для превью нужен компонент QtMultimedia. "
                "Переустановите программу или посмотрите файл после скачивания."
            )
            return
        self._kind = kind
        self._url = url
        self._title = title or ""
        self._expect_video = kind == "video"
        if kind == "audio":
            self._audio_page.setText(f"<div style='font-size:56px'>♪</div><div>{_escape(title)}</div>")
            self._show_page(self._audio_page)
        else:
            self._show_page(self._video)
        self._controls.setVisible(True)
        self._btn_full.setVisible(kind == "video")
        # Покадровые кнопки и таймкод — только для видео (ТЗ 4.9).
        self._frame_row.setVisible(kind == "video")
        self._timecode.setVisible(kind == "video")
        self._fps = 0.0
        self._in_ms = None
        self._out_ms = None
        self._speed_index = 0
        self._speed.setVisible(False)
        self._slider.set_marks(None, None)
        self._set_status("Загрузка…")
        log.info("Превью: открываем %s (%s)", title or url, kind)
        self._buffer_probe = buffer_probe
        self._want_play = autoplay
        self._buffering = False
        self._no_wait = False
        self._first_frame = kind != "video"   # у звука ждать кадр не нужно
        if audio_url:
            # Видео без звука + звук отдельно: звук играет второй плеер,
            # он идёт за позицией видео (см. _sync_audio).
            if self._aplayer is None:
                self._aplayer = QMediaPlayer(self)
            self._player.setAudioOutput(None)
            self._aplayer.setAudioOutput(self._audio)
            self._aplayer.setSource(QUrl(audio_url))
            self._av_timer.start()
        self._player.setSource(QUrl(url))
        if autoplay:
            self._player.play()
        else:
            self._player.pause()  # покажет первый кадр, не запуская звук
        if buffer_probe is not None:
            self._buffer_timer.start()

    def show_image(self, url: str) -> None:
        """Картинку скачиваем целиком (они небольшие) и вписываем в окно."""
        self.stop()
        if self._network is None:
            self.show_message("Превью изображений недоступно в этой сборке.")
            return
        self._kind = "image"
        self._url = url
        self._image.clear_image()
        self._show_page(self._image)
        self._set_status("Загрузка изображения…")
        reply = self._network.get(QNetworkRequest(QUrl(url)))
        self._image_reply = reply
        reply.finished.connect(lambda: self._on_image_loaded(reply))

    def show_message(self, text: str) -> None:
        self.stop()
        self._message.setText(text)
        self._show_page(self._message)
        self._set_status("")

    def stop(self) -> None:
        """Останавливает всё и закрывает соединения с сервером превью."""
        self.exit_fullscreen()   # видео должно вернуться на место до остановки
        self._buffer_timer.stop()
        self._buffer_probe = None
        self._want_play = False
        self._buffering = False
        self._no_wait = False
        self._btn_no_wait.setVisible(False)
        self._stop_at = None
        self._av_timer.stop()
        if self._aplayer is not None:
            self._aplayer.stop()
            self._aplayer.setSource(QUrl())
            self._aplayer.setAudioOutput(None)
            if self._player is not None:
                self._player.setAudioOutput(self._audio)
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        if self._image_reply is not None:
            reply, self._image_reply = self._image_reply, None
            reply.abort()
            reply.deleteLater()
        self._kind = ""
        self._url = ""
        self._title = ""
        self._fps = 0.0
        self._slider.setRange(0, 0)
        self._time.setText("0:00 / 0:00")
        self._timecode.setText("")
        self._timecode.setVisible(False)
        self._frame_row.setVisible(False)
        self._speed.setVisible(False)
        self._in_ms = None
        self._out_ms = None
        self._speed_index = 0
        self._controls.setVisible(False)
        self._btn_full.setVisible(False)

    def clear(self) -> None:
        self.show_message(PLACEHOLDER)

    def pause(self) -> None:
        """Пауза по воле пользователя (или вкладку скрыли): сами не продолжим."""
        self._want_play = False
        if self._buffering:
            self._buffering = False
            self._btn_no_wait.setVisible(False)
            self._set_status("")
        if self._player is not None and self._is_playing():
            self._player.pause()
        self._sync_audio()
        self._sync_play_button()

    def toggle_play(self) -> None:
        if self._player is None or self._kind not in ("video", "audio"):
            return
        if self._want_play or self._is_playing():
            self.pause()
            return
        self._want_play = True
        if self._player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
            self._player.setPosition(0)
        self._player.play()
        self._check_buffer()  # запаса мало — сразу перейдём в буферизацию
        self._sync_play_button()

    @property
    def buffering(self) -> bool:
        return self._buffering

    # --- полный экран ---------------------------------------------------
    @property
    def is_fullscreen(self) -> bool:
        return self._fs is not None

    def toggle_fullscreen(self) -> None:
        if self._fs is not None:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self) -> None:
        """Видео — на весь экран, панель управления едет вместе с ним.

        Плеер и QVideoWidget те же самые — их только переносим в другое
        окно, поэтому воспроизведение, звук, буферизация и отметки I/O
        идут без перезапуска.
        """
        if self._fs is not None or self._video is None or self._kind != "video":
            return
        window = FullscreenWindow(self)
        window.leave_requested.connect(self.exit_fullscreen)
        window.activity.connect(self._fs_wake)

        # В обычном окне на время остаётся заглушка.
        self._fs_saved_message = self._message.text()
        self._message.setText("Видео открыто на весь экран.\nEsc или F — вернуться.")
        self._stack.removeWidget(self._video)
        self._show_page(self._message)
        self._layout.removeWidget(self._controls)
        window.box.addWidget(self._video, 1)
        window.bar_layout.addWidget(self._controls)
        window.box.addWidget(window.bar)
        self._video.show()
        self._controls.show()

        self._fs = window
        screen = self.screen()
        if screen is not None:
            window.setGeometry(screen.geometry())   # на том экране, где окно программы
        window.showFullScreen()
        window.activateWindow()
        window.setFocus()
        self._fs_last_pos = QCursor.pos()
        self._fs_idle_since = time.monotonic()
        self._fs_timer.start()
        self._sync_fullscreen_button()
        self._sync_mute_button()

    def exit_fullscreen(self, activate: bool = True) -> None:
        """Вернуть видео и панель в окно программы. activate=False — окно
        программы само прячется (в трей/свёрнуто), активировать его не нужно."""
        window, self._fs = self._fs, None
        if window is None:
            return
        self._fs_timer.stop()
        window.bar_layout.removeWidget(self._controls)
        window.box.removeWidget(self._video)
        self._layout.insertWidget(self._layout.indexOf(self._stack) + 1, self._controls)
        self._stack.addWidget(self._video)
        self._stack.setCurrentWidget(self._video)
        self._message.setText(self._fs_saved_message)
        self._video.show()
        self._controls.show()
        window.hide()
        window.deleteLater()
        self._sync_fullscreen_button()
        self._sync_mute_button()
        if activate:
            self.window().activateWindow()
            self.setFocus()

    def _fs_wake(self) -> None:
        """Показать панель и курсор (мышь двинули или нажали клавишу)."""
        window = self._fs
        if window is None:
            return
        self._fs_idle_since = time.monotonic()
        if not window.bar.isVisible():
            window.bar.show()
        window.unsetCursor()

    def _fs_tick(self) -> None:
        """Панель и курсор прячутся, когда мышь неподвижна, а видео играет."""
        window = self._fs
        if window is None:
            return
        pos = QCursor.pos()
        moved = pos != self._fs_last_pos
        self._fs_last_pos = pos
        bar_visible = window.bar.isVisible()
        over_bar = bar_visible and window.bar.geometry().contains(window.mapFromGlobal(pos))
        playing = self._want_play or self._is_playing()
        if moved or over_bar or self._slider.isSliderDown() or not playing:
            self._fs_wake()
        elif bar_visible and time.monotonic() - self._fs_idle_since >= FULLSCREEN_IDLE_S:
            window.bar.hide()
            window.setCursor(Qt.CursorShape.BlankCursor)

    def _sync_fullscreen_button(self) -> None:
        active = self._fs is not None
        if active:
            color = QColor("#f2f2f2")
        else:
            try:
                from ..ui import theme as _theme

                color = QColor(_theme.current().text)
            except Exception:  # pragma: no cover
                color = QColor("#ffffff")
        self._btn_full.setIcon(_fullscreen_icon(color, exit_mode=active))
        self._btn_full.setToolTip(
            "Выйти из полного экрана (Esc или F)" if active
            else "На весь экран (F или двойной клик по видео)"
        )

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._video and event.type() == QEvent.Type.MouseButtonDblClick:
            # Откладываем: видео переезжает в другое окно, а мы внутри его же события.
            QTimer.singleShot(0, self.toggle_fullscreen)
            return True   # встроенный полноэкранный режим QVideoWidget не нужен
        return super().eventFilter(obj, event)

    # --- позиция и фрагменты (вкладка «Соцсети») ------------------------
    def position_ms(self) -> int:
        return int(self._player.position()) if self._player is not None else 0

    def duration_ms(self) -> int:
        return int(self._player.duration()) if self._player is not None else 0

    def seek_to(self, ms: int) -> None:
        self._seek(max(0, int(ms)))
        self._on_position(max(0, int(ms)))

    def set_marks(self, start_ms: int | None, end_ms: int | None) -> None:
        self._slider.set_marks(start_ms, end_ms)

    def play_range(self, start_ms: int, end_ms: int) -> None:
        """Проиграть кусок и встать на паузу в его конце."""
        if self._player is None or self._kind not in ("video", "audio"):
            return
        self._stop_at = int(end_ms)
        self._seek(int(start_ms))
        if not (self._want_play or self._is_playing()):
            self.toggle_play()

    # --- таймкод, кадры, отметки (ТЗ 4.9) ------------------------------
    @property
    def fps(self) -> float:
        """Частота кадров видео; 0 — неизвестна."""
        return self._fps

    @property
    def title(self) -> str:
        return self._title

    @property
    def url(self) -> str:
        return self._url

    def read_fps(self) -> float:
        """Берёт частоту кадров из метаданных плеера (0 — неизвестна)."""
        if self._player is None:
            return 0.0
        value = None
        try:
            meta = self._player.metaData()
        except Exception:      # pragma: no cover - зависит от мультимедиа-бэкенда
            meta = None
        if meta is not None:
            for key in self._fps_meta_keys():
                try:
                    value = meta.value(key)
                except Exception:  # pragma: no cover
                    value = None
                if value is not None:
                    break
        fps = normalize_fps(value)
        self._fps = fps
        self._update_time(self.position_ms())
        return fps

    @staticmethod
    def _fps_meta_keys() -> list:
        """Ключи метаданных с частотой кадров (зависит от версии Qt)."""
        keys = []
        try:
            from PySide6.QtMultimedia import QMediaMetaData
            keys.append(QMediaMetaData.Key.VideoFrameRate)
        except Exception:      # pragma: no cover - старый Qt или нет Multimedia
            pass
        keys.append("VideoFrameRate")
        return keys

    def step_frame(self, delta: int) -> None:
        """Сдвиг на один кадр (delta = -1 назад, +1 вперёд)."""
        if self._kind != "video" or self._player is None:
            return
        self.pause()
        step = frame_step_ms(self._fps)
        target = self.position_ms() + int(delta) * step
        self._seek(max(0, target))

    def mark_in(self) -> None:
        """Отметка начала фрагмента."""
        if self._kind != "video":
            return
        self._in_ms = self.position_ms()
        self._update_marks()

    def mark_out(self) -> None:
        """Отметка конца фрагмента."""
        if self._kind != "video":
            return
        self._out_ms = self.position_ms()
        self._update_marks()

    def clear_marks(self) -> None:
        """Убирает отметки in/out."""
        self._in_ms = None
        self._out_ms = None
        self._slider.set_marks(None, None)
        self._update_marks()

    def play_marked_range(self) -> None:
        """Проигрывает фрагмент между in и out."""
        if self._in_ms is None or self._out_ms is None:
            self._set_status("Сначала отметьте начало (I) и конец (O) фрагмента.")
            return
        start, end = self._in_ms, self._out_ms
        if end <= start:
            self._set_status("Конец фрагмента должен быть позже начала.")
            return
        self.play_range(start, end)

    def _update_marks(self) -> None:
        """Обновляет полосу отметок и подпись о длительности фрагмента."""
        self._slider.set_marks(self._in_ms, self._out_ms)
        if self._in_ms is not None and self._out_ms is not None:
            length = max(0, self._out_ms - self._in_ms)
            self._set_status(f"Фрагмент: {format_timecode(length, self._fps)}")
        elif self._in_ms is not None:
            self._set_status(f"Начало фрагмента: {format_timecode(self._in_ms, self._fps)}")
        elif self._out_ms is not None:
            self._set_status(f"Конец фрагмента: {format_timecode(self._out_ms, self._fps)}")

    # --- J / K / L -------------------------------------------------------
    def jkl(self, key: str) -> None:
        """Клавиши монтажного плеера: J — назад на кадр, K — пауза, L — играть.

        Повторное нажатие L ускоряет видео: 1x → 2x → 4x → 8x.
        """
        if self._kind != "video":
            return
        if key == "j":
            self.step_frame(-1)
            return
        if key == "k":
            self.pause()
            self._set_speed(0)
            return
        if key == "l":
            if self._is_playing():
                self._set_speed(min(3, self._speed_index + 1))
            else:
                self._set_speed(0)
                if self._player is not None:
                    self._player.play()
                self._want_play = True

    def _set_speed(self, index: int) -> None:
        """Скорость воспроизведения: индекс 0 — 1x, 1 — 2x, 2 — 4x, 3 — 8x."""
        self._speed_index = max(0, min(3, int(index)))
        rates = (1.0, 2.0, 4.0, 8.0)
        rate = rates[self._speed_index]
        if self._player is not None:
            try:
                self._player.setPlaybackRate(rate)
            except Exception:      # pragma: no cover
                pass
        if self._speed_index == 0:
            self._speed.setVisible(False)
        else:
            self._speed.setText(f"{int(rate)}×")
            self._speed.setVisible(True)

    # --- экспорт кадра --------------------------------------------------
    def request_save_frame(self) -> None:
        """Просит сохранить текущий кадр (силами окна — там папка загрузок)."""
        image = None
        if self._video is not None:
            try:
                sink = self._video.videoSink()
                frame = sink.videoFrame() if sink is not None else None
                if frame is not None:
                    image = frame.toImage()
            except Exception as exc:      # pragma: no cover
                log.info("Кадр получить не удалось: %s", exc)
                image = None
        self.frame_save_requested.emit(image, self.position_ms())

    def _sync_audio(self) -> None:
        """Видео и отдельный звук играют вместе.

        Звук — главные часы: во время игры его не перематываем (иначе он на
        миг пропадает), а видео догоняет его чуть быстрее/медленнее.
        Старт и продолжение после перемотки: сначала звук (ему нужно время,
        чтобы зазвучать), видео уже стоит на нужном кадре и трогается, как
        только звук пошёл. Если одна дорожка ждёт данные — вторая на паузе.
        BufferingMedia — это «играет, буфер неполный», ждать из-за него не нужно.
        """
        audio, video = self._aplayer, self._player
        if audio is None or video is None or audio.source().isEmpty():
            return
        S = QMediaPlayer.MediaStatus
        playing = QMediaPlayer.PlaybackState.PlayingState
        waiting = (S.StalledMedia, S.LoadingMedia)
        if not self._want_play:
            self._av_start = None
            if audio.playbackState() == playing:
                audio.pause()
            if video.playbackRate() != 1.0:
                video.setPlaybackRate(1.0)
            return
        if video.mediaStatus() == S.EndOfMedia:
            return
        if audio.mediaStatus() == S.EndOfMedia:      # звук короче видео — пусть видео доиграет
            if video.playbackState() != playing:
                video.play()
            return
        video_waits = video.mediaStatus() in waiting
        audio_waits = audio.mediaStatus() in waiting
        if video_waits or audio_waits:
            self._av_start = None
            if not video_waits and video.playbackState() == playing:
                video.pause()
            if not audio_waits and audio.playbackState() == playing:
                audio.pause()
            return
        if audio.playbackState() != playing:
            # Старт: видео ждёт на месте, звук запускаем с позиции видео —
            # но только когда видео уже показало кадр на этом месте (после
            # перемотки ему нужно время), иначе звук убежит вперёд.
            if video.playbackState() == playing:
                video.pause()
            if not self._av_frame and time.monotonic() - self._av_wait_since < AV_START_TIMEOUT_S:
                return
            if abs(audio.position() - video.position()) > AV_DRIFT_MS:
                audio.setPosition(video.position())
            audio.play()
            self._av_start = (audio.position(), time.monotonic())
            return
        if self._av_start is not None:
            first, since = self._av_start
            if audio.position() == first and time.monotonic() - since < AV_START_TIMEOUT_S:
                if video.playbackState() == playing:
                    video.pause()
                return                               # звук ещё не зазвучал
            self._av_start = None
            video.setPlaybackRate(1.0)
            video.play()
            return
        if video.playbackState() != playing:
            video.play()
        drift = video.position() - audio.position()  # > 0 — видео впереди
        rate = 1.0
        if abs(drift) > AV_JUMP_MS:
            video.setPosition(audio.position())
        elif abs(drift) > AV_DRIFT_MS:
            rate = 1.0 - max(-AV_MAX_RATE_SHIFT, min(AV_MAX_RATE_SHIFT, drift / 2000.0))
        if abs(video.playbackRate() - rate) > 0.005:
            video.setPlaybackRate(rate)

    # ------------------------------------------------------------------
    # Буферизация
    # ------------------------------------------------------------------
    def _check_buffer(self) -> None:
        """Не даём видео играть «впритык» к скачанному.

        Если впереди меньше LOW_WATER_S секунд — пауза и статус «Буферизация».
        Продолжаем, когда запаса хватит: при быстром интернете — на
        FAST_LINK_RESUME_S секунд, при медленном — столько, чтобы досмотреть
        ближайшие SLOW_LINK_HORIZON_S секунд без остановок (видео 100 Мбит/с
        при интернете 25 Мбит/с играет ровно, а не по 3 кадра в секунду).
        """
        probe, player = self._buffer_probe, self._player
        if probe is None or player is None or self._kind not in ("video", "audio"):
            return
        if self._no_wait:
            return
        duration = player.duration()
        if duration <= 0:
            return
        position = player.position()
        try:
            state = probe(position / duration)
        except Exception:
            log.debug("Состояние буфера недоступно", exc_info=True)
            return
        if state is None or state.error:
            return  # ошибку покажет сам плеер

        media_rate = state.size / (duration / 1000.0)        # байт на секунду видео
        remaining_s = max(0.0, (duration - position) / 1000.0)
        ahead_s = state.ahead_bytes / media_rate if media_rate else 0.0
        if state.complete:
            need_s = 0.0
        elif state.rate >= media_rate * 1.15:
            need_s = FAST_LINK_RESUME_S
        else:
            share = state.rate / media_rate if media_rate else 0.0
            need_s = (1.0 - share) * min(remaining_s, SLOW_LINK_HORIZON_S) + 2.0
        need_s = min(need_s, remaining_s)

        if self._buffering:
            if state.complete or ahead_s >= need_s:
                self._buffering = False
                self._btn_no_wait.setVisible(False)
                self._set_status("")
                if self._want_play:
                    player.play()
            else:
                self._set_status(_buffer_text(ahead_s, need_s, media_rate, state.rate))
                self._offer_no_wait(ahead_s, need_s, media_rate, state.rate)
        elif (
            self._want_play
            # Сначала показываем первый кадр — иначе всё ожидание был бы чёрный экран.
            and self._first_frame
            and not state.complete
            and ahead_s < LOW_WATER_S
            and ahead_s < remaining_s
        ):
            self._buffering = True
            if self._is_playing():
                player.pause()
            self._set_status(_buffer_text(ahead_s, need_s, media_rate, state.rate))
            self._offer_no_wait(ahead_s, need_s, media_rate, state.rate)
        self._sync_play_button()

    def _offer_no_wait(self, ahead_s: float, need_s: float, media_rate: float, rate: float) -> None:
        """Ждать долго — даём выбор: смотреть сразу, пусть и рывками."""
        wait_s = (need_s - ahead_s) * media_rate / rate if rate > 0 else float("inf")
        self._btn_no_wait.setVisible(wait_s > OFFER_NO_WAIT_S)

    def _watch_now(self) -> None:
        self._no_wait = True
        self._buffering = False
        self._btn_no_wait.setVisible(False)
        self._set_status("Показ без буфера: видео тяжелее интернета, поэтому идёт рывками. "
                         "Файл продолжает загружаться — повторный просмотр будет плавным.")
        self._want_play = True
        if self._player is not None:
            self._player.play()
        self._sync_play_button()

    def _on_frame(self, _frame) -> None:
        self._av_frame = True                # видео показало кадр — может трогаться
        if not self._first_frame:
            self._first_frame = True
            self._check_buffer()  # кадр показан — теперь можно копить запас

    # ------------------------------------------------------------------
    # Обработчики плеера
    # ------------------------------------------------------------------
    def _is_playing(self) -> bool:
        return (
            self._player is not None
            and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )

    def _on_duration(self, duration: int) -> None:
        self._slider.setRange(0, max(0, duration))
        self._update_time(self._player.position() if self._player else 0)

    def _on_position(self, position: int) -> None:
        if self._stop_at is not None and position >= self._stop_at:
            self._stop_at = None
            self.pause()
        if not self._slider.isSliderDown():
            self._slider.setValue(position)
            self._update_time(position)

    def _preview_time(self, value: int) -> None:
        self._update_time(value)

    def _update_time(self, position: int) -> None:
        duration = self._player.duration() if self._player else 0
        self._time.setText(f"{format_ms(position)} / {format_ms(duration)}")
        if self._kind == "video":
            # Таймкод рядом с длительностью: с кадрами, если знаем FPS.
            self._timecode.setText(format_timecode(position, self._fps))
            if self._fps > 0:
                self._timecode.setToolTip(f"Частота кадров: {self._fps:g} fps")
            else:
                self._timecode.setToolTip("FPS не определён — таймкод с миллисекундами")

    def _seek(self, value: int) -> None:
        if self._player is None or not self._player.isSeekable():
            return
        # На паузе (и во время буферизации) плеер сам покажет кадр в новом
        # месте, как только придут данные; буферизация продолжится оттуда.
        self._player.setPosition(int(value))
        if self._aplayer is not None and not self._aplayer.source().isEmpty():
            self._aplayer.pause()            # дальше — заново «старт» в _sync_audio
            self._aplayer.setPosition(int(value))
            self._av_start = None
            self._av_frame = False
            self._av_wait_since = time.monotonic()

    def _on_media_status(self, status) -> None:
        S = QMediaPlayer.MediaStatus
        if status == S.LoadingMedia:
            self._set_status("Загрузка…")
        elif status == S.StalledMedia:
            self._set_status("Ждём данные из облака…")
        elif status in (S.LoadedMedia, S.BufferedMedia, S.BufferingMedia):
            if not self._buffering:
                self._set_status("")
            if self._kind == "video":
                # Метаданные с FPS могли прийти раньше — читаем ещё раз.
                self.read_fps()
            if self._expect_video and status == S.LoadedMedia and self._player is not None \
                    and not self._player.hasVideo():
                self._set_status("В файле не нашлось видео, которое умеет показать плеер — играет только звук.")
        elif status == S.EndOfMedia:
            self._want_play = False
            self._buffering = False
            self._set_status("")
        self._sync_play_button()

    def _on_error(self, error, text: str) -> None:
        E = QMediaPlayer.Error
        if error == E.NoError or not self._url:
            return
        messages = {
            E.FormatError: "Плеер не смог открыть этот формат.",
            E.ResourceError: "Не удалось получить файл для превью. Проверьте интернет и попробуйте ещё раз.",
            E.NetworkError: "Связь прервалась при загрузке превью. Попробуйте ещё раз.",
            E.AccessDeniedError: "Нет доступа к файлу.",
        }
        message = messages.get(error, "Не удалось воспроизвести файл.")
        log.warning("Плеер превью: %s — %s (%s)", error.name, text, self._url)
        self._set_status(message, error=True)
        self._sync_play_button()
        self.failed.emit(message, error == E.FormatError)

    def _on_image_loaded(self, reply) -> None:
        if reply is not self._image_reply:
            reply.deleteLater()  # ответ на уже неактуальный запрос
            return
        self._image_reply = None
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                log.warning("Превью изображения: %s (%s)", reply.errorString(), self._url)
                self._set_status("Не удалось получить изображение из облака.", error=True)
                self.failed.emit("Не удалось получить изображение из облака.", False)
                return
            image, problem = decode_image(reply.readAll())
            if image is None:
                log.warning("Изображение не открылось: %s (%s)", problem, self._url)
                self._set_status("Этот формат изображения нельзя показать. Скачайте файл.", error=True)
                self.failed.emit("Этот формат изображения нельзя показать.", True)
                return
            self._image.set_image(image)
            self._set_status("")
        finally:
            reply.deleteLater()

    # ------------------------------------------------------------------
    # Звук
    # ------------------------------------------------------------------
    def _on_volume(self, value: int) -> None:
        if self._audio is not None:
            self._audio.setVolume(value / 100.0)
            if value and self._audio.isMuted():
                self._audio.setMuted(False)
        self._sync_mute_button()

    def _toggle_mute(self) -> None:
        if self._audio is not None:
            self._audio.setMuted(not self._audio.isMuted())
        self._sync_mute_button()

    def _sync_mute_button(self) -> None:
        from ..ui import theme as _theme

        muted = self._audio is None or self._audio.isMuted() or self._volume.value() == 0
        name = "mute" if muted else "volume"
        if self._fs is not None:   # панель тёмная — значок светлый
            try:
                self._btn_mute.setIcon(_theme.icon(name, "#f2f2f2", 20))
                return
            except Exception:  # pragma: no cover
                pass
        self._btn_mute.setIcon(_theme.icon(name, None, 20))

    def _sync_play_button(self, *_args) -> None:
        # Во время буферизации видео «как бы играет» — показываем паузу,
        # чтобы было понятно: оно продолжит само.
        from ..ui import theme as _theme

        playing = self._is_playing() or ((self._buffering or self._av_timer.isActive()) and self._want_play)
        self._btn_play.setIcon(_theme.icon("pause" if playing else "play", _theme.current().pri_text, 18))

    # ------------------------------------------------------------------
    def _show_page(self, page: QWidget | None) -> None:
        if page is not None:
            self._stack.setCurrentWidget(page)

    def _set_status(self, text: str, error: bool = False) -> None:
        self._status.setText(text)
        self._status.setProperty("error", error)
        self._status.setProperty("muted", not error)
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle_play()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self._player is not None:
            size = FINE_STEP_MS if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else SEEK_STEP_MS
            step = size if key == Qt.Key.Key_Right else -size
            target = max(0, min(self._player.duration(), self._player.position() + step))
            self._seek(target)
        elif self._kind == "video" and _is_letter_key(event, Qt.Key.Key_F, 0x46):
            self.toggle_fullscreen()
        elif self._kind == "video" and key == Qt.Key.Key_Comma:
            self.step_frame(-1)
        elif self._kind == "video" and key == Qt.Key.Key_Period:
            self.step_frame(1)
        elif self._kind == "video" and _is_letter_key(event, Qt.Key.Key_J, 0x4A):
            self.jkl("j")
        elif self._kind == "video" and _is_letter_key(event, Qt.Key.Key_K, 0x4B):
            self.jkl("k")
        elif self._kind == "video" and _is_letter_key(event, Qt.Key.Key_L, 0x4C):
            self.jkl("l")
        elif self._kind == "video" and _is_letter_key(event, Qt.Key.Key_I, 0x49):
            self.mark_in()
        elif self._kind == "video" and _is_letter_key(event, Qt.Key.Key_O, 0x4F):
            self.mark_out()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus()
        super().mousePressEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        # Ушли на вкладку «Очередь» или свернули окно — звук не должен играть «в фоне».
        self.exit_fullscreen(activate=False)
        self.pause()
        super().hideEvent(event)


def _buffer_text(ahead_s: float, need_s: float, media_rate: float, rate: float) -> str:
    percent = int(100 * ahead_s / need_s) if need_s > 0 else 100
    text = f"Буферизация {max(0, min(99, percent))}%"
    if rate > 0:
        eta = max(0.0, (need_s - ahead_s) * media_rate / rate)
        text += f" · ещё ~{format_ms(int(eta * 1000) + 999)}"
        if rate < media_rate:
            text += (
                f" · видео {media_rate * 8 / 1e6:.0f} Мбит/с, интернет "
                f"~{rate * 8 / 1e6:.0f} Мбит/с — поэтому сначала копим запас"
            )
    else:
        text += " · ждём данные из облака…"
    return text


def decode_image(data: QByteArray) -> tuple[QImage | None, str]:
    """Декодирует изображение; крупное сразу уменьшает (экономия памяти)."""
    buffer = QBuffer()
    buffer.setData(data)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)  # поворот по EXIF, как снимала камера
    size = reader.size()
    if size.isValid() and max(size.width(), size.height()) > IMAGE_MAX_SIDE:
        reader.setScaledSize(size.scaled(IMAGE_MAX_SIDE, IMAGE_MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return None, reader.errorString()
    return image, ""


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
