"""Работа с ffmpeg: постер, «облегчённая» копия, метаданные.

ffmpeg нужен только для форматов, которые не играет браузер
(mkv, avi, prores) и для кадров-постеров. Если ffmpeg не установлен —
приложение работает, но превью для таких файлов будет недоступно.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from ..core.config import AppConfig
from ..core.paths import bundled_ffmpeg

log = logging.getLogger(__name__)

DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")


class FfmpegTools:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._path: Optional[str] = self._locate()

    def _locate(self) -> Optional[str]:
        if self._config.ffmpeg_path and Path(self._config.ffmpeg_path).exists():
            return self._config.ffmpeg_path
        found = shutil.which("ffmpeg")
        if found:
            return found
        bundled = bundled_ffmpeg()
        return str(bundled) if bundled else None

    @property
    def available(self) -> bool:
        return bool(self._path)

    @property
    def path(self) -> Optional[str]:
        return self._path

    # ------------------------------------------------------------------
    def _headers_arg(self, headers: dict[str, str]) -> list[str]:
        if not headers:
            return []
        joined = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        return ["-headers", joined]

    async def _run(self, args: list[str], timeout: float = 120.0) -> tuple[int, bytes, bytes]:
        assert self._path
        proc = await asyncio.create_subprocess_exec(
            self._path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        return proc.returncode or 0, out, err

    # ------------------------------------------------------------------
    async def probe(self, url: str, headers: dict[str, str] | None = None) -> dict | None:
        """Достаёт длительность из вывода ffmpeg."""
        if not self._path:
            return None
        # -headers — опция входа: она обязана стоять ПЕРЕД -i, иначе
        # относится к выходному файлу и в облако не уходит.
        args = ["-hide_banner", *self._headers_arg(headers or {}), "-i", url, "-f", "null", "-"]
        try:
            _code, _out, err = await self._run(args, timeout=60)
        except Exception as exc:
            log.debug("ffmpeg probe не удался: %s", exc)
            return None
        text = err.decode("utf-8", "ignore")
        match = DURATION_RE.search(text)
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        width = height = None
        size_match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
        if size_match:
            width, height = int(size_match.group(1)), int(size_match.group(2))
        return {"duration": duration, "width": width, "height": height}

    async def poster(self, url: str, headers: dict[str, str] | None, out_path: Path,
                     at_seconds: float = 5.0) -> bool:
        """Сохраняет кадр-постер в JPEG."""
        if not self._path:
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, at_seconds):.2f}",
            *self._headers_arg(headers or {}), "-i", url,
            "-frames:v", "1", "-q:v", "3", "-y", str(out_path),
        ]
        try:
            code, _out, err = await self._run(args, timeout=180)
        except Exception as exc:
            log.info("Кадр не снят: %s", exc)
            return False
        if code != 0 or not out_path.exists():
            log.info("ffmpeg не смог снять кадр: %s", err.decode("utf-8", "ignore")[:200])
            return False
        return True

    async def build_proxy(self, url: str, headers: dict[str, str] | None, out_path: Path,
                          max_height: int = 480, seconds: int = 60) -> bool:
        """Перекодирует начало файла в mp4 (H.264/AAC) для браузера."""
        if not self._path:
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "-hide_banner", "-loglevel", "error",
            *self._headers_arg(headers or {}), "-i", url,
            "-t", str(max(5, seconds)),
            "-vf", f"scale=-2:{max(144, max_height // 2) * 2}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-y", str(out_path),
        ]
        log.info("Готовим облегчённую копию: %s", out_path.name)
        try:
            code, _out, err = await self._run(args, timeout=900)
        except Exception as exc:
            log.info("Не удалось перекодировать: %s", exc)
            return False
        if code != 0 or not out_path.exists():
            log.info("ffmpeg: %s", err.decode("utf-8", "ignore")[:300])
            return False
        return True
