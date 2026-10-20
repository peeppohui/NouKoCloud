"""Дерево файлов с чекбоксами.

Наверху два постоянных раздела — «Яндекс.Диск» и «Google Диск». В каждом
всегда есть «Мой диск» (его нельзя убрать), а ниже — открытые ссылки этого
облака; ссылку можно убрать из списка (правый клик или Delete).

Папки подгружаются лениво: при раскрытии папки её содержимое
запрашивается в фоновом потоке и подставляется в дерево.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QKeySequence, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..core.models import CloudFile, EntryKind, ProviderId, SourceKind
from ..providers.base import BrowseContext

log = logging.getLogger(__name__)

FILE_ROLE = Qt.ItemDataRole.UserRole
CTX_ROLE = Qt.ItemDataRole.UserRole + 1
LOADED_ROLE = Qt.ItemDataRole.UserRole + 2
IS_DIR_ROLE = Qt.ItemDataRole.UserRole + 3
GROUP_ROLE = Qt.ItemDataRole.UserRole + 4        # раздел облака: значение ProviderId
HINT_ROLE = Qt.ItemDataRole.UserRole + 5         # служебная строка («загрузка…», подсказка)
DRIVE_ROLE = Qt.ItemDataRole.UserRole + 6        # постоянный «Мой диск»: значение ProviderId

#: Разделы дерева — в этом порядке.
GROUPS: tuple[tuple[ProviderId, str], ...] = (
    (ProviderId.YANDEX, "Яндекс.Диск"),
    (ProviderId.GOOGLE, "Google Диск"),
)
MY_DRIVE = "Мой диск"
DRIVE_HINT = "раскройте, чтобы загрузить"


ICONS_DIR = Path(__file__).resolve().parent / "icons"


def _indicator_stylesheet() -> str:
    """Чекбоксы дерева: всегда видимая рамка, отмеченный — синий с галочкой."""
    check = (ICONS_DIR / "check.svg").as_posix()
    dash = (ICONS_DIR / "dash.svg").as_posix()
    return f"""
    QTreeView::indicator {{
        width: 15px; height: 15px;
        border: 1.5px solid #8f98a3; border-radius: 4px; background: #ffffff;
    }}
    QTreeView::indicator:hover {{ border-color: #2f6fed; }}
    QTreeView::indicator:checked {{
        background: #2f6fed; border-color: #2f6fed; image: url("{check}");
    }}
    QTreeView::indicator:indeterminate {{
        background: #2f6fed; border-color: #2f6fed; image: url("{dash}");
    }}
    """


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


class FileTree(QWidget):
    """Виджет дерева: слева — структура, справа — ничего."""

    file_activated = Signal(object)      # CloudFile: двойной клик или Enter
    file_selected = Signal(object)       # CloudFile: курсор встал на файл (клик, стрелки)
    checked_changed = Signal(int)        # количество отмеченных
    children_loaded = Signal(int, object)  # token, list[CloudFile]
    open_drive_requested = Signal(object)  # ProviderId: раскрыли/обновили «Мой диск»
    link_removed = Signal(object)          # BrowseContext убранной ссылки

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(["Файл", "Размер", "Изменён"])
        self._counter = 0
        self._pending: dict[int, QStandardItem] = {}   # token -> родительская папка
        self._no_autoload = False
        self._groups: dict[ProviderId, QStandardItem] = {}
        self._drives: dict[ProviderId, QStandardItem] = {}

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
        # В стиле Windows 11 вместе с нашей темой пустые чекбоксы в дереве не
        # рисуются вовсе (видна только галочка у отмеченного) — непонятно, куда
        # нажимать. Рисуем флажки сами: рамка, синий фон, белая галочка.
        self._view.setStyleSheet(_indicator_stylesheet())
        self._view.setModel(self._model)
        self._view.setHeaderHidden(False)
        self._view.setAlternatingRowColors(False)
        self._view.setUniformRowHeights(True)
        self._view.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self._view.setColumnWidth(0, 330)
        self._view.setColumnWidth(1, 90)
        self._view.setColumnWidth(2, 130)
        self._view.expanded.connect(self._on_expanded)
        # activated — двойной клик и Enter (на Windows).
        self._view.activated.connect(self._on_double_clicked)
        self._view.selectionModel().currentChanged.connect(self._on_current_changed)
        self._model.itemChanged.connect(self._on_item_changed)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._show_context_menu)
        remove = QAction("Убрать ссылку из списка", self._view)
        remove.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))
        remove.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        remove.triggered.connect(self._remove_current_link)
        self._view.addAction(remove)
        layout.addWidget(self._view, 1)

        self._build_groups()

    # ------------------------------------------------------------------
    # Разделы «Яндекс.Диск» / «Google Диск»
    # ------------------------------------------------------------------
    def _build_groups(self) -> None:
        self._groups = {}
        self._drives = {}
        for pid, title in GROUPS:
            group = QStandardItem(title)
            group.setEditable(False)
            group.setSelectable(False)
            group.setData(pid.value, GROUP_ROLE)
            font = group.font()
            font.setBold(True)
            group.setFont(font)
            group.setIcon(self._standard_icon("SP_DriveNetIcon"))
            self._model.appendRow(group)
            # Заголовок раздела — на всю ширину, без колонок «Размер»/«Изменён».
            self._view.setFirstColumnSpanned(group.row(), QModelIndex(), True)
            self._groups[pid] = group
            self._drives[pid] = self._make_drive(pid, group)
            self._view.expand(group.index())

    def _make_drive(self, pid: ProviderId, group: QStandardItem) -> QStandardItem:
        """Постоянный «Мой диск» — всегда первый в разделе, не убирается."""
        drive = QStandardItem(MY_DRIVE)
        drive.setEditable(False)
        drive.setData(pid.value, DRIVE_ROLE)
        drive.setData(BrowseContext(source=SourceKind.OAUTH, root_path="/", display_name=MY_DRIVE),
                      CTX_ROLE)
        drive.setData(False, LOADED_ROLE)
        drive.setIcon(self._standard_icon("SP_DriveHDIcon"))
        drive.setToolTip(f"Ваш {group.text()}: раскройте, чтобы загрузить; правый клик — обновить")
        group.appendRow(drive)
        self._set_hint(drive, DRIVE_HINT)
        return drive

    def _set_hint(self, item: QStandardItem, text: str) -> None:
        """Единственная служебная строка внутри item (серым)."""
        count = item.rowCount()
        if count:
            item.removeRows(0, count)
        hint = QStandardItem(text)
        hint.setEditable(False)
        hint.setSelectable(False)
        hint.setData(True, HINT_ROLE)
        hint.setForeground(QBrush(QColor("#8a929c")))
        item.appendRow(hint)

    def group(self, pid: ProviderId) -> QStandardItem:
        return self._groups[pid]

    def drive(self, pid: ProviderId) -> QStandardItem:
        return self._drives[pid]

    def set_drive_loading(self, pid: ProviderId) -> None:
        """«Мой диск» запрошен: показываем «загрузка…» и не просим повторно."""
        drive = self._drives[pid]
        drive.setData(True, LOADED_ROLE)
        self._set_hint(drive, "загрузка…")
        self._emit_checked()

    def drive_failed(self, pid: ProviderId, text: str) -> None:
        """«Мой диск» не загрузился: подсказка внутри, раскрыть можно снова."""
        drive = self._drives[pid]
        drive.setData(False, LOADED_ROLE)
        self._set_hint(drive, text)
        self._view.collapse(drive.index())
        self._emit_checked()

    def _request_drive(self, pid: ProviderId) -> None:
        self.set_drive_loading(pid)
        self.open_drive_requested.emit(pid)

    # ------------------------------------------------------------------
    # Убрать ссылку
    # ------------------------------------------------------------------
    def is_link_root(self, item: Optional[QStandardItem]) -> bool:
        """Корень открытой ссылки (его можно убрать; «Мой диск» — нельзя)."""
        if item is None or item.data(DRIVE_ROLE) or item.data(HINT_ROLE):
            return False
        parent = item.parent()
        return parent is not None and parent.data(GROUP_ROLE) is not None

    def remove_root(self, item: QStandardItem) -> None:
        """Убирает открытую ссылку из дерева (на облаке ничего не удаляется)."""
        if not self.is_link_root(item):
            return
        # Ответы на запросы содержимого этой ссылки, которые ещё в пути,
        # некуда подставлять — забываем их, иначе обращение к удалённому item.
        for token, pending in list(self._pending.items()):
            node = pending
            while node is not None:
                if node is item:
                    del self._pending[token]
                    break
                node = node.parent()
        ctx = item.data(CTX_ROLE)
        item.parent().removeRow(item.row())
        self._emit_checked()
        self.link_removed.emit(ctx)

    def _item_at_column0(self, index) -> Optional[QStandardItem]:
        if not index.isValid():
            return None
        return self._model.itemFromIndex(index.siblingAtColumn(0))

    def _remove_current_link(self) -> None:
        item = self._item_at_column0(self._view.currentIndex())
        if self.is_link_root(item):
            self.remove_root(item)

    def _show_context_menu(self, pos) -> None:
        item = self._item_at_column0(self._view.indexAt(pos))
        if item is None:
            return
        menu = QMenu(self._view)
        if self.is_link_root(item):
            action = menu.addAction("Убрать ссылку из списка")
            action.triggered.connect(lambda: self.remove_root(item))
        elif item.data(DRIVE_ROLE):
            pid = ProviderId(item.data(DRIVE_ROLE))
            action = menu.addAction("Обновить «Мой диск»")
            action.triggered.connect(lambda: self._request_drive(pid))
        else:
            return
        menu.exec(self._view.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------
    def add_root(self, title: str, ctx: BrowseContext, provider_label: str = "",
                 provider: Optional[ProviderId] = None) -> QStandardItem:
        """Добавляет открытую папку (ссылку или «Мой диск») в раздел своего облака.

        provider=None — корень верхнего уровня (как раньше; так открывают
        дерево тесты и всё, что не относится к конкретному облаку).
        """
        parent = self._groups.get(provider) if provider is not None else None
        if parent is not None and ctx.source is SourceKind.OAUTH:
            # Свой диск облака — это постоянный «Мой диск» раздела.
            drive = self._drives[provider]
            drive.setData(ctx, CTX_ROLE)
            drive.setData(True, LOADED_ROLE)
            return drive
        if parent is not None:
            # Внутри раздела «Яндекс.Диск» приставка «Яндекс.Диск · » лишняя.
            prefix = f"{parent.text()} · "
            if title.startswith(prefix):
                title = "Ссылка · " + title[len(prefix):]
        # Повторное открытие той же ссылки не должно плодить дубли.
        existing = self._find_root(title, parent, ctx)
        if existing is not None:
            return existing
        root = QStandardItem(title)
        root.setEditable(False)
        if ctx.public_key:
            root.setToolTip(f"Открыто по ссылке: {ctx.public_key}")
        root.setData(ctx, CTX_ROLE)
        root.setData(True, LOADED_ROLE)
        root.setIcon(self._icon_for_folder())
        if parent is None:
            self._model.appendRow(root)
        else:
            parent.appendRow(root)
            self._view.expand(parent.index())
        return root

    def _find_root(self, title: str, parent: Optional[QStandardItem] = None,
                   ctx: Optional[BrowseContext] = None) -> Optional[QStandardItem]:
        """Уже открытый корень: по ключу ссылки, а без ссылки («Мой диск») — по названию.

        Две разные ссылки на папки с одинаковым названием — два корня.
        """
        if parent is None:
            items = [self._model.item(row) for row in range(self._model.rowCount())]
        else:
            items = [parent.child(row) for row in range(parent.rowCount())]
        key = ctx.public_key if ctx is not None else None
        for item in items:
            if item is None or item.data(GROUP_ROLE) or item.data(HINT_ROLE) \
                    or item.data(DRIVE_ROLE):
                continue
            other = item.data(CTX_ROLE)
            other_key = other.public_key if isinstance(other, BrowseContext) else None
            if key or other_key:
                if key == other_key:
                    return item
            elif item.text() == title:
                return item
        return None

    def clear(self) -> None:
        self._model.clear()
        self._model.setHorizontalHeaderLabels(["Файл", "Размер", "Изменён"])
        self._pending.clear()
        self._build_groups()

    # ------------------------------------------------------------------
    def _icon_for_folder(self):
        return self._standard_icon("SP_DirIcon")

    @staticmethod
    def _standard_icon(name: str):
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication, QStyle

        app = QApplication.instance()
        if app is None:
            return QIcon()
        return app.style().standardIcon(getattr(QStyle.StandardPixmap, name))

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
        # Папку отметили до того, как пришло её содержимое: отмечаем и его,
        # иначе «Скачать выбранное» молча пропустит всё внутри.
        if parent_item.isCheckable() and parent_item.checkState() == Qt.CheckState.Checked:
            self._propagate(parent_item, Qt.CheckState.Checked)
            self._emit_checked()

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
        if item.data(DRIVE_ROLE):
            self._request_drive(ProviderId(item.data(DRIVE_ROLE)))
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

    def handle_children_loaded(self, token: int, files: Optional[list[CloudFile]]) -> None:
        """files=None — загрузка не удалась: папку можно раскрыть ещё раз."""
        parent = self._pending.pop(token, None)
        if parent is None:
            return
        if files is None:
            # Без флага setData вызвал бы _on_item_changed, и отмеченная папка
            # тут же запросила бы содержимое снова — бесконечный повтор.
            self._no_autoload = True
            try:
                parent.setData(False, LOADED_ROLE)
                parent.setChild(0, QStandardItem("не загрузилось — сверните и раскройте папку снова"))
            finally:
                self._no_autoload = False
            self._view.collapse(self._model.indexFromItem(parent))
            return
        self.set_children(parent, files)

    # ------------------------------------------------------------------
    def _on_double_clicked(self, index) -> None:
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        if item.data(DRIVE_ROLE):
            # Enter на «Моём диске» (двойной клик и так раскрывает его).
            if not item.data(LOADED_ROLE):
                self._request_drive(ProviderId(item.data(DRIVE_ROLE)))
            self._view.expand(index.siblingAtColumn(0))
            return
        file = item.data(FILE_ROLE)
        if isinstance(file, CloudFile) and file.is_file:
            self.file_activated.emit(file)

    def current_file(self) -> Optional[CloudFile]:
        """Файл под курсором (или None, если курсор на папке / пусто)."""
        index = self._view.currentIndex()
        item = self._model.itemFromIndex(index) if index.isValid() else None
        file = item.data(FILE_ROLE) if item is not None else None
        return file if isinstance(file, CloudFile) and file.is_file else None

    def _on_current_changed(self, current, _previous) -> None:
        item = self._model.itemFromIndex(current) if current.isValid() else None
        file = item.data(FILE_ROLE) if item is not None else None
        if isinstance(file, CloudFile) and file.is_file:
            self.file_selected.emit(file)

    def _on_item_changed(self, item: QStandardItem) -> None:
        # Отметили папку — отмечаем всё внутри неё.
        if item.isCheckable() and item.checkState() in (
            Qt.CheckState.Checked,
            Qt.CheckState.Unchecked,
        ):
            self._propagate(item, item.checkState())
        # Содержимое отмеченной папки ещё не загружено — загружаем,
        # отметки проставит set_children.
        if not self._no_autoload and item.checkState() == Qt.CheckState.Checked \
                and item.data(IS_DIR_ROLE) and not item.data(LOADED_ROLE):
            file = item.data(FILE_ROLE)
            ctx = item.data(CTX_ROLE)
            if isinstance(file, CloudFile) and isinstance(ctx, BrowseContext):
                self.request_children(item, file, ctx)
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
