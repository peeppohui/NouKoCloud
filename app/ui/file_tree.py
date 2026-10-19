"""Дерево файлов с чекбоксами.

Папки подгружаются лениво: при раскрытии папки её содержимое
запрашивается в фоновом потоке и подставляется в дерево.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTreeView, QVBoxLayout, QWidget

from ..core.models import CloudFile, EntryKind
from ..providers.base import BrowseContext

log = logging.getLogger(__name__)

FILE_ROLE = Qt.ItemDataRole.UserRole
CTX_ROLE = Qt.ItemDataRole.UserRole + 1
LOADED_ROLE = Qt.ItemDataRole.UserRole + 2
IS_DIR_ROLE = Qt.ItemDataRole.UserRole + 3


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


class FileTree(QWidget):
    """Виджет дерева: слева — структура, справа — ничего."""

    file_activated = Signal(object)      # CloudFile
    checked_changed = Signal(int)        # количество отмеченных
    children_loaded = Signal(int, object)  # token, list[CloudFile]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(["Файл", "Размер", "Изменён"])
        self._counter = 0
        self._pending: dict[int, QStandardItem] = {}   # token -> родительская папка

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._title = QLabel("Файлы", self)
        self._title.setProperty("heading", True)
        top.addWidget(self._title)
        top.addStretch(1)
        self._btn_all = QPushButton("Отметить всё", self)
        self._btn_none = QPushButton("Снять всё", self)
        self._btn_all.clicked.connect(lambda: self._set_all_checked(True))
        self._btn_none.clicked.connect(lambda: self._set_all_checked(False))
        top.addWidget(self._btn_all)
        top.addWidget(self._btn_none)
        layout.addLayout(top)

        self._view = QTreeView(self)
        self._view.setModel(self._model)
        self._view.setHeaderHidden(False)
        self._view.setAlternatingRowColors(False)
        self._view.setUniformRowHeights(True)
        self._view.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self._view.setColumnWidth(0, 330)
        self._view.setColumnWidth(1, 90)
        self._view.setColumnWidth(2, 130)
        self._view.expanded.connect(self._on_expanded)
        self._view.doubleClicked.connect(self._on_double_clicked)
        self._model.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self._view, 1)

        self._empty = QLabel(
            "Откройте папку: вставьте ссылку или выберите «Мой диск».",
            self,
        )
        self._empty.setProperty("muted", True)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        layout.addWidget(self._empty, 1)
        self._update_empty()

    # ------------------------------------------------------------------
    def add_root(self, title: str, ctx: BrowseContext, provider_label: str = "") -> QStandardItem:
        # Повторное открытие той же ссылки не должно плодить дубли.
        existing = self._find_root(title)
        if existing is not None:
            return existing
        root = QStandardItem(title)
        root.setEditable(False)
        root.setData(ctx, CTX_ROLE)
        root.setData(True, LOADED_ROLE)
        root.setIcon(self._icon_for_folder())
        self._model.appendRow(root)
        self._update_empty()
        return root

    def _find_root(self, title: str) -> Optional[QStandardItem]:
        for row in range(self._model.rowCount()):
            item = self._model.item(row)
            if item is not None and item.text() == title:
                return item
        return None

    def clear(self) -> None:
        self._model.clear()
        self._model.setHorizontalHeaderLabels(["Файл", "Размер", "Изменён"])
        self._pending.clear()
        self._update_empty()

    def _update_empty(self) -> None:
        has_rows = self._model.rowCount() > 0
        self._view.setVisible(has_rows)
        self._empty.setVisible(not has_rows)

    # ------------------------------------------------------------------
    def _icon_for_folder(self):
        from PySide6.QtWidgets import QApplication, QStyle

        app = QApplication.instance()
        if app is None:
            return None
        return app.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)

    def set_children(self, parent_item: QStandardItem, files: list[CloudFile]) -> None:
        """Подставляет содержимое папки."""
        # QStandardItemModel работает с индексами, а не с самими item'ами,
        # а детей добавляет item (у модели appendRow принимает один аргумент).
        parent_index = self._model.indexFromItem(parent_item)
        count = self._model.rowCount(parent_index)
        if count:
            self._model.removeRows(0, count, parent_index)
        for file in files:
            parent_item.appendRow(self._make_row(file, parent_item.data(CTX_ROLE)))
        parent_item.setData(True, LOADED_ROLE)

    def expand_item(self, item: QStandardItem) -> None:
        """Раскрывает элемент дерева (разворачивание живёт во view, не в item)."""
        self._view.expand(self._model.indexFromItem(item))

    def _make_row(self, file: CloudFile, ctx: Optional[BrowseContext]) -> list[QStandardItem]:
        name = QStandardItem(file.name)
        name.setEditable(False)
        name.setData(file, FILE_ROLE)
        name.setData(ctx, CTX_ROLE)
        name.setData(False, LOADED_ROLE)
        name.setData(file.kind is EntryKind.FOLDER, IS_DIR_ROLE)
        if file.kind is EntryKind.FILE:
            name.setCheckable(True)
            name.setCheckState(Qt.CheckState.Unchecked)
        else:
            # У папки чекбокс ставится автоматически, когда отмечены все дети.
            name.setCheckable(True)
            name.setCheckState(Qt.CheckState.Unchecked)
            name.setData(False, LOADED_ROLE)
            name.setChild(0, QStandardItem("загрузка…"))

        size = QStandardItem(human_size(file.size) if file.is_file else "")
        size.setEditable(False)
        size.setData(file, FILE_ROLE)

        modified = QStandardItem(
            file.modified.strftime("%d.%m.%Y %H:%M") if file.modified else ""
        )
        modified.setEditable(False)
        modified.setData(file, FILE_ROLE)
        return [name, size, modified]

    # ------------------------------------------------------------------
    def _on_expanded(self, index) -> None:
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        if item.data(LOADED_ROLE):
            return
        file = item.data(FILE_ROLE)
        ctx = item.data(CTX_ROLE)
        if not isinstance(file, CloudFile) or not isinstance(ctx, BrowseContext):
            return
        self.request_children(item, file, ctx)

    def request_children(self, item: QStandardItem, file: CloudFile, ctx: BrowseContext) -> None:
        """Просит содержимое папки (вызывается из контроллера)."""
        self._counter += 1
        token = self._counter
        # Держим САМ item, а не id(): PySide6 создаёт новые обёртки Python
        # для одного и того же C++-объекта, и поиск по id() ненадёжен.
        self._pending[token] = item
        item.setData(True, LOADED_ROLE)
        item.setChild(0, QStandardItem("загрузка…"))

        def done(files: list[CloudFile]) -> None:
            self.children_loaded.emit(token, files)

        self._loader(file, ctx, done)

    def _loader(self, file: CloudFile, ctx: BrowseContext, done) -> None:
        """Переопределяется в MainWindow: реально ходит в облако."""
        done([])

    def handle_children_loaded(self, token: int, files: list[CloudFile]) -> None:
        parent = self._pending.pop(token, None)
        if parent is None:
            return
        self.set_children(parent, files)

    # ------------------------------------------------------------------
    def _on_double_clicked(self, index) -> None:
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        file = item.data(FILE_ROLE)
        if isinstance(file, CloudFile) and file.is_file:
            self.file_activated.emit(file)

    def _on_item_changed(self, item: QStandardItem) -> None:
        # Отметили папку — отмечаем всё внутри неё.
        if item.isCheckable() and item.checkState() in (
            Qt.CheckState.Checked,
            Qt.CheckState.Unchecked,
        ):
            self._propagate(item, item.checkState())
        self._sync_parents(item)
        self._emit_checked()

    def _propagate(self, item: QStandardItem, state) -> None:
        for row in range(item.rowCount()):
            child = item.child(row)
            if child is None:
                continue
            if child.isCheckable() and child.checkState() != state:
                child.setCheckState(state)
            self._propagate(child, state)

    def _sync_parents(self, item: QStandardItem) -> None:
        """Папка отмечена, если отмечены все её файлы."""
        parent = item.parent()
        if parent is None:
            return
        children = [parent.child(row) for row in range(parent.rowCount())]
        if not children:
            return
        states = {child.checkState() for child in children if child.isCheckable()}
        if not states:
            return
        if states == {Qt.CheckState.Checked}:
            state = Qt.CheckState.Checked
        elif states == {Qt.CheckState.Unchecked}:
            state = Qt.CheckState.Unchecked
        else:
            state = Qt.CheckState.PartiallyChecked
        if parent.checkState() != state:
            parent.setCheckState(state)

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self._model.rowCount()):
            root = self._model.item(row)
            self._set_item_checked(root, state)
        self._emit_checked()

    def _set_item_checked(self, item: QStandardItem, state) -> None:
        if item.isCheckable():
            item.setCheckState(state)
        for row in range(item.rowCount()):
            self._set_item_checked(item.child(row), state)

    def _emit_checked(self) -> None:
        self.checked_changed.emit(len(self.checked_files()))

    # ------------------------------------------------------------------
    def checked_files(self) -> list[CloudFile]:
        result: list[CloudFile] = []
        for row in range(self._model.rowCount()):
            root = self._model.item(row)
            self._collect(root, result)
        return result

    def _collect(self, item: QStandardItem, result: list[CloudFile]) -> None:
        file = item.data(FILE_ROLE)
        if isinstance(file, CloudFile):
            if item.checkState() == Qt.CheckState.Checked and file.is_file:
                result.append(file)
        for row in range(item.rowCount()):
            self._collect(item.child(row), result)

    def visible_files(self) -> list[CloudFile]:
        """Все файлы в дереве (для «отметить всё»)."""
        result: list[CloudFile] = []
        for row in range(self._model.rowCount()):
            root = self._model.item(row)
            self._collect_all(root, result)
        return result

    def _collect_all(self, item: QStandardItem, result: list[CloudFile]) -> None:
        file = item.data(FILE_ROLE)
        if isinstance(file, CloudFile) and file.is_file:
            result.append(file)
        for row in range(item.rowCount()):
            self._collect_all(item.child(row), result)
