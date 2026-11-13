"""Сбор диагностики для обращения в поддержку.

Архив создаётся только по явной команде пользователя и никуда не
отправляется. Перед упаковкой из журналов и настроек вычищаются токены,
ключи, заголовки Authorization и подписанные параметры ссылок.
"""

from __future__ import annotations

import json
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import AppConfig
from .models import ProviderId
from .paths import app_data_dir, is_frozen, logs_dir

#: Сколько байт журнала кладём в архив (с конца файла).
LOG_TAIL_BYTES = 5 * 1024 * 1024
DIAG_PREFIX = "NoukoCloud-diagnostics-"

_SECRET_NAMES = (
    "access_token", "refresh_token", "client_secret", "client_id",
    "api_key", "apikey", "authorization", "token", "signature", "sig",
    "password", "secret",
)
_SECRET_FIELDS = {
    "yandex_client_id", "yandex_client_secret",
    "google_client_id", "google_client_secret", "google_api_key",
}
_SAFE_FIELDS = (
    "download_dir", "max_parallel_files", "max_parallel_segments",
    "segment_min_mb", "use_segments", "max_retries", "retry_base_delay",
    "retry_max_delay", "requests_per_second", "download_speed_limit_mbps",
    "keep_partial_files", "user_agent", "connect_timeout", "read_timeout",
    "preview_max_height", "preview_proxy_seconds", "preview_autoplay",
    "preview_cache_gb", "preview_cache_dir", "ui_theme", "ui_motion",
    "ui_sounds", "ui_volume", "run_in_background", "update_check_on_start",
    "config_version", "log_level", "device_name",
)

_KV_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(x) for x in _SECRET_NAMES) + r")\b"
    r"\s*([:=])\s*(\"[^\"]*\"|'[^']*'|[^\s&,;]+)"
)
_AUTH_RE = re.compile(r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s,;]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")
_USERINFO_RE = re.compile(r"(https?://)[^/@\s]+@")


def sanitize_text(text: Any) -> str:
    """Убирает из текста секреты и подписанные параметры ссылок."""
    value = str(text or "")
    value = _AUTH_RE.sub(r"\1<redacted>", value)
    value = _BEARER_RE.sub("Bearer <redacted>", value)
    value = _URL_QUERY_RE.sub(r"\1?<redacted>", value)
    value = _USERINFO_RE.sub(r"\1<redacted>@", value)
    value = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", value)
    return value


def _secret_state(value: Any) -> str:
    return "задан" if str(value or "").strip() else "не задан"


def safe_config(config: AppConfig) -> dict[str, Any]:
    """Настройки без секретов: только то, что помогает разбору."""
    result: dict[str, Any] = {}
    for name in _SAFE_FIELDS:
        if hasattr(config, name):
            value = getattr(config, name)
            if name == "proxy":
                value = sanitize_text(value)
            elif isinstance(value, Path):
                value = str(value)
            result[name] = value
    for name in sorted(_SECRET_FIELDS):
        result[name] = _secret_state(getattr(config, name, ""))
    result["recent_links_count"] = len(getattr(config, "recent_links", []) or [])
    result["download_dir_exists"] = Path(config.download_dir).exists()
    return result


def _ffmpeg_version(ffmpeg: Any) -> str:
    path = getattr(ffmpeg, "path", None) or ffmpeg
    if not path:
        candidate = shutil.which("ffmpeg")
        path = candidate
    if not path:
        return "не найден"
    try:
        kwargs: dict[str, Any] = {
            "capture_output": True, "text": True, "timeout": 5,
            "encoding": "utf-8", "errors": "replace",
        }
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run([str(path), "-version"], **kwargs)
        line = (proc.stdout or proc.stderr or "").splitlines()
        return sanitize_text(line[0] if line else "версия не определена")
    except Exception as exc:
        return f"не удалось определить: {sanitize_text(exc)}"


def _ytdlp_version() -> str:
    try:
        import yt_dlp.version  # type: ignore
        return str(yt_dlp.version.__version__)
    except Exception:
        return "не установлен"


