"""Состояние недокачанного файла.

Лежит рядом с файлом как `<имя>.part.json` — поэтому переживает
перезапуск приложения и позволяет продолжить с того же места.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from ..core.models import CloudFile, DownloadJob, EntryKind, ProviderId, Segment, SourceKind

log = logging.getLogger(__name__)

VERSION = 1


def _file_to_dict(file: CloudFile) -> dict:
    return {
        "provider": file.provider.value,
        "file_id": file.file_id,
        "path": file.path,
        "name": file.name,
        "size": file.size,
        "md5": file.md5,
        "mime": file.mime,
        "source": file.source.value,
        "public_key": file.public_key,
        "drive_id": file.drive_id,
        "resource_key": file.resource_key,
        "google_mime": file.google_mime,
    }


def _file_from_dict(data: dict) -> CloudFile:
    return CloudFile(
        provider=ProviderId(data.get("provider", "yandex")),
        file_id=data.get("file_id", ""),
        path=data.get("path", ""),
        name=data.get("name", ""),
        kind=EntryKind.FILE,
        size=int(data.get("size") or 0),
        mime=data.get("mime", ""),
        md5=data.get("md5"),
        source=SourceKind(data.get("source", "oauth")),
        public_key=data.get("public_key"),
        drive_id=data.get("drive_id"),
        resource_key=data.get("resource_key"),
        google_mime=data.get("google_mime"),
    )


def save_state(job: DownloadJob) -> None:
    """Сохраняет прогресс сегментов на диск."""
    payload = {
        "version": VERSION,
        "file": _file_to_dict(job.file),
        "dest": str(job.dest_dir),
        "total": job.total_bytes,
        "segments": [asdict(seg) for seg in job.segments],
        # Состояние на момент сохранения. По нему recover() отличает
        # загрузку, прерванную закрытием программы, от отменённой вручную:
        # отменённую не надо запускать автоматически.
        "job_state": job.state.value,
        "updated_at": time.time(),
    }
    path = job.state_path
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Не удалось сохранить состояние %s: %s", path, exc)


def load_state(path: Path) -> Optional[tuple[CloudFile, int, list[Segment]]]:
    """Читает состояние. None — если файла нет или он повреждён."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Состояние %s повреждено: %s", path, exc)
        return None
    if payload.get("version") != VERSION:
        return None
    try:
        file = _file_from_dict(payload["file"])
        total = int(payload.get("total") or file.size or 0)
        segments = [
            Segment(index=int(s["index"]), start=int(s["start"]), end=int(s["end"]), received=int(s.get("received", 0)))
            for s in payload.get("segments", [])
        ]
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("Состояние %s не разобрано: %s", path, exc)
        return None
    if total and segments and sum(seg.total for seg in segments) != total:
        return None
    return file, total, segments


def state_dest(path: Path) -> Path | None:
    """Папка назначения из файла состояния."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    dest = payload.get("dest")
    return Path(dest) if dest else None


def state_job_state(path: Path) -> str | None:
    """Состояние задания, записанное вместе с сегментами."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("job_state")
    return str(value) if value else None


def clear_state(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def find_states(directory: Path) -> list[Path]:
    """Ищет файлы состояния в каталоге и подкаталогах (восстановление после рестарта)."""
    if not directory.exists():
        return []
    return sorted(directory.rglob("*.part.json"))
