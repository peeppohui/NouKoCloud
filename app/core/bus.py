"""Шина событий между фоновым asyncio-потоком и GUI.

Сигналы Qt — единственный безопасный способ передавать данные из
рабочего потока в главный: они автоматически маршалятся в поток GUI.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class Bus(QObject):
    """Словарь сигналов приложения."""

    # Навигация по облаку
    listing_started = Signal(str)                 # provider id
    listing_progress = Signal(str, int, int)      # provider id, загружено, всего
    listing_finished = Signal(str, object)        # provider id, list[CloudFile]
    listing_failed = Signal(str, str)             # provider id, сообщение

    # Очередь загрузок
    jobs_snapshot = Signal(object)                # list[DownloadJob]
    job_progress = Signal(object)                 # DownloadJob
    job_finished = Signal(object)                 # DownloadJob
    queue_stats = Signal(int, int, int)           # всего, готово, в работе

    # Превью
    preview_state = Signal(str, str)              # token, состояние
    preview_ready = Signal(str, object)           # token, MediaInfo

    # Уведомления для пользователя
    notice = Signal(str, str)                     # уровень (info/warn/error), текст
    logged_in = Signal(str, str)                 # provider id, название аккаунта
    logged_out = Signal(str)
