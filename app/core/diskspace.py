"""Проверка свободного места перед загрузкой.

Ошибка «диск заполнен» посреди многогигабайтной загрузки стоит дорого:
пользователь ждал, а файл всё равно не сохранился. Поэтому до постановки
в очередь проверяем папку и запас, а во время работы движок следит за
местом отдельно.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import CloudFile

GIB = 1024 ** 3
RESERVE_BYTES = GIB          # запас на служебные файлы и «хвост» загрузки
LOW_SPACE_BYTES = 256 * 1024 ** 2


@dataclass(frozen=True, slots=True)
class SpaceCheck:
    directory: Path
    known_bytes: int
    unknown_count: int
    free_bytes: int
    required_bytes: int

    @property
    def ok(self) -> bool:
        return self.free_bytes >= self.required_bytes

    @property
    def missing_bytes(self) -> int:
        return max(0, self.required_bytes - self.free_bytes)


def _probe_writable(directory: Path) -> None:
    """Создаёт и сразу удаляет временный файл — проверка прав и ФС."""
    directory.mkdir(parents=True, exist_ok=True)
    probe = tempfile.NamedTemporaryFile(
        mode="wb", prefix=".noukocloud-space-", dir=directory, delete=False)
    try:
        probe.write(b"ok")
        probe.flush()
    finally:
        probe.close()
        try:
            Path(probe.name).unlink(missing_ok=True)
        except OSError:
            pass


def check_space(directory: Path | str, files: Iterable[CloudFile],
                reserve: int = RESERVE_BYTES) -> SpaceCheck:
    """Сколько нужно и сколько есть.

    Размер части файлов может быть неизвестен (экспорт Google Документов,
    редкие ответы API). В этом случае требуем хотя бы резерв: иначе
    проверка «0 байт нужно» пропустила бы явно недостаточное место.
    """
    target = Path(directory).expanduser()
    _probe_writable(target)
    known = 0
    unknown = 0
    for file in files:
        size = max(0, int(file.size or 0))
        if size:
            known += size
        else:
            unknown += 1
    required = known + max(0, reserve)
    free = int(shutil.disk_usage(target).free)
    return SpaceCheck(target, known, unknown, free, required)


def format_bytes(value: int) -> str:
    number = float(max(0, value))
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if number < 1024 or unit == "ТБ":
            return f"{number:.0f} {unit}" if unit == "Б" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} ТБ"
