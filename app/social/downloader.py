"""Скачивание видео и звука по ссылке из соцсетей — через yt-dlp.

yt-dlp знает сотни сайтов: YouTube, VK, Rutube, Дзен, OK, TikTok, Instagram,
X (Twitter), Vimeo, Twitch и другие. Видео и звук там часто лежат отдельно —
yt-dlp склеивает их с помощью ffmpeg (bin/ffmpeg.exe рядом с программой).

Модуль без Qt: блокирующие функции, которые интерфейс запускает в своём
потоке. Прогресс — через колбэк, отмена — через threading.Event.

Скорость. YouTube (и не только) ограничивает каждое соединение — из России
это бывает ~70 КБ/с. Поэтому файл качается частями по PIECE байт во много
соединений одновременно: части тянутся параллельно и пишутся каждая на своё
место (_dl_pieces). Замер на YouTube: 1 соединение — 0,06 МБ/с, 16 — 1 МБ/с,
64 — 5 МБ/с.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..core.models import PARTS_DIR
from ..core.paths import bundled_ffmpeg, hide_path

log = logging.getLogger(__name__)

#: Варианты качества видео (высота кадра); 0 — лучшее доступное.
QUALITIES = (0, 2160, 1440, 1080, 720, 480, 360)
AUDIO_FORMATS = ("mp3", "m4a", "wav")
#: Сколько соединений на один файл и размер одной части.
CONNECTIONS = 32
#: YouTube режет каждое соединение сильнее всех — ему больше.
YOUTUBE_CONNECTIONS = 64
PIECE = 1024 * 1024


class SocialError(Exception):
    """Ошибка с понятным человеку текстом."""


class Cancelled(BaseException):
    """Отмена пользователем.

    BaseException, а не Exception: yt-dlp ловит Exception (повторы, пропуск
    частей) и иначе продолжал бы качать ещё долго после отмены.
    """


@dataclass
class MediaInfo:
    url: str
    title: str
    uploader: str = ""
    site: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    heights: list[int] = field(default_factory=list)   # доступные высоты видео
    has_video: bool = True
    raw: dict = field(default_factory=dict, repr=False)  # ответ yt-dlp (для предпросмотра)

    @property
    def duration_text(self) -> str:
        total = int(self.duration or 0)
        if total <= 0:
            return ""
        h, rest = divmod(total, 3600)
        m, s = divmod(rest, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass
class Progress:
    percent: float = 0.0           # 0..100, -1 — неизвестно
    speed: float = 0.0             # байт/с
    eta: float = 0.0               # секунд
    stage: str = "download"        # download | process


def ffmpeg_path() -> Optional[str]:
    bundled = bundled_ffmpeg()
    if bundled is not None:
        return str(bundled)
    return shutil.which("ffmpeg")


# ----------------------------------------------------------------------
# Понятные ошибки
# ----------------------------------------------------------------------
_ERRORS = (
    (r"Unsupported URL|is not a valid URL|no suitable extractor",
     "Этот сайт или тип ссылки не поддерживается. Проверьте, что это ссылка на видео или пост."),
    (r"Private video|This video is private|private",
     "Видео закрыто владельцем (приватное) — скачать его нельзя."),
    (r"Sign in|login required|log in|confirm you.re not a bot|cookies",
     "Сайт требует войти в аккаунт, чтобы показать это видео. Такие видео программа "
     "скачать не может — попробуйте открытую ссылку."),
    (r"not available in your country|geo.?restrict|blocked it in your country",
     "Видео недоступно в вашей стране."),
    (r"Video unavailable|has been removed|does not exist|404",
     "Видео не найдено: его удалили или ссылка неверная."),
    (r"members.only|Join this channel|premium",
     "Видео только для подписчиков или платное — скачать его нельзя."),
    (r"DRM|protected",
     "Видео защищено от копирования (DRM) — скачать его нельзя."),
    (r"ffmpeg not found|Unable to find ffmpeg|ffprobe not found|ffmpeg.*not found",
     "Не найден ffmpeg — он нужен, чтобы склеить видео со звуком. "
     "Переустановите программу (ffmpeg лежит в папке bin рядом с ней)."),
    (r"timed out|Connection|Network|getaddrinfo|Temporary failure",
     "Нет связи с сайтом. Проверьте интернет и попробуйте ещё раз."),
    (r"Requested format is not available",
     "Такого качества у этого видео нет — выберите другое."),
)


def friendly(exc: BaseException) -> str:
    # SocialError уже несёт человеческий текст — не заменяем его общим
    # «не удалось скачать»: пользователь должен видеть причину.
    if isinstance(exc, SocialError):
        return str(exc)
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))   # убираем цветовые коды yt-dlp
    for pattern, message in _ERRORS:
        if re.search(pattern, text, re.I):
            return message
    return "Не удалось скачать по этой ссылке. Подробности — в журнале."


# ----------------------------------------------------------------------
# Сведения о видео
# ----------------------------------------------------------------------
def _base_options() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,          # ссылка на видео в плейлисте — только это видео
        "windowsfilenames": True,
        "logger": _YtLogger(),
        "socket_timeout": 20,
        "retries": 5,
        "fragment_retries": 10,
        # Пропавшая часть — это битый файл: лучше повторить или упасть.
        "skip_unavailable_fragments": False,
        "concurrent_fragment_downloads": CONNECTIONS,
        # Читаем небольшими блоками: после каждого блока вызывается хук, и
        # отмена срабатывает за доли секунды даже на медленном соединении.
        "buffersize": 64 * 1024,
        "noresizebuffer": True,
    }


def split_ranges(size: int, piece: int = 0) -> list[tuple[int, int]]:
    """Диапазоны частей файла: [(начало, конец), …] — без дыр и перекрытий."""
    piece = piece or PIECE
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < size:
        end = min(start + piece, size) - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def _remove(path: str) -> None:
    """Убирает временный файл (мог остаться после обрыва)."""
    try:
        os.remove(path)
    except OSError:
        pass


def _download_ranges(url: str, headers: dict, size: int, piece: int, target: str,
                     connections: int, hooks: list, cancel: threading.Event | None) -> None:
    """Качает файл частями по Range, каждая часть пишется на своё место.

    Части тянутся в нескольких потоках, порядок не важен — пишем по
    смещениям. Сервер без поддержки Range отдаёт 200 вместо 206: тогда
    выбрасываем исключение, и вызывающий качает файл обычным способом.
    """
    import httpx

    ranges = split_ranges(size, piece)
    # Файл заранее растягиваем на весь размер — части пишут в свои места.
    with open(target, "wb") as handle:
        handle.truncate(size)

    lock = threading.Lock()
    state = {"done": 0, "started": time.monotonic(), "speed": 0.0}
    errors: list[BaseException] = []

    def report() -> None:
        now = time.monotonic()
        with lock:
            done = state["done"]
            elapsed = now - state["started"]
            if elapsed > 0:
                current = done / elapsed
                state["speed"] = current if not state["speed"] else 0.7 * state["speed"] + 0.3 * current
            speed = state["speed"]
        status = {
            "status": "downloading",
            "downloaded_bytes": done,
            "total_bytes": size,
            "speed": speed,
            "eta": (size - done) / speed if speed > 0 else None,
        }
        for hook in hooks:
            try:
                hook(status)
            except BaseException:
                return          # отмена: хук бросает Cancelled — просто выходим

    def worker(rng: tuple[int, int]) -> None:
        begin, end = rng
        want = end - begin + 1
        got = 0
        try:
            request = {**headers, "Range": f"bytes={begin}-{end}"}
            with httpx.Client(follow_redirects=True, timeout=30.0) as client:
                with client.stream("GET", url, headers=request) as response:
                    if response.status_code != 206:
                        raise IOError(f"сервер не поддержал диапазон (код {response.status_code})")
                    offset = begin
                    with open(target, "r+b") as handle:
                        for chunk in response.iter_bytes(256 * 1024):
                            if cancel is not None and cancel.is_set():
                                raise Cancelled()
                            handle.seek(offset)
                            handle.write(chunk)
                            offset += len(chunk)
                            got += len(chunk)
                            with lock:
                                state["done"] += len(chunk)
                            report()
            if got != want:
                # Часть пришла короче — в файле останется дыра из нулей.
                raise IOError(f"часть {begin}-{end} пришла на {got} байт вместо {want}")
        except BaseException as exc:
            with lock:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=max(1, connections)) as pool:
        list(pool.map(worker, ranges))
    if errors:
        raise errors[0]
    report()


def _yt_class():
    import yt_dlp
    from yt_dlp.networking import Request

    class _FastYDL(yt_dlp.YoutubeDL):
        """YoutubeDL, который качает обычные файлы в несколько соединений."""

        cancel: threading.Event | None = None

        def urlopen(self, req):
            # Запросы при разборе ссылки: после отмены новых не начинаем.
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled()
            return super().urlopen(req)

        def dl(self, name, info, subtitle=False, test=False):
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled()
            if not subtitle and not test and (info.get("section_start") or info.get("section_end")):
                return self._dl_section(name, info)
            if (not subtitle and not test and info.get("protocol") in ("http", "https")
                    and not info.get("is_live")):
                size = info.get("filesize") or self._content_length(info)
                if size and size > PIECE * 2:
                    log.debug("Соцсети: формат %s качаем частями (%d МБ)", info.get("format_id"), size >> 20)
                    return self._dl_pieces(name, info, int(size))
            return super().dl(name, info, subtitle, test)

        def _dl_pieces(self, name, info, size: int):
            """Обычный файл — в несколько соединений, части пишутся по местам.

            Раньше здесь был обман: файл превращался в «нарезанный» (DASH),
            но yt-dlp про Range в таких частях не знает и тянул ЦЕЛИКОМ каждую
            часть. Ролик на 2 ГБ качался 2000 раз — и не заканчивался никогда.
            Теперь части тянутся по Range и пишутся на свои места.
            """
            url = info.get("url")
            headers = info.get("http_headers") or {}
            many = YOUTUBE_CONNECTIONS if "youtube" in str(info.get("extractor_key", "")).lower() else CONNECTIONS
            part = f"{name}.part"
            try:
                _download_ranges(url, headers, size, PIECE, part, many,
                                 self._progress_hooks, self.cancel)
            except Cancelled:
                _remove(part)
                raise
            except Exception as exc:
                _remove(part)
                log.debug("Соцсети: частями не вышло (%s) — качаем одним потоком", exc)
                return super().dl(name, info)
            os.replace(part, name)
            # yt-dlp ждёт пару (успех, real_download) — как у его загрузчиков.
            return True, True

        def _dl_section(self, name, info):
            """Фрагмент: его вырезает ffmpeg, а качает посредник во много соединений."""
            from .range_proxy import RangeProxy

            def fetch(url: str, headers: dict, start: int, end: int) -> bytes:
                req = Request(url, headers={**headers, "Range": f"bytes={start}-{end}"})
                with self.urlopen(req) as resp:
                    return resp.read()

            proxy = RangeProxy(fetch)
            try:
                info = dict(info)
                formats = [dict(f) for f in (info.get("requested_formats") or [])]
                for fmt in formats or [info]:
                    if fmt.get("protocol") not in ("http", "https"):
                        continue             # потоки m3u8 и т. п. ffmpeg читает сам
                    headers = fmt.get("http_headers") or info.get("http_headers") or {}
                    size = fmt.get("filesize") or self._content_length({**fmt, "http_headers": headers})
                    if size:
                        fmt["url"] = proxy.add(fmt["url"], headers, int(size))
                if formats:
                    info["requested_formats"] = formats
                log.debug("Соцсети: фрагмент %s–%s через посредник", info.get("section_start"),
                          info.get("section_end"))
                # ffmpeg прогресс не сообщает — считаем по тому, что отдал посредник.
                length = float(info.get("section_end") or 0) - float(info.get("section_start") or 0)
                expected = sum(float(f.get("tbr") or 0) * 125 * length for f in formats or [info])
                done = threading.Event()
                ticker = threading.Thread(target=self._report_section, args=(proxy, expected, done),
                                          name="social-section", daemon=True)
                ticker.start()
                try:
                    return super().dl(name, info)
                finally:
                    done.set()
            finally:
                proxy.close()

        def _report_section(self, proxy, expected: float, done: threading.Event) -> None:
            last_bytes, last_time, speed = 0, time.monotonic(), 0.0
            while not done.wait(0.5):
                now = time.monotonic()
                got = proxy.served
                if now > last_time:
                    current = (got - last_bytes) / (now - last_time)
                    speed = current if not speed else 0.7 * speed + 0.3 * current
                last_bytes, last_time = got, now
                status = {"status": "downloading", "downloaded_bytes": got, "speed": speed,
                          "total_bytes_estimate": max(expected, got * 1.02) if expected else None}
                for hook in self._progress_hooks:
                    try:
                        hook(status)
                    except BaseException:
                        return               # отмена: хук бросает Cancelled — просто выходим

        def _content_length(self, info: dict) -> int:
            try:
                req = Request(info["url"], headers=info.get("http_headers") or {}, method="HEAD")
                with self.urlopen(req) as resp:
                    if resp.headers.get("Accept-Ranges", "").lower() == "none":
                        return 0
                    return int(resp.headers.get("Content-Length") or 0)
            except Exception as exc:
                log.debug("Соцсети: размер не узнать (%s) — качаем одним потоком", exc)
                return 0

    return _FastYDL


# ----------------------------------------------------------------------
# Отмена: ffmpeg (склейка, конвертация) прерываем сразу
# ----------------------------------------------------------------------
_procs_lock = threading.Lock()
_procs: dict[int, list] = {}     # поток загрузки -> запущенные им процессы
_patched = False


def _track_processes() -> None:
    """Запоминает процессы, которые запускает yt-dlp (ffmpeg), — по потоку.

    И не даёт начинать новые части после отмены: yt-dlp ставит в очередь
    сразу все части файла и после ошибки одной всё равно открывал бы
    соединения под остальные.
    """
    global _patched
    if _patched:
        return
    from yt_dlp.downloader.fragment import FragmentFD
    from yt_dlp.utils import _utils

    download_fragment = FragmentFD._download_fragment

    def guarded_fragment(self, ctx, *args, **kwargs):
        cancel = getattr(self.ydl, "cancel", None)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        return download_fragment(self, ctx, *args, **kwargs)

    FragmentFD._download_fragment = guarded_fragment

    original = _utils.Popen.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        with _procs_lock:
            _procs.setdefault(threading.get_ident(), []).append(self)

    _utils.Popen.__init__ = init
    _patched = True


def _kill_processes(thread_id: int) -> None:
    with _procs_lock:
        procs = list(_procs.get(thread_id, []))
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


class _YtLogger:
    """Сообщения yt-dlp — в наш журнал (а не в консоль, которой у .exe нет)."""

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.info("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)


def probe(url: str) -> MediaInfo:
    """Что лежит по ссылке: название, автор, длительность, доступные качества."""
    import yt_dlp

    url = url.strip()
    if not url:
        raise SocialError("Вставьте ссылку на видео.")
    try:
        with yt_dlp.YoutubeDL({**_base_options(), "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        log.warning("Соцсети: не удалось прочитать ссылку %s: %s", url, exc, exc_info=True)
        raise SocialError(friendly(exc)) from exc
    if info is None:
        raise SocialError("По ссылке ничего не нашлось.")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise SocialError("По ссылке пустой список видео.")
        info = entries[0]
    return info_from_dict(url, info)


def info_from_dict(url: str, info: dict) -> MediaInfo:
    formats = info.get("formats") or []
    heights = sorted({int(f["height"]) for f in formats
                      if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
    has_video = bool(heights) or (info.get("vcodec") not in (None, "none") and bool(info.get("height")))
    if not heights and info.get("height"):
        heights = [int(info["height"])]
    return MediaInfo(
        url=info.get("webpage_url") or url,
        title=info.get("title") or "Без названия",
        uploader=info.get("uploader") or info.get("channel") or "",
        site=info.get("extractor_key") or info.get("extractor") or "",
        duration=float(info.get("duration") or 0),
        thumbnail=info.get("thumbnail") or "",
        heights=heights,
        has_video=has_video,
        raw=info,
    )


def quality_choices(info: MediaInfo) -> list[int]:
    """Качества для списка: «лучшее» и только те высоты, что реально есть."""
    if not info.heights:
        return [0]
    # Только качества, которые у видео реально есть (±10% — у вертикальных
    # и нестандартных роликов высота бывает 1072, 1088 и т. п.).
    return [0] + [h for h in QUALITIES[1:] if any(abs(x - h) <= h * 0.1 for x in info.heights)]


# ----------------------------------------------------------------------
# Скачивание
# ----------------------------------------------------------------------
def clip_label(start: float, end: float) -> str:
    """«02-05» — метки времени в имени файла (двоеточие в Windows нельзя).

    Без ведущих нулей минут: «0.02–0.05» читалось бы как две десятых
    секунды, а не как две и пять секунд.
    """

    def part(seconds: float) -> str:
        tenths = int(round(max(0.0, seconds) * 10))
        total = tenths // 10
        h, rest = divmod(total, 3600)
        m, s = divmod(rest, 60)
        return f"{h}-{m:02d}-{s:02d}" if h else f"{m:02d}-{s:02d}"

    return f"{part(start)}–{part(end)}"


def format_time(seconds: float, precise: bool = False) -> str:
    """1:05 (или 1:05.4 с десятыми, если precise и они есть), 1:02:05 для часов."""
    tenths = int(round(max(0.0, seconds) * 10))
    total, frac = divmod(tenths, 10)
    if not precise:
        total, frac = int(round(max(0.0, seconds))), 0
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    text = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return f"{text}.{frac}" if frac else text


def parse_time(text: str) -> float:
    """«90», «1:30», «1:02:30», «1:30.5» -> секунды. ValueError, если не время."""
    text = text.strip().replace(",", ".")
    if not text:
        raise ValueError("пусто")
    parts = text.split(":")
    if len(parts) > 3 or any(not p.strip() for p in parts):
        raise ValueError(text)
    value = 0.0
    for part in parts:
        number = float(part)
        if number < 0:
            raise ValueError(text)
        value = value * 60 + number
    if len(parts) > 1 and any(float(p) >= 60 for p in parts[1:]):
        raise ValueError(text)
    return value


def build_options(dest: Path, mode: str, height: int = 0, audio_format: str = "mp3",
                  for_editing: bool = True, section: tuple[float, float] | None = None,
                  precise: bool = True) -> dict:
    """Параметры yt-dlp.

    mode: "video" | "audio". for_editing — предпочесть H.264 + AAC в MP4:
    такие файлы без перекодирования открывают Premiere, DaVinci и After Effects
    (у YouTube H.264 бывает до 1080p; выше — только VP9/AV1).
    """
    opts = _base_options()
    # Имя без папки: папки задаёт "paths" (home — итог, temp — куски), иначе
    # yt-dlp игнорирует temp и сорит кусками прямо в папке загрузок.
    opts["outtmpl"] = "%(title).150B.%(ext)s"
    opts["paths"] = {"home": str(dest)}
    if section is not None:
        from yt_dlp.utils import download_range_func

        start, end = section
        opts["download_ranges"] = download_range_func(None, [(start, end)])
        # Точно по кадрам: ffmpeg перекодирует края, иначе начало «прилипает»
        # к ближайшему ключевому кадру (может уйти на несколько секунд).
        opts["force_keyframes_at_cuts"] = bool(precise)
        if precise and mode != "audio":
            # Перекодирование по умолчанию (x264 medium) втрое медленнее;
            # veryfast + CRF 17 — почти без потерь и быстро.
            opts["external_downloader_args"] = {"ffmpeg_o": [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k"]}
        # Фрагмент не должен затереть целое видео с тем же названием.
        opts["outtmpl"] = f"%(title).140B [{clip_label(start, end)}].%(ext)s"
    ffmpeg = ffmpeg_path()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    if mode == "audio":
        opts["format"] = "bestaudio/best"
        codec = audio_format if audio_format in AUDIO_FORMATS else "mp3"
        post = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
        if codec == "mp3":
            post["preferredquality"] = "320"
        opts["postprocessors"] = [post]
        return opts
    cap = f"[height<={height}]" if height else ""
    opts["format"] = f"bv*{cap}+ba/b{cap}/bv*+ba/b"
    opts["merge_output_format"] = "mp4"
    resolution = [f"res:{height}"] if height else ["res"]
    # Для монтажа сначала H.264 + AAC, потом разрешение; иначе — просто лучшее качество.
    opts["format_sort"] = (["vcodec:h264", "acodec:m4a", *resolution] if for_editing else resolution)
    return opts


def download(url: str, dest: Path, mode: str = "video", height: int = 0, audio_format: str = "mp3",
             for_editing: bool = True, on_progress: Callable[[Progress], None] | None = None,
             cancel: threading.Event | None = None, section: tuple[float, float] | None = None,
             precise: bool = True) -> Path:
    """Скачивает и возвращает путь к готовому файлу.

    Промежуточные файлы — в скрытой папке dest/.noukocloud-parts/social-…;
    после отмены или ошибки она удаляется целиком.
    """
    dest.mkdir(parents=True, exist_ok=True)
    parts = dest / PARTS_DIR
    parts.mkdir(exist_ok=True)
    hide_path(parts)
    temp = parts / f"social-{uuid.uuid4().hex[:8]}"
    opts = build_options(dest, mode, height, audio_format, for_editing, section, precise)
    opts["paths"]["temp"] = str(temp)
    result: dict = {}
    _track_processes()
    thread_id = threading.get_ident()
    finished = threading.Event()

    def watch_cancel() -> None:
        # Склейка и конвертация не вызывают наших хуков — ffmpeg просто убиваем.
        while not finished.wait(0.2):
            if cancel is not None and cancel.is_set():
                _kill_processes(thread_id)

    def hook(d: dict) -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        status = d.get("status")
        if status == "downloading" and on_progress is not None:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            on_progress(Progress(
                percent=(done * 100 / total) if total else -1,
                speed=float(d.get("speed") or 0),
                eta=float(d.get("eta") or 0),
            ))
        elif status == "finished":
            result["file"] = d.get("filename")
            if on_progress is not None:
                on_progress(Progress(percent=100, stage="process"))

    def post_hook(d: dict) -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if d.get("status") == "finished":
            path = (d.get("info_dict") or {}).get("filepath")
            if path:
                result["file"] = path

    opts["progress_hooks"] = [hook]
    opts["postprocessor_hooks"] = [post_hook]
    if cancel is not None:
        threading.Thread(target=watch_cancel, name="social-cancel", daemon=True).start()
    final = None
    failure: str | None = None       # None — успех, "" — отмена, иначе текст ошибки
    try:
        with _yt_class()(opts) as ydl:
            ydl.cancel = cancel
            info = ydl.extract_info(url, download=True)
            if info is not None and info.get("_type") == "playlist":
                entries = [e for e in (info.get("entries") or []) if e]
                info = entries[0] if entries else None
            if info is not None:
                final = (info.get("requested_downloads") or [{}])[-1].get("filepath") or info.get("filepath")
            final = final or result.get("file")
    except Cancelled:
        failure = ""
    except Exception as exc:
        if cancel is not None and cancel.is_set():
            failure = ""
        else:
            log.warning("Соцсети: не удалось скачать %s: %s", url, exc, exc_info=True)
            failure = friendly(exc)
    finally:
        finished.set()
        with _procs_lock:
            _procs.pop(thread_id, None)
    # Чистим уже вне except: исключение держит кадры yt-dlp, а с ними и
    # открытый .part — Windows не дала бы его удалить.
    _cleanup(temp)
    if failure == "":
        raise Cancelled()
    if failure is not None:
        raise SocialError(failure)
    if not final or not Path(final).exists():
        raise SocialError("Файл не сохранился. Подробности — в журнале.")
    log.info("Соцсети: скачано %s", final)
    return Path(final)


def _cleanup(temp: Path) -> None:
    """Убирает временную папку загрузки (файлы бывают ещё заняты ffmpeg)."""
    import gc

    gc.collect()                     # закрывает брошенные yt-dlp файлы
    for _ in range(25):
        shutil.rmtree(temp, ignore_errors=True)
        if not temp.exists():
            break
        time.sleep(0.2)
    try:
        temp.parent.rmdir()          # пустую .noukocloud-parts тоже убираем
    except OSError:
        pass
