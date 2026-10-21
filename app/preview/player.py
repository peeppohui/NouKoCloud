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

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (
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
        self._av_timer = QTimer(self)
        self._av_timer.setInterval(100)
        self._av_start: tuple[int, float] | None = None  # старт: (позиция звука, когда запустили)
        self._av_frame = False               # после перемотки видео уже показало кадр
        self._av_wait_since = 0.0
        self._av_timer.timeout.connect(self._sync_audio)
        self._buffer_timer = QTimer(self)
        self._buffer_timer.setInterval(BUFFER_CHECK_MS)
        self._buffer_timer.timeout.connect(self._check_buffer)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

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
        layout.addWidget(self._controls)
        # Клавиши (пробел, стрелки) обрабатывает сам плеер: ползунок с фокусом
        # перехватил бы стрелки и сдвигал бы позицию на 1 мс без перемотки.
        for control in (self._btn_play, self._slider, self._btn_mute, self._volume):
            control.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self._status = QLabel("", self)
        self._status.setProperty("muted", True)
        self._status.setWordWrap(True)
        status_row.addWidget(self._status, 1)
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
        self._show_page(self._message)
        try:
            from ..ui import theme as _theme

            _theme.manager().changed.connect(self._on_theme)
        except Exception:  # pragma: no cover
            pass

    def _on_theme(self, _palette) -> None:
        self._sync_play_button()
        self._sync_mute_button()

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
        self._expect_video = kind == "video"
        if kind == "audio":
            self._audio_page.setText(f"<div style='font-size:56px'>♪</div><div>{_escape(title)}</div>")
            self._show_page(self._audio_page)
        else:
            self._show_page(self._video)
        self._controls.setVisible(True)
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
        self._slider.setRange(0, 0)
        self._time.setText("0:00 / 0:00")
        self._controls.setVisible(False)

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
        self._btn_mute.setIcon(_theme.icon("mute" if muted else "volume", None, 20))

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
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus()
        super().mousePressEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        # Ушли на вкладку «Очередь» или свернули окно — звук не должен играть «в фоне».
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
