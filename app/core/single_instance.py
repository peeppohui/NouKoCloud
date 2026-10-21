"""Одна копия программы.

Программа живёт в фоне, поэтому повторный запуск (ярлык, автозапуск,
«Открыть с помощью») не должен открывать вторую копию: он передаёт
команду уже запущенной — показать окно или открыть ссылку — и выходит.

Имя канала зависит от папки настроек: копии с разными настройками
(например, переносная версия) друг другу не мешают.
"""

from __future__ import annotations

import hashlib
import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .paths import APP_NAME, app_data_dir

log = logging.getLogger(__name__)


def channel_name() -> str:
    digest = hashlib.sha1(str(app_data_dir()).lower().encode("utf-8")).hexdigest()[:12]
    return f"{APP_NAME}-{digest}"


def send_to_running(message: str, timeout_ms: int = 400) -> bool:
    """True — программа уже запущена и получила команду."""
    socket = QLocalSocket()
    socket.connectToServer(channel_name())
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write((message + "\n").encode("utf-8"))
    socket.flush()
    socket.waitForBytesWritten(timeout_ms)
    # Ждём ответ «ok»: на Windows канал, закрытый сразу после записи,
    # может потерять ещё не прочитанную команду.
    socket.waitForReadyRead(max(timeout_ms, 2000))
    socket.disconnectFromServer()
    return True


class InstanceServer(QObject):
    """Принимает команды от повторных запусков: "show" или "open <ссылка>"."""

    message = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server = QLocalServer(self)
        name = channel_name()
        if not self._server.listen(name):
            # Остался канал от упавшей копии — убираем и пробуем ещё раз.
            QLocalServer.removeServer(name)
            if not self._server.listen(name):
                log.warning("Канал одной копии не открыт: %s", self._server.errorString())
        self._server.newConnection.connect(self._on_connection)

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(socket.deleteLater)
            # Команда могла прийти раньше подписки на readyRead.
            self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        while socket.canReadLine():
            text = bytes(socket.readLine().data()).decode("utf-8", "replace").strip()
            if text:
                socket.write(b"ok\n")
                socket.flush()
                self.message.emit(text)