def _provider_status(oauth: Any) -> list[str]:
    lines: list[str] = []
    if oauth is None:
        return ["провайдеры: нет данных"]
    for pid in ProviderId:
        try:
            configured = bool(oauth.configured(pid))
            logged = bool(oauth.is_logged_in(pid))
        except Exception:
            configured = logged = False
        lines.append(
            f"{pid.value}: ключи={'да' if configured else 'нет'}, "
            f"вход={'да' if logged else 'нет'}"
        )
    return lines


def queue_snapshot(engine: Any) -> list[dict[str, Any]]:
    jobs = []
    if engine is None:
        return jobs
    try:
        items = engine.snapshot()
    except Exception:
        return jobs
    for job in items:
        file = getattr(job, "file", None)
        jobs.append({
            "name": getattr(file, "name", ""),
            "provider": getattr(getattr(file, "provider", None), "value", ""),
            "size": int(getattr(file, "size", 0) or 0),
            "state": getattr(getattr(job, "state", None), "value", ""),
            "downloaded_bytes": int(getattr(job, "downloaded_bytes", 0) or 0),
            "error": sanitize_text(getattr(job, "error", "") or ""),
        })
    return jobs


def build_manifest(
    config: AppConfig,
    *,
    oauth: Any = None,
    engine: Any = None,
    ffmpeg: Any = None,
) -> str:
    from app import __version__

    now = datetime.now().astimezone()
    try:
        loc = locale.getlocale()[0] or "не определена"
    except Exception:
        loc = "не определена"
    lines = [
        "Nouko Cloud — диагностика",
        f"Версия: {__version__}",
        f"Собрано: {now.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"Сборка: {'portable' if is_frozen() else 'из исходников'}",
        f"ОС: {platform.platform()}",
        f"Архитектура: {platform.machine()}",
        f"Python: {sys.version.split()[0]}",
        f"Локаль: {loc}",
        f"Данные программы: {app_data_dir()}",
        f"Журналы: {logs_dir()}",
        "",
        "Провайдеры:",
        *_provider_status(oauth),
        "",
        f"ffmpeg: {_ffmpeg_version(ffmpeg)}",
        f"yt-dlp: {_ytdlp_version()}",
        "",
        f"Заданий в очереди: {len(queue_snapshot(engine))}",
    ]
    return sanitize_text("\n".join(lines)) + "\n"


README = """Nouko Cloud — архив диагностики

Что внутри:
  manifest.txt   — версия, ОС, состояние входа и версии ffmpeg/yt-dlp;
  settings.json  — безопасные настройки (ключи отмечены «задан/не задан»);
  queue.json     — список заданий без путей к личным папкам;
  *.log          — последние 5 МБ журналов с вычищенными секретами.

Чего здесь нет:
  токенов входа, client_secret, заголовков Authorization, паролей,
  подписанных ссылок и полных URL с параметрами.

Архив создан по вашей команде и никуда не отправлялся. Перед отправкой
в поддержку откройте его и проверьте содержимое. Приложите zip к issue
на GitHub или письму, опишите шаги, после которых возникла проблема.
"""


def _tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            data = fh.read()
        return sanitize_text(data.decode("utf-8", errors="replace"))
    except OSError:
        return ""


def _default_target() -> Path:
    desktop = Path.home() / "Desktop"
    return desktop if desktop.is_dir() else Path.home()


def create_diagnostics(
    target_dir: Optional[Path] = None,
    *,
    config: AppConfig,
    oauth: Any = None,
    engine: Any = None,
    ffmpeg: Any = None,
) -> Path:
    """Создаёт zip и возвращает путь. Ничего не отправляет."""
    target = Path(target_dir) if target_dir is not None else _default_target()
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = target / f"{DIAG_PREFIX}{stamp}.zip"
    temp = target / f".{DIAG_PREFIX}{stamp}.tmp"
    manifest = build_manifest(config, oauth=oauth, engine=engine, ffmpeg=ffmpeg)
    settings = json.dumps(safe_config(config), ensure_ascii=False, indent=2)
    queue = json.dumps(queue_snapshot(engine), ensure_ascii=False, indent=2)
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.txt", README)
            zf.writestr("manifest.txt", manifest)
            zf.writestr("settings.json", settings + "\n")
            zf.writestr("queue.json", queue + "\n")
            for name in ("noukocloud.log", "errors.log", "crash.log"):
                path = logs_dir() / name
                if path.is_file():
                    zf.writestr(name, _tail(path))
        os.replace(temp, final)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass
    return final
