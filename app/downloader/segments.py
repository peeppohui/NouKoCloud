"""Разбиение файла на сегменты для параллельной докачки."""

from __future__ import annotations

import logging

from ..core.models import Segment

log = logging.getLogger(__name__)

MB = 1024 * 1024


#: Больше частей не нарезаем: у файла в 10 ГБ части станут крупнее 4 МБ.
MAX_SEGMENTS = 512


def plan_segments(total: int, *, segment_size_mb: int, max_segments: int = MAX_SEGMENTS) -> list[Segment]:
    """Делит файл на части примерно по segment_size_mb.

    Частей обычно больше, чем одновременных соединений: загрузчик держит
    очередь и качает их по N штук (Transfer._run_parallel). Так соединения
    не простаивают до самого конца файла: облако (особенно Яндекс) режет
    скорость каждого соединения, и всё решает их число.
    Файл не больше одной части не делим.
    """
    if total <= 0:
        return []
    chunk = max(1, segment_size_mb) * MB
    if total <= chunk:
        return [Segment(index=0, start=0, end=total - 1)]

    count = min(max(2, max_segments), -(-total // chunk))
    # Раскладываем остаток на последний сегмент, чтобы сумма совпала с total.
    base = total // count
    segments: list[Segment] = []
    start = 0
    for index in range(count):
        end = start + base - 1 if index < count - 1 else total - 1
        segments.append(Segment(index=index, start=start, end=end))
        start = end + 1
    return segments


def segments_complete(segments: list[Segment]) -> bool:
    return bool(segments) and all(seg.done for seg in segments)


def validate_segments(segments: list[Segment], total: int) -> bool:
    """Проверяет, что сегменты покрывают файл целиком и без пересечений."""
    if not segments:
        return False
    ordered = sorted(segments, key=lambda s: s.start)
    if ordered[0].start != 0:
        return False
    if ordered[-1].end != total - 1:
        return False
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt.start != prev.end + 1:
            return False
    return True
