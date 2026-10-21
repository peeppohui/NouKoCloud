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

from PySide6.QtCore import QModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QKeySequence, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..core.models import CloudFile, EntryKind, ProviderId, SourceKind
from ..providers.base import BrowseContext
from . import theme

log = logging.getLogger(__name__)

FILE_ROLE = Qt.ItemDataRole.UserRole
CTX_ROLE = Qt.ItemDataRole.UserRole + 1
LOADED_ROLE = Qt.ItemDataRole.UserRole + 2
IS_DIR_ROLE = Qt.ItemDataRole.UserRole + 3
GROUP_ROLE = Qt.ItemDataRole.UserRole + 4        # раздел облака: значение ProviderId
HINT_ROLE = Qt.ItemDataRole.UserRole + 5         # служебная строка («загрузка…», подсказка)
DRIVE_ROLE = Qt.ItemDataRole.UserRole + 6        # постоянный «Мой диск»: значение ProviderId
ICON_ROLE = Qt.ItemDataRole.UserRole + 7         # (иконка, цвет палитры) — перекраска при смене темы

HEADER_LABELS = ["ФАЙЛ", "РАЗМЕР", "ИЗМЕНЁН"]

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".mxf", ".mts", ".m2ts", ".webm", ".m4v",
             ".mpg", ".mpeg", ".wmv", ".flv", ".r3d", ".braw", ".3gp")
AUDIO_EXT = (".wav", ".mp3", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".aif", ".aiff")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic",
             ".dng", ".cr2", ".cr3", ".nef", ".arw", ".psd")


def file_icon_name(file: CloudFile) -> str:
    """Иконка строки: папка, видео, звук, картинка или документ."""
    if file.kind is EntryKind.FOLDER:
        return "folder"
    name = file.name.lower()
    mime = (file.mime or "").lower()
    if mime.startswith("video/") or name.endswith(VIDEO_EXT):
        return "video"
    if mime.startswith("audio/") or name.endswith(AUDIO_EXT):
        return "audio"
    if mime.startswith("image/") or name.endswith(IMAGE_EXT):
        return "image"
    return "doc"

#: Разделы дерева — в этом порядке.
GROUPS: tuple[tuple[ProviderId, str], ...] = (
    (ProviderId.YANDEX, "Яндекс.Диск"),
    (ProviderId.GOOGLE, "Google Диск"),
)
MY_DRIVE = "Мой диск"
DRIVE_HINT = "раскройте, чтобы загрузить"


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


