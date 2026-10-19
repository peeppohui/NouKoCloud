"""Настройки приложения.

Сложных настроек по умолчанию нет: в AppConfig попадает только то,
что реально нужно менять, и всё с разумными значениями.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .paths import app_data_dir, default_download_dir, portable_config_path

log = logging.getLogger(__name__)

CONFIG_FILE = app_data_dir() / "config.json"


@dataclass
class AppConfig:
    # --- Аккаунты ---
    yandex_client_id: str = ""
    yandex_client_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_api_key: str = ""

    # --- Загрузки ---
    download_dir: str = field(default_factory=lambda: str(default_download_dir()))
    max_parallel_files: int = 4          # сколько файлов одновременно
    max_parallel_segments: int = 4       # сколько кусков одного файла одновременно
    segment_min_mb: int = 64             # минимальный размер куска
    use_segments: bool = True            # многопоточная докачка больших файлов
    max_retries: int = 6
    retry_base_delay: float = 1.5
    retry_max_delay: float = 90.0
    requests_per_second: float = 4.0     # ограничитель частоты запросов
    download_speed_limit_mbps: int = 0   # 0 = без ограничения
    keep_partial_files: bool = True      # не удалять .part после обрыва

    # --- Сеть ---
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    connect_timeout: float = 20.0
    read_timeout: float = 120.0
    proxy: str = ""

    # --- Превью ---
    ffmpeg_path: str = ""
    preview_max_height: int = 720
    preview_proxy_seconds: int = 60
    preview_autoplay: bool = True

    # --- Служебное ---
    window_geometry: str = ""
    log_level: str = "INFO"
    device_id: str = ""        # идентификатор этой машины для Яндекс.Диска
    device_name: str = "svsCloud"

    # ------------------------------------------------------------------
    @property
    def download_dir_path(self) -> Path:
        path = Path(self.download_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self) -> None:
        CONFIG_FILE.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("Настройки сохранены: %s", CONFIG_FILE)

    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        # 1. «Портативные» настройки рядом с программой — дефолты сборки
        #    (например, ключи OAuth, чтобы пользователю не пришлось их вводить).
        portable = portable_config_path()
        if portable is not None:
            cfg._apply_file(portable)
        # 2. Свои настройки пользователя — имеют приоритет.
        if CONFIG_FILE.exists():
            cfg._apply_file(CONFIG_FILE)
        _apply_env_overrides(cfg)
        if not cfg.device_id:
            # Идентификатор устройства для Яндекс OAuth: пользователь видит
            # в своих настройках, какое устройство имеет доступ к диску,
            # и может отозвать доступ одним кликом.
            cfg.device_id = uuid.uuid4().hex[:32]
        return cfg

    def _apply_file(self, path: Path) -> None:
        """Читает файл настроек. Пустые значения не затирают заданные."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Не удалось прочитать настройки %s: %s", path, exc)
            return
        if not isinstance(raw, dict):
            return
        known = {f.name for f in fields(self)}
        for key, value in raw.items():
            if key not in known:
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            setattr(self, key, value)


def _apply_env_overrides(cfg: AppConfig) -> None:
    """Позволяет задать ключи через переменные окружения (удобно для сборок)."""
    mapping = {
        "SVSCLOUD_YANDEX_CLIENT_ID": "yandex_client_id",
        "SVSCLOUD_YANDEX_CLIENT_SECRET": "yandex_client_secret",
        "SVSCLOUD_GOOGLE_CLIENT_ID": "google_client_id",
        "SVSCLOUD_GOOGLE_CLIENT_SECRET": "google_client_secret",
        "SVSCLOUD_GOOGLE_API_KEY": "google_api_key",
    }
    for env_name, attr in mapping.items():
        value = os.environ.get(env_name)
        if value:
            setattr(cfg, attr, value)


def ensure_oauth_configured(cfg: AppConfig) -> list[str]:
    """Возвращает список провайдеров, для которых не хватает ключей."""
    missing: list[str] = []
    if not cfg.yandex_client_id:
        missing.append("Яндекс.Диск")
    if not cfg.google_client_id:
        missing.append("Google Диск")
    return missing
