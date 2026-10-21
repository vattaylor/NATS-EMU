"""Desktop window for editing emulated signals and running the NATS server."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QLocale, QTimer, Qt
from PySide6.QtGui import QBrush, QColor, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from nats_emu.client import client_url
from nats_emu.engine import Engine
from nats_emu.model import (
    ConfigError,
    format_number,
    format_value,
    load_config,
    new_signal,
    validate_subject,
)
from nats_emu.natsbin import ensure_nats_server
from nats_emu.paths import BIN_DIR

_ROLE_PATH = Qt.ItemDataRole.UserRole
_ROLE_SIGNAL = Qt.ItemDataRole.UserRole + 1

_NUMERIC_MOTIONS = (
    ("Drift — any value in range", "any"),
    ("Jump by the change amount", "jump"),
)
_TEXT_MOTIONS = (
    ("Any value in the list", "any"),
    ("Jump to the next value", "jump"),
    ("Hold the selected value", "hold"),
)
_MOTION_HELP = {
    "numeric": {
        "any": "Each update moves up or down by a random amount inside Change by, so it can sit on any value between min and max. It wanders around the default. A range of 1 to 5 jumps by 1, 2, 3, 4, or 5.",
        "jump": "Each update jumps up or down by a random amount inside Change by, and turns around at min and max. A range of 1 to 5 jumps by 1, 2, 3, 4, or 5.",
    },
    "strings": {
        "any": "Each update picks any value from the list.",
        "jump": "Each update jumps to the next value in the list, then starts again at the top.",
        "hold": "Keeps publishing the selected value.",
    },
}

_TEXT = "#e7edf2"
_MUTED = "#9aa7b2"
_DISABLED = "#6d7a86"
_WINDOW = "#1c2128"
_PANEL = "#252b33"
_FIELD = "#1a1f26"
_LINE = "#3d4652"
_SELECTED = "#245a78"

STYLE = f"""
QMainWindow, QDialog {{ background: {_WINDOW}; color: {_TEXT}; }}
QFrame#panel {{ background: {_PANEL}; border: 1px solid {_LINE}; border-radius: 10px; }}
QLabel#title {{ font-size: 20px; font-weight: bold; color: {_TEXT}; }}
QLabel#heading {{ font-size: 15px; font-weight: bold; color: {_TEXT}; }}
QLabel#value {{ font-size: 26px; font-weight: bold; color: {_TEXT}; }}
QLabel#hint {{ color: {_MUTED}; }}
QLabel#error {{ color: #ff8f8f; }}
QLabel#state[state="running"] {{ color: #5dca8c; font-weight: bold; }}
QLabel#state[state="stopped"] {{ color: {_MUTED}; font-weight: bold; }}
QLabel#state[state="starting"] {{ color: #e0b15a; font-weight: bold; }}
QLabel#state[state="error"] {{ color: #ff8f8f; font-weight: bold; }}
QLabel#detail[state="error"] {{ color: #ff8f8f; }}
QLabel#detail {{ color: {_MUTED}; }}
QTreeWidget {{ border: none; background: {_PANEL}; alternate-background-color: #2c333c; color: {_TEXT}; }}
QTreeWidget::item {{ padding: 3px 2px; }}
QTreeWidget::item:selected {{ background: {_SELECTED}; color: #f4f7fa; }}
QHeaderView::section {{
    background: #2c333c; color: {_TEXT}; padding: 6px 4px; border: none;
    border-bottom: 1px solid {_LINE}; font-weight: bold;
}}
QPushButton {{ background: #2c333c; color: {_TEXT}; border: 1px solid {_LINE}; border-radius: 6px; padding: 6px 12px; }}
QPushButton:hover {{ background: #36404b; }}
QPushButton:disabled {{ color: {_DISABLED}; }}
QPushButton#primary {{ background: #1f8a5b; color: white; border: none; font-weight: bold; }}
QPushButton#primary:hover {{ background: #18724b; }}
QPushButton#primary:disabled {{ background: #2d5342; color: #b7c4ce; }}
QPushButton#stop {{ background: #a33b3b; color: white; border: none; font-weight: bold; }}
QPushButton#stop:hover {{ background: #8a2f2f; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QListWidget, QPlainTextEdit {{
    background: {_FIELD}; color: {_TEXT}; border: 1px solid {_LINE}; border-radius: 4px; padding: 4px;
    selection-background-color: {_SELECTED}; selection-color: #f4f7fa;
}}
QComboBox QAbstractItemView {{
    background: {_FIELD}; color: {_TEXT}; selection-background-color: {_SELECTED}; selection-color: #f4f7fa;
}}
QPlainTextEdit {{ font-family: monospace; }}
QScrollArea, QStackedWidget, QScrollArea > QWidget > QWidget {{ background: {_PANEL}; border: none; }}
QStatusBar {{ background: {_WINDOW}; color: {_MUTED}; }}
QStatusBar::item {{ border: none; }}
QToolTip {{ background: #2c333c; color: {_TEXT}; border: 1px solid {_LINE}; }}
QSplitter::handle {{ background: {_WINDOW}; }}
"""


def _restyle(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _set_state(label: QLabel, state: str) -> None:
    if label.property("state") == state:
        return
    label.setProperty("state", state)
    _restyle(label)


class NumberBox(QDoubleSpinBox):
    """Spin box that shows 0.5 rather than 0.5000."""

    def __init__(self) -> None:
        super().__init__()
        self.setLocale(QLocale.c())
        self.setDecimals(4)
        self.setRange(-1_000_000_000, 1_000_000_000)
        self.setKeyboardTracking(False)
        self.setMinimumWidth(100)

    def textFromValue(self, value: float) -> str:
        return format_number(value)


class AddSignalDialog(QDialog):
    def __init__(self, parent: QWidget, suggested: str, taken: set[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add signal")
        self._taken = taken
        self.subject = QLineEdit(suggested)
        self.subject.setPlaceholderText("vehicle.YawRate")
        self.subject.setCursorPosition(len(suggested))
        self.kind = QComboBox()
        self.kind.addItem("Number", "numeric")
        self.kind.addItem("List of strings", "strings")
        self.error = QLabel()
        self.error.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Subject", self.subject)
        form.addRow("Value type", self.kind)
        form.addRow(self.error)
        self.ok_btn = QPushButton("Add")
        self.ok_btn.setObjectName("primary")
        self.ok_btn.setDefault(True)
        cancel = QPushButton("Cancel")
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.ok_btn)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(buttons)
        self.subject.textChanged.connect(self._refresh_ok)
        self.ok_btn.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        self._refresh_ok()
        self.subject.setFocus()
        self.resize(460, 180)

    def _problem(self) -> str | None:
        text = self.subject.text().strip()
        if not text:
            return "Enter a subject, for example vehicle.YawRate"
        try:
            validate_subject(text)
        except ConfigError as exc:
            return str(exc)
        if text in self._taken:
            return "That subject already exists"
        return None

    def _refresh_ok(self) -> None:
        problem = self._problem()
        empty = not self.subject.text().strip()
        self.error.setText(problem or "")
        self.error.setObjectName("hint" if empty else "error")
        _restyle(self.error)
        self.ok_btn.setEnabled(problem is None)

    def accept(self) -> None:
        if self._problem() is not None:
            self._refresh_ok()
            return
        super().accept()

    def chosen(self) -> tuple[str, str]:
        return self.subject.text().strip(), self.kind.currentData()


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.setWindowTitle("NATS EMU")
        self.resize(1180, 780)
        self._config_path = Path(config_path)
        self.model, warning, self._autosave = load_config(self._config_path)
        self.engine = Engine(self.model)
        self._dirty = False
        self._filling = True
        self._loading_editor = False
        self._updating_tree = False
        self._filter_expand = False
        self._closing = False
        self._selected_path: str | None = None
        self._selected_subject: str | None = None
        self._expanded: set[str] | None = None
        self._items: dict[str, QTreeWidgetItem] = {}
        self._enabled_cache: dict[str, bool] = {}
        self._log_seen = 0
        self._prepare_lock = threading.Lock()
        self._preparing = False
        self._prepare_result: tuple[str, str] | None = None
        self._build()
        self._fill_connection()
        created = not self._config_path.exists()
        self._rebuild()
        self._select_initial()
        self._filling = False
        if created and self._autosave:
            self.model.save(self._config_path)
        if warning:
            QTimer.singleShot(0, lambda: self._warn_config(warning))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(100)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_if_allowed)

    def _warn_config(self, warning: str) -> None:
        QMessageBox.warning(
            self,
            "Config not loaded",
            warning + "\n\nStarting from the built-in signals. Save will overwrite the file.",
        )

    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 12, 12, 8)
        outer.setSpacing(10)
        outer.addLayout(self._build_header())

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Signal", "Value", "Kind", "Every"])
        self.tree.setColumnCount(4)
        self.tree.setAlternatingRowColors(True)
        self.tree.setAnimated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(18)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setColumnWidth(1, 120)
        self.tree.headerItem().setToolTip(
            0, "Tick a signal to publish it. A group tick turns every signal inside it on or off."
        )
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.currentItemChanged.connect(self._on_select)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemCollapsed.connect(self._on_collapsed)
        delete = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.tree)
        delete.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        delete.activated.connect(self._remove_selected)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter, for example yaw")
        self.filter_edit.textChanged.connect(self._on_filter)

        self.add_button = QPushButton("Add signal")
        self.add_button.clicked.connect(self._add_signal)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove_selected)
        self.remove_button.setEnabled(False)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._save)
        expand = QPushButton("Expand")
        expand.clicked.connect(self._expand_all)
        collapse = QPushButton("Collapse")
        collapse.clicked.connect(self._collapse_all)
        tools = QHBoxLayout()
        tools.addWidget(self.add_button)
        tools.addWidget(self.remove_button)
        tools.addWidget(self.save_button)
        tools.addStretch(1)
        tools.addWidget(expand)
        tools.addWidget(collapse)

        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(10, 10, 10, 10)
        heading = QLabel("Signals")
        heading.setObjectName("heading")
        left_layout.addWidget(heading)
        left_layout.addLayout(tools)
        left_layout.addWidget(self.filter_edit)
        left_layout.addWidget(self.tree, 1)
        left = QFrame()
        left.setObjectName("panel")
        left.setLayout(left_layout)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_placeholder())
        self.stack.addWidget(self._build_editor())
        self.stack.addWidget(self._build_group())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.stack)
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(scroll)
        right = QFrame()
        right.setObjectName("panel")
        right.setLayout(right_layout)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([540, 620])
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)

        log_heading = QLabel("Server log")
        log_heading.setObjectName("heading")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        self.log.setFixedHeight(120)
        self.log.setPlaceholderText("Server output appears here.")
        log_layout = QVBoxLayout()
        log_layout.setContentsMargins(10, 8, 10, 10)
        log_layout.addWidget(log_heading)
        log_layout.addWidget(self.log)
        log_panel = QFrame()
        log_panel.setObjectName("panel")
        log_panel.setLayout(log_layout)
        outer.addWidget(log_panel)

        self._config_label = QLabel(str(self._config_path))
        self.statusBar().addPermanentWidget(self._config_label)

    def _build_header(self) -> QVBoxLayout:
        title = QLabel("NATS EMU")
        title.setObjectName("title")
        subtitle = QLabel("Local NATS server publishing emulated signals such as vehicle.YawRate.")
        subtitle.setObjectName("hint")
        subtitle.setWordWrap(True)
        self.state_label = QLabel("Stopped")
        self.state_label.setObjectName("state")
        _set_state(self.state_label, "stopped")
        self.start_button = QPushButton("Start server")
        self.start_button.setObjectName("primary")
        self.start_button.setMinimumWidth(130)
        self.start_button.clicked.connect(self._on_start_clicked)
        titles = QVBoxLayout()
        titles.addWidget(title)
        titles.addWidget(subtitle)
        top = QHBoxLayout()
        top.addLayout(titles, 1)
        top.addWidget(self.state_label)
        top.addWidget(self.start_button)

        self.host_edit = QLineEdit()
        self.host_edit.setFixedWidth(140)
        self.host_edit.setPlaceholderText("127.0.0.1")
        self.host_edit.editingFinished.connect(self._commit_host)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setMaximumWidth(90)
        self.port_spin.valueChanged.connect(self._commit_port)
        self.payload_combo = QComboBox()
        self.payload_combo.addItem("JSON", "json")
        self.payload_combo.addItem("Raw value", "raw")
        self.payload_combo.currentIndexChanged.connect(self._commit_payload)
        self.url_edit = QLineEdit()
        self.url_edit.setReadOnly(True)
        self.stats_label = QLabel()
        self.stats_label.setObjectName("hint")
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Listen"))
        controls.addWidget(self.host_edit)
        controls.addWidget(QLabel("Port"))
        controls.addWidget(self.port_spin)
        controls.addWidget(QLabel("Payload"))
        controls.addWidget(self.payload_combo)
        controls.addWidget(QLabel("Client URL"))
        controls.addWidget(self.url_edit, 1)
        controls.addWidget(self.stats_label)
        self.detail_label = QLabel()
        self.detail_label.setObjectName("detail")
        self.detail_label.setWordWrap(True)
        header = QVBoxLayout()
        header.addLayout(top)
        header.addLayout(controls)
        header.addWidget(self.detail_label)
        return header

    def _build_placeholder(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        title = QLabel("No signal selected")
        title.setObjectName("heading")
        body = QLabel(
            "Select a signal in the tree, or add one.\n\n"
            "Each signal that is switched on is published on a NATS subject of the same name. "
            "vehicle.YawRate is a number centred on 0 that moves between -20 and +20. "
            "A signal can instead publish text from a list, such as P, R, N and D.\n\n"
            "Tick a box to turn a signal or a whole group on or off."
        )
        body.setObjectName("hint")
        body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addStretch(1)
        return page

    def _build_group(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        self.group_title = QLabel()
        self.group_title.setObjectName("heading")
        self.group_body = QLabel()
        self.group_body.setWordWrap(True)
        self.group_body.setObjectName("hint")
        layout.addWidget(self.group_title)
        layout.addWidget(self.group_body)
        layout.addStretch(1)
        return page

    def _build_editor(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        self.subject_edit = QLineEdit()
        self.subject_edit.editingFinished.connect(self._apply_editor)
        self.enabled_box = self._make_enabled_box()
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Number", "numeric")
        self.kind_combo.addItem("List of strings", "strings")
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        self.value_label = QLabel("—")
        self.value_label.setObjectName("value")
        form = QFormLayout()
        form.addRow("Subject", self.subject_edit)
        form.addRow("", self.enabled_box)
        form.addRow("Value type", self.kind_combo)
        layout.addLayout(form)
        layout.addWidget(self.value_label)

        self.set_spin = NumberBox()
        self.set_button = QPushButton("Publish this value now")
        self.set_button.clicked.connect(self._publish_now)
        self.reset_button = QPushButton("Reset to default")
        self.reset_button.clicked.connect(self._reset_current)
        actions = QHBoxLayout()
        actions.addWidget(self.set_spin)
        actions.addWidget(self.set_button)
        actions.addWidget(self.reset_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.min_spin = NumberBox()
        self.max_spin = NumberBox()
        self.default_spin = NumberBox()
        self.step_min_spin = NumberBox()
        self.step_max_spin = NumberBox()
        for box in (self.step_min_spin, self.step_max_spin):
            box.setRange(0, 1_000_000_000)
            box.setSingleStep(0.1)
            box.setToolTip(
                "Each update moves up or down by a random amount between these values. "
                "1 to 5 jumps by 1, 2, 3, 4, or 5. Set both to 0 to hold the value still."
            )
        self.unit_edit = QLineEdit()
        self.unit_edit.setPlaceholderText("unit, optional")
        self.unit_edit.editingFinished.connect(self._apply_editor)
        for box in (self.min_spin, self.max_spin, self.default_spin, self.step_min_spin, self.step_max_spin):
            box.valueChanged.connect(self._apply_editor)
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("Min"))
        range_row.addWidget(self.min_spin)
        range_row.addWidget(QLabel("Max"))
        range_row.addWidget(self.max_spin)
        range_row.addWidget(QLabel("Default (centre)"))
        range_row.addWidget(self.default_spin)
        range_row.addStretch(1)
        change_row = QHBoxLayout()
        change_row.addWidget(QLabel("Change by"))
        change_row.addWidget(self.step_min_spin)
        change_row.addWidget(QLabel("to"))
        change_row.addWidget(self.step_max_spin)
        change_row.addWidget(QLabel("Unit"))
        change_row.addWidget(self.unit_edit)
        change_row.addStretch(1)
        number_layout = QVBoxLayout()
        number_layout.addLayout(range_row)
        number_layout.addLayout(change_row)
        self.numeric_box = QWidget()
        self.numeric_box.setLayout(number_layout)
        layout.addWidget(self.numeric_box)

        self.choice_list = QListWidget()
        self.choice_list.setMaximumHeight(120)
        self.choice_input = QLineEdit()
        self.choice_input.setPlaceholderText("Add a value, or several separated by commas")
        self.choice_input.returnPressed.connect(self._add_choice)
        add_choice = QPushButton("Add")
        add_choice.clicked.connect(self._add_choice)
        remove_choice = QPushButton("Remove")
        remove_choice.clicked.connect(self._remove_choice)
        up = QPushButton("Up")
        up.clicked.connect(lambda: self._move_choice(-1))
        down = QPushButton("Down")
        down.clicked.connect(lambda: self._move_choice(1))
        choice_buttons = QHBoxLayout()
        choice_buttons.addWidget(self.choice_input, 1)
        choice_buttons.addWidget(add_choice)
        choice_buttons.addWidget(remove_choice)
        choice_buttons.addWidget(up)
        choice_buttons.addWidget(down)
        self.selected_combo = QComboBox()
        self.selected_combo.currentIndexChanged.connect(self._apply_editor)
        selected_row = QHBoxLayout()
        selected_row.addWidget(QLabel("Selected value"))
        selected_row.addWidget(self.selected_combo, 1)
        string_layout = QVBoxLayout()
        string_layout.addWidget(self.choice_list)
        string_layout.addLayout(choice_buttons)
        string_layout.addLayout(selected_row)
        self.string_box = QWidget()
        self.string_box.setLayout(string_layout)
        layout.addWidget(self.string_box)

        self.motion_combo = QComboBox()
        self.motion_combo.currentIndexChanged.connect(self._on_motion)
        self.motion_help = QLabel()
        self.motion_help.setWordWrap(True)
        self.motion_help.setObjectName("hint")
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(20, 3_600_000)
        self.interval_spin.setSingleStep(50)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setMaximumWidth(160)
        self.interval_spin.setToolTip("How often this signal is updated and published.")
        self.interval_spin.valueChanged.connect(self._apply_editor)
        timing = QFormLayout()
        timing.addRow("Movement", self.motion_combo)
        timing.addRow("", self.motion_help)
        timing.addRow("Every", self.interval_spin)
        layout.addLayout(timing)

        self.error_label = QLabel()
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        layout.addStretch(1)
        return page

    def _make_enabled_box(self) -> QCheckBox:
        box = QCheckBox("Publishing")
        box.toggled.connect(self._on_enabled_box)
        self.enabled_box = box
        return box

    def _fill_connection(self) -> None:
        self.host_edit.setText(self.model.host)
        self.port_spin.setValue(self.model.port)
        index = self.payload_combo.findData(self.model.payload)
        if index >= 0:
            self.payload_combo.setCurrentIndex(index)
        self.url_edit.setText(client_url(self.model.host, self.model.port))

    def _select_initial(self) -> None:
        if "vehicle.YawRate" in self._items:
            self._show_for_path("vehicle.YawRate")
            return
        signals = self.model.list_signals()
        if signals:
            self._show_for_path(signals[0].subject)
        else:
            self._show_placeholder()

    def _rebuild(self) -> None:
        selected = self._selected_path
        first = self._expanded is None
        self._updating_tree = True
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            self._items = {}
            for node in _nodes(self.model.list_signals()):
                self._make_item(node, None)
            if first:
                self.tree.expandToDepth(0)
                self._expanded = self._capture_expanded()
            else:
                for path in list(self._expanded or ()):
                    item = self._items.get(path)
                    if item is not None and item.childCount():
                        item.setExpanded(True)
            if selected and selected in self._items:
                self.tree.setCurrentItem(self._items[selected])
        finally:
            self.tree.blockSignals(False)
            self._updating_tree = False
        self._remember_enabled()
        if self.filter_edit.text().strip():
            self._apply_filter(self.filter_edit.text())

    def _make_item(self, node: _Node, parent: QTreeWidgetItem | None) -> QTreeWidgetItem:
        item = QTreeWidgetItem()
        item.setText(0, node.name)
        item.setData(0, _ROLE_PATH, node.path)
        item.setData(0, _ROLE_SIGNAL, node.signal is not None)
        if node.signal is not None:
            item.setText(1, format_value(node.signal))
            item.setText(2, "Text" if node.signal.kind == "strings" else "Number")
            item.setText(3, f"{node.signal.interval_ms} ms")
            item.setToolTip(0, _tip(node.signal))
        elif node.children:
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
        if parent is None:
            self.tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        self._items[node.path] = item
        for child in node.children.values():
            self._make_item(child, item)
        has_children = item.childCount() > 0
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
        if has_children:
            flags |= Qt.ItemFlag.ItemIsUserTristate
        item.setFlags(flags)
        item.setCheckState(0, self._state_for(node.path, live=True))
        return item

    def _capture_expanded(self) -> set[str]:
        found: set[str] = set()

        def walk(item: QTreeWidgetItem) -> None:
            path = item.data(0, _ROLE_PATH)
            if item.isExpanded() and path:
                found.add(path)
            for index in range(item.childCount()):
                walk(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index))
        return found

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        if self._updating_tree or self._filter_expand or self._expanded is None:
            return
        path = item.data(0, _ROLE_PATH)
        if path:
            self._expanded.add(path)

    def _on_collapsed(self, item: QTreeWidgetItem) -> None:
        if self._updating_tree or self._filter_expand or self._expanded is None:
            return
        path = item.data(0, _ROLE_PATH)
        if path:
            self._expanded.discard(path)

    def _expand_all(self) -> None:
        self._updating_tree = True
        self.tree.expandAll()
        self._updating_tree = False
        self._expanded = self._capture_expanded()

    def _collapse_all(self) -> None:
        self._updating_tree = True
        self.tree.collapseAll()
        self._updating_tree = False
        self._expanded = set()

    def _on_filter(self, text: str) -> None:
        if not text.strip():
            self._rebuild()
            return
        self._apply_filter(text)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        self._filter_expand = True
        try:
            def walk(item: QTreeWidgetItem) -> bool:
                path = str(item.data(0, _ROLE_PATH) or "").lower()
                matched = needle in path or needle in item.text(0).lower()
                child_matched = False
                for index in range(item.childCount()):
                    if walk(item.child(index)):
                        child_matched = True
                visible = matched or child_matched
                item.setHidden(not visible)
                if child_matched:
                    item.setExpanded(True)
                return visible

            for index in range(self.tree.topLevelItemCount()):
                walk(self.tree.topLevelItem(index))
        finally:
            self._filter_expand = False

    def _show_for_path(self, path: str) -> None:
        item = self._items.get(path)
        if item is None:
            self._show_placeholder()
            return
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            if self._expanded is not None:
                expanded_path = parent.data(0, _ROLE_PATH)
                if expanded_path:
                    self._expanded.add(expanded_path)
            parent = parent.parent()
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        if item.data(0, _ROLE_SIGNAL):
            self._show_signal(path)
        else:
            self._show_group(path)

    def _on_select(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        if self._updating_tree:
            return
        if current is None:
            self._show_placeholder()
            return
        path = current.data(0, _ROLE_PATH)
        if current.data(0, _ROLE_SIGNAL):
            self._show_signal(path)
        else:
            self._show_group(path)

    def _show_placeholder(self) -> None:
        self._selected_path = None
        self._selected_subject = None
        self.stack.setCurrentIndex(0)
        self.remove_button.setEnabled(False)

    def _show_group(self, path: str) -> None:
        self._selected_path = path
        self._selected_subject = None
        signals = self.model.signals_under(path)
        publishing = sum(1 for sig in signals if sig.enabled)
        self.group_title.setText(path)
        self.group_body.setText(
            f"{len(signals)} signals, {publishing} publishing.\n\n"
            "Tick the box beside this group to turn all of them on or off. "
            "Expand it to set the range, the movement, and how often each signal is published."
        )
        self.stack.setCurrentIndex(2)
        self.remove_button.setEnabled(True)

    def _show_signal(self, subject: str) -> None:
        self._load_editor(subject)

    def _load_editor(self, subject: str) -> None:
        sig = self.model.get(subject)
        if sig is None:
            self._show_placeholder()
            return
        self._selected_subject = subject
        self._selected_path = subject
        self._loading_editor = True
        try:
            self.subject_edit.setText(sig.subject)
            self.enabled_box.setChecked(sig.enabled)
            self.kind_combo.setCurrentIndex(0 if sig.kind == "numeric" else 1)
            self._toggle_kind_sections(sig.motion)
            self.min_spin.setValue(sig.minimum)
            self.max_spin.setValue(sig.maximum)
            self.default_spin.setValue(sig.default)
            self.step_min_spin.setValue(sig.step_min)
            self.step_max_spin.setValue(sig.step_max)
            self.unit_edit.setText(sig.unit)
            self.interval_spin.setValue(sig.interval_ms)
            self.choice_list.clear()
            self.choice_list.addItems(sig.choices)
            self._refill_selected(sig.selected)
            if sig.kind == "numeric":
                self.set_spin.setRange(sig.minimum, sig.maximum)
                self.set_spin.setValue(float(sig.current))
            self.value_label.setText(format_value(sig) if sig.enabled else f"{format_value(sig)}   ·   off")
            self.error_label.setText("")
        finally:
            self._loading_editor = False
        self.stack.setCurrentIndex(1)
        self.remove_button.setEnabled(True)

    def _toggle_kind_sections(self, motion: str | None = None) -> None:
        numeric = self.kind_combo.currentData() == "numeric"
        if motion is None:
            motion = self.motion_combo.currentData()
        self.numeric_box.setVisible(numeric)
        self.string_box.setVisible(not numeric)
        self.set_spin.setVisible(numeric)
        self._fill_motion(numeric, motion)
        self.reset_button.setText("Reset to default" if numeric else "Reset to selected value")

    def _fill_motion(self, numeric: bool, current: str | None) -> None:
        self.motion_combo.blockSignals(True)
        self.motion_combo.clear()
        for label, data in (_NUMERIC_MOTIONS if numeric else _TEXT_MOTIONS):
            self.motion_combo.addItem(label, data)
        index = 0
        for row in range(self.motion_combo.count()):
            if self.motion_combo.itemData(row) == current:
                index = row
                break
        self.motion_combo.setCurrentIndex(index)
        self.motion_combo.blockSignals(False)
        self._update_motion_help()

    def _update_motion_help(self) -> None:
        kind = "numeric" if self.kind_combo.currentData() == "numeric" else "strings"
        motion = self.motion_combo.currentData() or "any"
        self.motion_help.setText(_MOTION_HELP[kind].get(motion, ""))

    def _on_kind_changed(self) -> None:
        if self._loading_editor or not self._selected_subject:
            return
        if self.kind_combo.currentData() == "strings" and self.choice_list.count() == 0:
            self.choice_list.addItems(["Off", "On"])
            self._refill_selected("Off")
        self._toggle_kind_sections()
        if not self._apply_editor():
            message = self.error_label.text()
            self._load_editor(self._selected_subject)
            self.error_label.setText(message)

    def _on_motion(self) -> None:
        if self._loading_editor:
            return
        self._update_motion_help()
        self._apply_editor()

    def _on_enabled_box(self, checked: bool) -> None:
        if self._loading_editor or not self._selected_subject:
            return
        try:
            self.model.update(self._selected_subject, enabled=checked)
        except (ConfigError, KeyError) as exc:
            self.error_label.setText(str(exc))
            return
        self._remember_enabled()
        self._sync_checks()
        self._schedule_save()

    def _collect(self, subject: str) -> tuple[dict, list[str]]:
        fields: dict = {}
        errors: list[str] = []
        fields["enabled"] = self.enabled_box.isChecked()
        kind = self.kind_combo.currentData()
        fields["kind"] = kind
        fields["interval_ms"] = int(self.interval_spin.value())
        fields["motion"] = self.motion_combo.currentData()
        typed = self.subject_edit.text().strip()
        if typed != subject:
            try:
                validate_subject(typed)
            except ConfigError as exc:
                errors.append(str(exc))
            else:
                fields["subject"] = typed
        if kind == "numeric":
            minimum = self.min_spin.value()
            maximum = self.max_spin.value()
            default = self.default_spin.value()
            if minimum > maximum:
                errors.append("Min cannot be above max.")
            elif not (minimum <= default <= maximum):
                errors.append("Default must be between min and max.")
            else:
                change_from = self.step_min_spin.value()
                change_to = self.step_max_spin.value()
                if change_from > change_to:
                    errors.append("Change from cannot be above change to.")
                else:
                    fields["minimum"] = minimum
                    fields["maximum"] = maximum
                    fields["default"] = default
                    fields["step_min"] = change_from
                    fields["step_max"] = change_to
                    fields["unit"] = self.unit_edit.text().strip()
        else:
            choices = [self.choice_list.item(i).text() for i in range(self.choice_list.count())]
            if not choices:
                errors.append("Add at least one text value.")
            else:
                fields["choices"] = choices
                selected = self.selected_combo.currentText()
                fields["selected"] = selected if selected in choices else choices[0]
                fields["unit"] = ""
        return fields, errors

    def _apply_editor(self) -> bool:
        if self._loading_editor or self._filling or not self._selected_subject:
            return False
        subject = self._selected_subject
        fields, errors = self._collect(subject)
        if errors:
            self.error_label.setText(errors[0])
            return False
        new_subject = str(fields.get("subject", subject))
        try:
            sig = self.model.update(subject, **fields)
        except (ConfigError, KeyError) as exc:
            self.error_label.setText(str(exc))
            return False
        self.error_label.setText("")
        self._schedule_save()
        if new_subject != subject:
            self._selected_subject = new_subject
            self._selected_path = new_subject
            self._rebuild()
            self._show_for_path(new_subject)
        else:
            self._patch_row(sig)
            if sig.kind == "numeric":
                self.set_spin.setRange(sig.minimum, sig.maximum)
        return True

    def _patch_row(self, sig) -> None:
        item = self._items.get(sig.subject)
        if item is None:
            return
        self._updating_tree = True
        try:
            item.setText(0, sig.subject.split(".")[-1])
            item.setText(2, "Text" if sig.kind == "strings" else "Number")
            item.setText(3, f"{sig.interval_ms} ms")
            item.setToolTip(0, _tip(sig))
        finally:
            self._updating_tree = False

    def _refill_selected(self, select: str | None) -> None:
        choices = [self.choice_list.item(i).text() for i in range(self.choice_list.count())]
        self.selected_combo.blockSignals(True)
        self.selected_combo.clear()
        self.selected_combo.addItems(choices)
        if select in choices:
            self.selected_combo.setCurrentText(select)
        self.selected_combo.blockSignals(False)

    def _add_choice(self) -> None:
        import re

        parts = [part.strip() for part in re.split(r"[\n,]", self.choice_input.text()) if part.strip()]
        if not parts:
            return
        existing = {self.choice_list.item(i).text() for i in range(self.choice_list.count())}
        for part in parts:
            if part not in existing and len(existing) < 100 and len(part) <= 80:
                self.choice_list.addItem(part)
                existing.add(part)
        self.choice_input.clear()
        self._refill_selected(self.selected_combo.currentText() or (parts[0] if parts else None))
        self._apply_editor()

    def _remove_choice(self) -> None:
        row = self.choice_list.currentRow()
        if row < 0:
            return
        if self.choice_list.count() <= 1:
            self.error_label.setText("Keep at least one value.")
            return
        self.choice_list.takeItem(row)
        self._refill_selected(self.selected_combo.currentText())
        self._apply_editor()

    def _move_choice(self, delta: int) -> None:
        row = self.choice_list.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self.choice_list.count():
            return
        item = self.choice_list.takeItem(row)
        self.choice_list.insertItem(target, item)
        self.choice_list.setCurrentRow(target)
        self._refill_selected(self.selected_combo.currentText())
        self._apply_editor()

    def _publish_now(self) -> None:
        subject = self._selected_subject
        if not subject:
            return
        value = self.set_spin.value() if self.kind_combo.currentData() == "numeric" else self.selected_combo.currentText()
        try:
            sig = self.model.set_current(subject, value)
        except (ConfigError, KeyError) as exc:
            self.error_label.setText(str(exc))
            return
        self.error_label.setText("")
        self.value_label.setText(format_value(sig) if sig.enabled else f"{format_value(sig)}   ·   off")

    def _reset_current(self) -> None:
        subject = self._selected_subject
        if not subject:
            return
        try:
            sig = self.model.reset_current(subject)
        except KeyError as exc:
            self.error_label.setText(str(exc))
            return
        if sig.kind == "numeric":
            self.set_spin.setValue(float(sig.current))
        self.value_label.setText(format_value(sig) if sig.enabled else f"{format_value(sig)}   ·   off")

    def _add_signal(self) -> None:
        suggested = "vehicle."
        item = self.tree.currentItem()
        if item is not None:
            path = item.data(0, _ROLE_PATH) or ""
            if item.data(0, _ROLE_SIGNAL) and "." in path:
                suggested = path.rsplit(".", 1)[0] + "."
            elif path and not item.data(0, _ROLE_SIGNAL):
                suggested = path + "."
        taken = {sig.subject for sig in self.model.list_signals()}
        dialog = AddSignalDialog(self, suggested, taken)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        subject, kind = dialog.chosen()
        try:
            self.model.add(new_signal(subject, kind))
        except ConfigError as exc:
            QMessageBox.warning(self, "Add signal", str(exc))
            return
        self._rebuild()
        self._show_for_path(subject)
        self._schedule_save()

    def _remove_selected(self) -> None:
        path = self._selected_path
        if not path or self._updating_tree:
            return
        signals = self.model.signals_under(path)
        if not signals:
            return
        if len(signals) == 1 and signals[0].subject == path:
            question = f"Remove {path}?"
        else:
            question = f"Remove {len(signals)} signals under {path}?"
        answer = QMessageBox.question(self, "Remove signal", question)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.model.remove_under(path)
        parent = path.rsplit(".", 1)[0] if "." in path else ""
        self._selected_path = None
        self._selected_subject = None
        self._rebuild()
        if parent and parent in self._items:
            self._show_for_path(parent)
        else:
            self._show_placeholder()
        self._schedule_save()

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating_tree or column != 0:
            return
        path = item.data(0, _ROLE_PATH)
        if not path:
            return
        state = item.checkState(0)
        enabled = state != Qt.CheckState.Unchecked
        target = Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        if self._state_for(path, live=True) == target:
            return
        self.model.set_enabled_under(path, enabled)
        self._remember_enabled()
        self._sync_checks()
        self._sync_editor_enabled()
        if self._selected_path and not self._selected_subject:
            self._show_group(self._selected_path)
        self._schedule_save()

    def _sync_editor_enabled(self) -> None:
        if not self._selected_subject:
            return
        sig = self.model.get(self._selected_subject)
        if sig is None:
            return
        self._loading_editor = True
        self.enabled_box.setChecked(sig.enabled)
        self._loading_editor = False

    def _remember_enabled(self) -> None:
        self._enabled_cache = {subject: flag for subject, _text, flag in self.model.snapshot_rows()}

    def _state_for(self, path: str, *, live: bool = False) -> Qt.CheckState:
        if live or not self._enabled_cache:
            flags = [sig.enabled for sig in self.model.list_signals() if sig.subject == path or sig.subject.startswith(path + ".")]
        else:
            flags = [
                flag
                for subject, flag in self._enabled_cache.items()
                if subject == path or subject.startswith(path + ".")
            ]
        if not flags or not any(flags):
            return Qt.CheckState.Unchecked
        if all(flags):
            return Qt.CheckState.Checked
        return Qt.CheckState.PartiallyChecked

    def _sync_checks(self) -> None:
        self.tree.blockSignals(True)
        self._updating_tree = True
        try:
            def walk(item: QTreeWidgetItem) -> None:
                path = item.data(0, _ROLE_PATH)
                state = self._state_for(path)
                if item.checkState(0) != state:
                    item.setCheckState(0, state)
                for index in range(item.childCount()):
                    walk(item.child(index))

            for index in range(self.tree.topLevelItemCount()):
                walk(self.tree.topLevelItem(index))
        finally:
            self._updating_tree = False
            self.tree.blockSignals(False)

    def _refresh_tree(self, rows: list[tuple[str, str, bool]]) -> None:
        enabled = {subject: flag for subject, _text, flag in rows}
        if enabled != self._enabled_cache:
            self._enabled_cache = enabled
            self._sync_checks()
        self._updating_tree = True
        try:
            for subject, text, flag in rows:
                item = self._items.get(subject)
                if item is None:
                    continue
                if item.text(1) != text:
                    item.setText(1, text)
                color = QColor(_TEXT if flag else _DISABLED)
                brush = QBrush(color)
                item.setForeground(0, brush)
                item.setForeground(1, brush)
            self._paint_groups()
        finally:
            self._updating_tree = False

    def _paint_groups(self) -> None:
        def walk(item: QTreeWidgetItem) -> None:
            if item.childCount() == 0:
                return
            path = item.data(0, _ROLE_PATH)
            flags = [
                flag
                for subject, flag in self._enabled_cache.items()
                if subject == path or subject.startswith(path + ".")
            ]
            color = QColor(_DISABLED if flags and not any(flags) else _TEXT)
            item.setForeground(0, QBrush(color))
            for index in range(item.childCount()):
                walk(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index))

    def _commit_host(self) -> None:
        if self._filling:
            return
        try:
            self.model.set_connection(host=self.host_edit.text())
        except ConfigError as exc:
            self.detail_label.setText(str(exc))
            _set_state(self.detail_label, "error")
            self.host_edit.setText(self.model.host)
            return
        self._refresh_url()
        self._schedule_save()

    def _commit_port(self) -> None:
        if self._filling:
            return
        self.model.set_connection(port=self.port_spin.value())
        self._refresh_url()
        self._schedule_save()

    def _commit_payload(self) -> None:
        if self._filling:
            return
        self.model.set_connection(payload=self.payload_combo.currentData())
        self._schedule_save()

    def _refresh_url(self) -> None:
        self.url_edit.setText(client_url(self.model.host, self.model.port))

    def _schedule_save(self) -> None:
        if self._filling:
            return
        self._dirty = True
        if self._autosave:
            self._save_timer.start(400)

    def _save_if_allowed(self) -> None:
        if self._autosave and self._dirty:
            self._save()

    def _save(self) -> None:
        try:
            if not self._filling:
                self.model.set_connection(
                    host=self.host_edit.text(),
                    port=self.port_spin.value(),
                    payload=self.payload_combo.currentData(),
                )
            self.model.save(self._config_path)
        except ConfigError as exc:
            self.detail_label.setText(str(exc))
            _set_state(self.detail_label, "error")
            return
        self._dirty = False
        self._autosave = True
        self.statusBar().showMessage(f"Saved {self._config_path.name}", 2000)

    def _on_start_clicked(self) -> None:
        snap = self.engine.snapshot()
        if self._preparing:
            return
        if snap["state"] in {"starting", "running"}:
            self.engine.stop()
            self._poll()
            return
        try:
            self.model.set_connection(host=self.host_edit.text(), port=self.port_spin.value())
        except ConfigError as exc:
            self.detail_label.setText(str(exc))
            _set_state(self.detail_label, "error")
            return
        with self._prepare_lock:
            self._preparing = True
            self._prepare_result = None
        self._poll()

        def work() -> None:
            try:
                path = ensure_nats_server(BIN_DIR, status=self.engine.add_log)
                result = ("ok", str(path))
            except Exception as exc:
                result = ("err", str(exc))
            with self._prepare_lock:
                self._prepare_result = result
                self._preparing = False

        threading.Thread(target=work, name="nats-emu-prepare", daemon=True).start()

    def _collect_prepare(self) -> bool:
        with self._prepare_lock:
            preparing = self._preparing
            result = self._prepare_result
            self._prepare_result = None
        if result is not None and not self._closing:
            kind, payload = result
            if kind == "ok":
                self.engine.start(Path(payload))
            else:
                self.engine.fail(payload)
            preparing = False
        return preparing

    def _poll(self) -> None:
        preparing = self._collect_prepare()
        snap = self.engine.snapshot()
        state = "starting" if preparing else str(snap["state"])
        labels = {
            "running": "Running",
            "starting": "Starting",
            "error": "Error",
            "stopped": "Stopped",
        }
        self.state_label.setText(labels.get(state, state))
        _set_state(self.state_label, state if state in labels else "stopped")
        running = state in {"running", "starting"}
        self.start_button.setText("Stop server" if state == "running" else ("Starting…" if state == "starting" else "Start server"))
        self.start_button.setEnabled(state != "starting")
        _restyle_button(self.start_button, "stop" if state == "running" else "primary")
        self.host_edit.setEnabled(not running)
        self.port_spin.setEnabled(not running)
        if state == "running" and snap["url"]:
            self.url_edit.setText(str(snap["url"]))
        elif not self.host_edit.hasFocus():
            self.url_edit.setText(client_url(self.model.host, self.model.port))
        hints = {
            "running": "Clients can subscribe to a signal name, for example vehicle.YawRate.",
            "stopped": "Start the server, then subscribe to a signal name such as vehicle.YawRate.",
            "starting": "Waiting for the NATS server.",
            "error": str(snap["detail"]),
        }
        self.detail_label.setText(hints.get(state, ""))
        _set_state(self.detail_label, "error" if state == "error" else "")
        rows = self.model.snapshot_rows()
        publishing = sum(1 for _subject, _text, enabled in rows if enabled)
        if state == "running":
            self.stats_label.setText(f"{publishing} publishing · {float(snap['rate']):.0f} msg/s")
        else:
            self.stats_label.setText(f"{publishing} of {len(rows)} switched on")
        self._refresh_tree(rows)
        self._refresh_value(rows)
        self._drain_logs()

    def _refresh_value(self, rows: list[tuple[str, str, bool]]) -> None:
        subject = self._selected_subject
        if not subject:
            return
        for name, text, enabled in rows:
            if name == subject:
                self.value_label.setText(text if enabled else f"{text}   ·   off")
                return

    def _drain_logs(self) -> None:
        lines = self.engine.logs_since(self._log_seen)
        if not lines:
            return
        self._log_seen = lines[-1][0]
        bar = self.log.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4
        for _number, line in lines:
            self.log.appendPlainText(line)
        if follow:
            bar.setValue(bar.maximum())

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt name
        if self._dirty and not self._autosave:
            answer = QMessageBox.question(
                self,
                "Save signals",
                "Save these signals over the existing config file?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.StandardButton.Yes:
                self._autosave = True
                self._save()
        elif self._autosave and self._dirty:
            self._save()
        self._closing = True
        self._timer.stop()
        self.engine.stop()
        event.accept()


def _restyle_button(button: QPushButton, name: str) -> None:
    if button.objectName() == name:
        return
    button.setObjectName(name)
    _restyle(button)


class _Node:
    def __init__(self, name: str, path: str, signal) -> None:
        self.name = name
        self.path = path
        self.signal = signal
        self.children: dict[str, _Node] = {}


def _nodes(signals) -> list[_Node]:
    root = _Node("", "", None)
    for sig in signals:
        node = root
        walked: list[str] = []
        parts = sig.subject.split(".")
        for index, part in enumerate(parts):
            walked.append(part)
            path = ".".join(walked)
            child = node.children.get(part)
            if child is None:
                child = _Node(part, path, None)
                node.children[part] = child
            node = child
            if index == len(parts) - 1:
                node.signal = sig
    return list(root.children.values())


def _tip(sig) -> str:
    if sig.kind == "numeric":
        return f"{sig.subject}\n{format_number(sig.minimum)} to {format_number(sig.maximum)}, centre {format_number(sig.default)}"
    return f"{sig.subject}\n{', '.join(sig.choices)}"


def _dark_palette() -> QPalette:
    palette = QPalette()
    window = QColor(_WINDOW)
    panel = QColor(_PANEL)
    field = QColor(_FIELD)
    text = QColor(_TEXT)
    disabled = QColor(_DISABLED)
    highlight = QColor(_SELECTED)
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, field)
    palette.setColor(QPalette.ColorRole.AlternateBase, panel)
    palette.setColor(QPalette.ColorRole.ToolTipBase, panel)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, panel)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#f4f7fa"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(_MUTED))
    palette.setColor(QPalette.ColorRole.Link, QColor("#7ec8e3"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    return palette


def run_app(config_path: Path) -> int:
    app = QApplication.instance() or QApplication([])
    QLocale.setDefault(QLocale.c())
    app.setApplicationName("NATS EMU")
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(STYLE)
    window = MainWindow(Path(config_path))
    window.show()
    return app.exec()