class TreeModel(QStandardItemModel):
    """Модель дерева файлов с сортировкой по настоящим значениям.

    Стандартная QStandardItemModel сортирует по строке в колонке, поэтому
    «10 МБ» встало бы раньше «9 МБ», а дата — в виде «31.12.2025 10:00».
    Своя сортировка сравнивает размер в байтах и время изменения.

    Папки всегда сверху — как в проводнике Windows; разделы облаков и
    «Мой диск» на своих местах и не переставляются.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.sort_column = 0
        self.sort_order = Qt.SortOrder.AscendingOrder

    # ------------------------------------------------------------------
    # Сортировка списка файлов (до вставки в дерево)
    # ------------------------------------------------------------------
    @staticmethod
    def _value(file: CloudFile, column: int):
        if column == 1:
            return file.size
        if column == 2:
            return file.modified_ts
        return file.name.lower()

    def sorted_files(self, files: list[CloudFile]) -> list[CloudFile]:
        """Файлы в порядке текущей сортировки — так они встанут в дерево."""
        column, order = self.sort_column, self.sort_order
        result = sorted(files, key=lambda f: f.name.lower())
        result.sort(key=lambda f: self._value(f, column),
                    reverse=order == Qt.SortOrder.DescendingOrder)
        result.sort(key=lambda f: 0 if not f.is_file else 1)
        return result

    # ------------------------------------------------------------------
    # Сортировка уже показанных строк
    # ------------------------------------------------------------------
    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self.sort_column = column
        self.sort_order = order
        for row in range(self.rowCount()):
            self._sort_subtree(self.item(row), column, order)

    def _sort_subtree(self, item, column: int, order: Qt.SortOrder) -> None:
        if item is None:
            return
        self._sort_children(item, column, order)
        for row in range(item.rowCount()):
            self._sort_subtree(item.child(row), column, order)

    def _sort_children(self, parent, column: int, order: Qt.SortOrder) -> None:
        count = parent.rowCount()
        if count < 2:
            return
        children = [parent.child(r) for r in range(count)]
        if parent.data(GROUP_ROLE) is not None:
            ordered = self._order_links(children, order)
        else:
            ordered = self._order_files(children, column, order)
        if ordered is None:
            return
        if [self._key(children[i], column) for i in ordered] == \
                [self._key(c, column) for c in children]:
            return
        # takeRow отдаёт СПИСОК item'ов (вся строка целиком). Если взять
        # только первый item, колонки «Размер» и «Изменён» исчезнут.
        rows = [parent.takeRow(0) for _ in range(count)]
        for index in ordered:
            parent.appendRow(rows[index])

    @staticmethod
    def _order_links(children, order: Qt.SortOrder) -> list[int]:
        """В разделе облака «Мой диск» закреплён первым, ссылки — по имени."""
        fixed = [i for i, c in enumerate(children) if c.data(DRIVE_ROLE)]
        links = [i for i, c in enumerate(children) if not c.data(DRIVE_ROLE)]
        links.sort(key=lambda i: children[i].text().lower(),
                   reverse=order == Qt.SortOrder.DescendingOrder)
        return fixed + links

    def _order_files(self, children, column: int, order: Qt.SortOrder) -> Optional[list[int]]:
        """Строки-файлы: сначала по имени, потом по выбранной колонке, папки сверху."""
        files = []
        for child in children:
            file = child.data(FILE_ROLE) if child is not None else None
            if not isinstance(file, CloudFile):
                return None       # служебная строка («загрузка…», подсказка)
            files.append(file)
        result = sorted(range(len(files)), key=lambda i: files[i].name.lower())
        result.sort(key=lambda i: self._value(files[i], column),
                    reverse=order == Qt.SortOrder.DescendingOrder)
        result.sort(key=lambda i: 0 if not files[i].is_file else 1)
        return result

    def _key(self, child, column: int):
        """Ключ строки — по нему видно, что порядок уже правильный."""
        file = child.data(FILE_ROLE) if child is not None else None
        if isinstance(file, CloudFile):
            return (0 if not file.is_file else 1, self._value(file, column), file.name.lower())
        return (2, 0, child.text().lower() if child is not None else "")


class FileTree(QWidget):
    """Виджет дерева: слева — структура, справа — ничего."""

    file_activated = Signal(object)      # CloudFile: двойной клик или Enter
    file_selected = Signal(object)       # CloudFile: курсор встал на файл (клик, стрелки)
    file_hovered = Signal(object)        # CloudFile: мышь над файлом или папкой (предзагрузка)
    folder_hovered = Signal(object, object)  # CloudFile, BrowseContext: мышь над папкой
    checked_changed = Signal(int)        # количество отмеченных
    children_loaded = Signal(int, object)  # token, list[CloudFile]
    open_drive_requested = Signal(object)  # ProviderId: раскрыли/обновили «Мой диск»
    link_removed = Signal(object)          # BrowseContext убранной ссылки

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = TreeModel(self)
        self._model.setHorizontalHeaderLabels(HEADER_LABELS)
        self._counter = 0
        self._pending: dict[int, QStandardItem] = {}   # token -> родительская папка
        self._no_autoload = False
        self._groups: dict[ProviderId, QStandardItem] = {}
        self._drives: dict[ProviderId, QStandardItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setContentsMargins(20, 20, 20, 10)
        top.setSpacing(14)
        self._title = QLabel("исходники", self)
        self._title.setProperty("role", "h3")
        top.addWidget(self._title, 0, Qt.AlignmentFlag.AlignBottom)
        self._subtitle = QLabel("", self)
        self._subtitle.setProperty("muted", True)
        self._subtitle.setMinimumWidth(20)
        self._subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(self._subtitle, 1, Qt.AlignmentFlag.AlignBottom)
        self._btn_all = QPushButton("Отметить всё", self)
        self._btn_none = QPushButton("Снять всё", self)
        for btn in (self._btn_all, self._btn_none):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_all.clicked.connect(lambda: self._set_all_checked(True))
        self._btn_none.clicked.connect(lambda: self._set_all_checked(False))
        top.addWidget(self._btn_all)
        top.addWidget(self._btn_none)
        layout.addLayout(top)

        self._view = QTreeView(self)
        # Флажки рисует тема (theme.stylesheet): в стиле Windows 11 пустые
        # флажки в дереве иначе не видны вовсе — непонятно, куда нажимать.
        self._view.setModel(self._model)
        self._view.setIconSize(QSize(18, 18))
        self._view.setIndentation(22)
        self._view.setHeaderHidden(False)
        self._view.setAlternatingRowColors(False)
        self._view.setUniformRowHeights(True)
        self._view.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self._view.setColumnWidth(0, 330)
        self._view.setColumnWidth(1, 96)
        self._view.setColumnWidth(2, 132)
        self._view.header().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # Сортировку делаем сами (см. TreeModel): штатная QStandardItemModel
        # сравнивает колонки как строки, и «10 МБ» встало бы раньше «9 МБ».
        # Сортировку view выключена, но стрелку в заголовке показываем.
        self._view.header().setSectionsClickable(True)
        self._view.header().setSortIndicatorShown(True)
        self._view.header().sectionClicked.connect(self._on_section_clicked)
        self._view.header().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self._view.expanded.connect(self._on_expanded)
        # activated — двойной клик и Enter (на Windows).
        self._view.activated.connect(self._on_double_clicked)
        self._view.selectionModel().currentChanged.connect(self._on_current_changed)
        # Мышь над файлом — сигнал для предзагрузки превью (без клика).
        self._view.setMouseTracking(True)
        self._view.entered.connect(self._on_entered)
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
        theme.manager().changed.connect(self._retheme)

    def set_subtitle(self, text: str) -> None:
        """Подпись рядом с заголовком: что открыто последним."""
        self._subtitle.setText(text)
        self._subtitle.setToolTip(text)

    # --- иконки и цвета темы ---------------------------------------------
    @staticmethod
    def _set_icon(item: QStandardItem, name: str, role: str = "text") -> None:
        item.setData((name, role), ICON_ROLE)
        palette = theme.current()
        item.setIcon(theme.icon(name, getattr(palette, role), 18))

    def _retheme(self, palette) -> None:
        def walk(item: QStandardItem) -> None:
            spec = item.data(ICON_ROLE)
            if spec:
                item.setIcon(theme.icon(spec[0], getattr(palette, spec[1]), 18))
            if item.data(HINT_ROLE):
                item.setForeground(QBrush(theme.qcolor(palette.muted)))
            for row in range(item.rowCount()):
                child = item.child(row)
                if child is not None:
                    walk(child)

        for row in range(self._model.rowCount()):
            top = self._model.item(row)
            if top is not None:
                walk(top)

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
            self._set_icon(group, "cloud")
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
        self._set_icon(drive, "drive")
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
        hint.setForeground(QBrush(theme.qcolor(theme.current().muted)))
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
        self._set_icon(root, "link")
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
        self._model.setHorizontalHeaderLabels(HEADER_LABELS)
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
        # Файлы сразу встают в порядке текущей сортировки.
        for file in self._model.sorted_files(files):
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
        icon_name = file_icon_name(file)
        self._set_icon(name, icon_name, "text" if icon_name == "folder" else "muted")
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
    # Сортировка по клику на заголовок
    # ------------------------------------------------------------------
    def _on_section_clicked(self, column: int) -> None:
        """Клик по заголовку — сортируем по этой колонке, повторный — наоборот."""
        order = Qt.SortOrder.AscendingOrder
        if self._model.sort_column == column \
                and self._model.sort_order == Qt.SortOrder.AscendingOrder:
            order = Qt.SortOrder.DescendingOrder
        # Перестановка строк сбрасывает раскрытость — запоминаем и возвращаем.
        expanded = self._expanded_items()
        self._model.sort(column, order)
        self._view.header().setSortIndicator(column, order)
        for item in expanded:
            self._view.expand(self._model.indexFromItem(item))

    def _expanded_items(self) -> list[QStandardItem]:
        """Что сейчас раскрыто — чтобы после сортировки вернуть как было."""
        result = []
        for item in self._all_items():
            if self._view.isExpanded(self._model.indexFromItem(item)):
                result.append(item)
        return result

    def _all_items(self):
        def walk(item):
            yield item
            for row in range(item.rowCount()):
                child = item.child(row)
                if child is not None:
                    yield from walk(child)

        for row in range(self._model.rowCount()):
            item = self._model.item(row)
            if item is not None:
                yield from walk(item)

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

    def _on_entered(self, index) -> None:
        item = self._model.itemFromIndex(index.siblingAtColumn(0)) if index.isValid() else None
        file = item.data(FILE_ROLE) if item is not None else None
        if isinstance(file, CloudFile) and file.is_file:
            self.file_hovered.emit(file)
        elif isinstance(file, CloudFile) and not item.data(LOADED_ROLE):
            ctx = item.data(CTX_ROLE)
            if isinstance(ctx, BrowseContext):
                self.folder_hovered.emit(file, ctx)

    def neighbor_files(self, count: int = 2) -> list[CloudFile]:
        """Файлы рядом с текущим (ниже, затем выше) — их, скорее всего, откроют следующими."""
        index = self._view.currentIndex()
        item = self._model.itemFromIndex(index.siblingAtColumn(0)) if index.isValid() else None
        if item is None:
            return []
        parent = item.parent() or self._model.invisibleRootItem()
        row = item.row()
        result: list[CloudFile] = []
        for step in range(1, count + 1):
            for r in (row + step, row - step):
                if 0 <= r < parent.rowCount():
                    child = parent.child(r)
                    file = child.data(FILE_ROLE) if child is not None else None
                    if isinstance(file, CloudFile) and file.is_file:
                        result.append(file)
        return result

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

    def checked_downloads(self) -> list[tuple[CloudFile, tuple[str, ...]]]:
        """Отмеченные файлы и папки, в которые их класть.

        Отмечена папка целиком — она скачается папкой: файлы внутри неё (и во
        вложенных папках) получают путь от неё, например ("Камера A", "День 1").
        Отдельно отмеченные файлы — путь пустой: прямо в папку загрузок.
        """
        result: list[tuple[CloudFile, tuple[str, ...]]] = []
        for row in range(self._model.rowCount()):
            self._collect_dirs(self._model.item(row), (), False, result)
        return result

    def _collect_dirs(self, item: QStandardItem, path: tuple[str, ...], inside: bool,
                      result: list) -> None:
        file = item.data(FILE_ROLE)
        if isinstance(file, CloudFile):
            checked = item.checkState() == Qt.CheckState.Checked
            if file.is_file:
                if checked:
                    result.append((file, path if inside else ()))
                return
            if checked or inside:
                # Папка отмечена целиком (или лежит в такой) — она станет папкой на диске.
                path, inside = path + (file.name,), True
        for row in range(item.rowCount()):
            child = item.child(row)
            if child is not None:
                self._collect_dirs(child, path, inside, result)

    def loading(self) -> bool:
        """Какие-то папки ещё подгружаются (список отмеченного может быть неполным)."""
        return bool(self._pending)

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
