"""Оформление интерфейса.

Правило: крупные элементы, мало кнопок, никаких серых надписей —
всё, что важно, должно читаться с одного взгляда.
"""

from __future__ import annotations

STYLESHEET = """
QMainWindow, QWidget {
    background: #f4f5f7;
    color: #1f2328;
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 14px;
}

QToolBar {
    background: #ffffff;
    border: none;
    border-bottom: 1px solid #e2e5ea;
    padding: 6px 8px;
    spacing: 6px;
}
QToolBar QToolButton {
    padding: 8px 14px;
    border-radius: 8px;
    border: 1px solid transparent;
    color: #1f2328;
}
QToolBar QToolButton:hover {
    background: #eef1f5;
    border-color: #dfe3e9;
}
QToolBar QToolButton:pressed {
    background: #e3e8ef;
}
QToolBar QToolButton[accent="true"] {
    background: #2f6fed;
    color: #ffffff;
    font-weight: 600;
}
QToolBar QToolButton[accent="true"]:hover {
    background: #2560d8;
}
QToolBar QToolButton:disabled {
    color: #9aa3ae;
}

QPushButton {
    background: #ffffff;
    border: 1px solid #d7dce3;
    border-radius: 8px;
    padding: 7px 14px;
}
QPushButton:hover { background: #f0f3f7; }
QPushButton:disabled { color: #9aa3ae; background: #f7f8fa; }
QPushButton[accent="true"] {
    background: #2f6fed; color: #fff; border-color: #2f6fed; font-weight: 600;
}
QPushButton[accent="true"]:hover { background: #2560d8; }

QTreeView, QTableWidget, QListWidget, QTextEdit, QPlainTextEdit, QLineEdit {
    background: #ffffff;
    border: 1px solid #e2e5ea;
    border-radius: 8px;
    selection-background-color: #e6efff;
    selection-color: #1f2328;
}
QTreeView::item { padding: 5px 2px; }
QTreeView::item:hover { background: #f2f5fa; }
QHeaderView::section {
    background: #fafbfc;
    border: none;
    border-bottom: 1px solid #e6e9ee;
    padding: 8px 10px;
    font-weight: 600;
    color: #5b636e;
}

QSplitter::handle { background: #e6e9ee; width: 4px; }

QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab {
    background: transparent;
    padding: 10px 18px;
    margin-right: 4px;
    border-radius: 8px;
    color: #5b636e;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #1f2328;
    font-weight: 600;
}

QStatusBar {
    background: #ffffff;
    border-top: 1px solid #e2e5ea;
    color: #5b636e;
}

QGroupBox {
    border: 1px solid #e2e5ea;
    border-radius: 10px;
    margin-top: 14px;
    padding-top: 10px;
    background: #ffffff;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #5b636e;
    font-weight: 600;
}

QProgressBar {
    border: none;
    border-radius: 5px;
    background: #e8ebf0;
    height: 10px;
    text-align: center;
}
QProgressBar::chunk { background: #2f6fed; border-radius: 5px; }

QLabel[muted="true"] { color: #6b7480; }
QLabel[heading="true"] { font-size: 17px; font-weight: 600; }
QLabel[error="true"] { color: #c0392b; }
QLabel[ok="true"] { color: #1e8e5a; }

QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 18px; height: 18px; }

QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
QScrollBar::handle:vertical { background: #c8ced7; border-radius: 6px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #aeb6c1; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 2px; }
QScrollBar::handle:horizontal { background: #c8ced7; border-radius: 6px; min-width: 30px; }

QMenu {
    background: #ffffff;
    border: 1px solid #e2e5ea;
    border-radius: 8px;
    padding: 6px;
}
QMenu::item { padding: 7px 24px 7px 12px; border-radius: 6px; }
QMenu::item:selected { background: #eef3ff; }
"""
