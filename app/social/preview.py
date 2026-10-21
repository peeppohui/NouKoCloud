"""Предпросмотр во вкладке «Соцсети».

Смотреть ролик нужно сразу и с перемоткой — чтобы отметить начало и конец
фрагмента. Поэтому:

* берём лёгкий поток (до 720p, предпочтительно H.264 + AAC — их уверенно
  показывает плеер);
* у YouTube видео и звук лежат отдельно — плеер играет их двумя дорожками
  и держит в синхроне (PreviewPlayer, audio_url);
* читает плеер через локальный посредник (RangeProxy): тот качает нужные
  куски во много соединений, иначе YouTube отдавал бы ~70 КБ/с — меньше,
  чем нужно даже для 360p.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_PREVIEW_HEIGHT = 720
HTTP = ("http", "https")


@dataclass
class PreviewPick:
    video: dict | None          # формат с картинкой (может содержать и звук)
    audio: dict | None = None   # отдельный звук, если у video его нет


def _has(fmt: dict, key: str) -> bool:
    return fmt.get(key) not in (None, "none")


def _codec_rank(fmt: dict) -> int:
    codec = str(fmt.get("vcodec") or "")
    if codec.startswith(("avc1", "h264")):
        return 0
    if codec.startswith(("vp09", "vp9")):
        return 1
    return 2                     # AV1 и прочее: тяжелее для показа


def pick_streams(info: dict) -> PreviewPick | None:
    """Какие потоки показывать в предпросмотре (None — нечего показать)."""
    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    http = [f for f in formats if f.get("protocol") in HTTP]

    def fits(f: dict) -> bool:
        return (f.get("height") or 0) <= MAX_PREVIEW_HEIGHT

    def best(candidates: list[dict]) -> dict | None:
        candidates = [f for f in candidates if fits(f)] or candidates
        if not candidates:
            return None
        return min(candidates, key=lambda f: (_codec_rank(f), -(f.get("height") or 0), f.get("tbr") or 0))

    both = [f for f in http if _has(f, "vcodec") and _has(f, "acodec")]
    if both:
        return PreviewPick(best(both))
    video_only = [f for f in http if _has(f, "vcodec") and not _has(f, "acodec")]
    audio_only = [f for f in http if _has(f, "acodec") and not _has(f, "vcodec")]
    if video_only:
        audio = None
        if audio_only:
            # AAC (m4a) плеер играет надёжнее всего; среди них — полегче.
            audio = min(audio_only, key=lambda f: (0 if str(f.get("acodec", "")).startswith("mp4a") else 1,
                                                    f.get("abr") or f.get("tbr") or 0))
        return PreviewPick(best(video_only), audio)
    # Только потоки m3u8 и т. п.: плеер прочитает их сам (без ускорения).
    streams = [f for f in formats if _has(f, "vcodec") and _has(f, "acodec")]
    if streams:
        return PreviewPick(best(streams))
    if audio_only:
        return PreviewPick(None, audio_only[-1])
    if info.get("url") and info.get("protocol") in HTTP:
        return PreviewPick(info)     # одиночный файл без списка форматов
    return None


class PreviewSession:
    """Адреса для плеера + посредник, который живёт, пока идёт просмотр."""

    def __init__(self, info: dict) -> None:
        from .downloader import _base_options, _yt_class
        from .range_proxy import RangeProxy

        self.video_url = ""
        self.audio_url = ""
        self.kind = "video"
        pick = pick_streams(info)
        if pick is None:
            raise LookupError("нет потока для предпросмотра")
        self._ydl = _yt_class()({**_base_options(), "skip_download": True})

        def fetch(url: str, headers: dict, start: int, end: int) -> bytes:
            from yt_dlp.networking import Request

            req = Request(url, headers={**headers, "Range": f"bytes={start}-{end}"})
            with self._ydl.urlopen(req) as resp:
                return resp.read()

        self._proxy = RangeProxy(fetch)
        try:
            if pick.video is not None:
                self.video_url = self._local(pick.video, info)
            if pick.audio is not None:
                self.audio_url = self._local(pick.audio, info)
            if not self.video_url:
                self.kind, self.video_url, self.audio_url = "audio", self.audio_url, ""
        except Exception:
            self.close()
            raise
        log.info("Соцсети: предпросмотр %s%s", (pick.video or {}).get("format_id", "—"),
                 f" + звук {pick.audio.get('format_id')}" if pick.audio else "")

    def _local(self, fmt: dict, info: dict) -> str:
        """Адрес через посредника; если размер неизвестен — прямой адрес."""
        url = fmt["url"]
        if fmt.get("protocol") not in HTTP:
            return url
        headers = fmt.get("http_headers") or info.get("http_headers") or {}
        size = fmt.get("filesize") or self._ydl._content_length({**fmt, "http_headers": headers})
        if not size:
            return url
        return self._proxy.add(url, headers, int(size))

    def close(self) -> None:
        try:
            self._proxy.close()
        except Exception:
            pass
        try:
            self._ydl.close()
        except Exception:
            pass
