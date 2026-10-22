"""Модели данных, которыми обмениваются ядро, провайдеры и UI."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class ProviderId(str, Enum):
    YANDEX = "yandex"
    GOOGLE = "google"


class EntryKind(str, Enum):
    FILE = "file"
    FOLDER = "folder"


class SourceKind(str, Enum):
    """Откуда получен доступ к ресурсу."""

    OAUTH = "oauth"          # свой аккаунт
    PUBLIC = "public"        # публичная ссылка (без входа)


@dataclass(slots=True)
class CloudFile:
    """Один элемент облака: файл или папка."""

    provider: ProviderId
    file_id: str
    path: str
    name: str
    kind: EntryKind
    size: int = 0
    mime: str = ""
    modified: datetime | None = None
    md5: str | None = None
    source: SourceKind = SourceKind.OAUTH
    public_key: str | None = None
    parent_id: str | None = None
    is_shared_drive: bool = False
    drive_id: str | None = None
    resource_key: str | None = None
    google_mime: str | None = None   # MIME-тип Google (для export документов)
    preview_hint: str | None = None  # ссылка на превью, если провайдер её отдаёт
    thumbnail_link: str | None = None
    media_duration_ms: int | None = None
    media_width: int | None = None
    media_height: int | None = None
    can_download: bool = True

    @property
    def is_file(self) -> bool:
        return self.kind is EntryKind.FILE

    @property
    def modified_ts(self) -> float:
        if self.modified is None:
            return 0.0
        return self.modified.replace(tzinfo=timezone.utc).timestamp()


@dataclass(slots=True)
class DownloadLink:
    """Готовая ссылка на скачивание + заголовки, нужные для запроса."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "GET"
    size: int | None = None
    expires_at: float | None = None
    supports_range: bool = True
    content_type: str | None = None
    etag: str | None = None
    #: Резервный адрес того же файла (например, официальный API после
    #: публичного веб-адреса Google). Используется, если основной отдал 403.
    fallback: "DownloadLink | None" = None

    def is_expired(self, skew: float = 60.0) -> bool:
        if self.expires_at is None:
            return False
        return time.time() + skew >= self.expires_at


@dataclass(slots=True)
class MediaInfo:
    """Метаданные медиа для превью (длительность, разрешение, постер)."""

    duration: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool = False
    poster_url: str | None = None
    kind: str = "unknown"  # video | audio | image | unknown
    transcodable: bool = True


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass(slots=True)
class Segment:
    """Часть файла, скачиваемая отдельным запросом (для больших файлов)."""

    index: int
    start: int
    end: int            # включительно
    received: int = 0

    @property
    def total(self) -> int:
        return self.end - self.start + 1

    @property
    def done(self) -> bool:
        return self.received >= self.total

    @property
    def percent(self) -> int:
        return min(100, int(self.received * 100 / max(1, self.total)))


#: Служебная (скрытая) папка недокачанных файлов внутри папки загрузок.
PARTS_DIR = ".noukocloud-parts"


@dataclass(slots=True)
class DownloadJob:
    """Задание на скачивание одного файла."""

    file: CloudFile
    dest_dir: Path
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: JobState = JobState.QUEUED
    total_bytes: int = 0
    downloaded_bytes: int = 0
    speed_bps: float = 0.0
    error: str | None = None
    attempts: int = 0
    segments: list[Segment] = field(default_factory=list)
    link: DownloadLink | None = None
    #: Уже переключились на резервную ссылку — назад не возвращаемся.
    link_switched: bool = False
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    priority: int = 0
    pause_event: "asyncio.Event" = field(default_factory=asyncio.Event, repr=False, compare=False)
    cancel_event: "asyncio.Event" = field(default_factory=asyncio.Event, repr=False, compare=False)

    @property
    def filename(self) -> str:
        return self.file.name

    @property
    def percent(self) -> int:
        if not self.total_bytes:
            return 0
        return min(100, int(self.downloaded_bytes * 100 / self.total_bytes))

    @property
    def parts_dir(self) -> Path:
        """Скрытая папка для недокачанного: в папке загрузок появляется
        сразу готовый файл, без «.part» рядом."""
        return self.dest_dir / PARTS_DIR

    @property
    def part_path(self) -> Path:
        return self.parts_dir / f"{self.file.name}.part"

    @property
    def state_path(self) -> Path:
        return self.parts_dir / f"{self.file.name}.part.json"

    @property
    def final_path(self) -> Path:
        return self.dest_dir / self.file.name
