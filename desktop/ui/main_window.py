"""
系统监控主窗口 — Apple-inspired dark-mode system monitor.

Commercial-grade PySide6 interface with frameless window, custom title bar,
frosted-glass sidebar, and smooth metric cards.
"""

from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontDatabase,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsBlurEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpacerItem,
    QStackedWidget,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from desktop.ui.charts import MultiLineChart, SmoothLineChart
from desktop.collectors.system import SystemCollector

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

_COLORS = {
    "bg":             "#1C1C1E",
    "card":           "#2C2C2E",
    "card_border":    "#3A3A3C",
    "sidebar":        "#252528",
    "sidebar_hover":  "#3A3A3C",
    "accent":         "#0A84FF",
    "accent_dim":     "#0A64CC",
    "text":           "#FFFFFF",
    "text_secondary":  "#8E8E93",
    "text_tertiary":  "#636366",
    "green":          "#30D158",
    "orange":         "#FF9F0A",
    "red":            "#FF453A",
    "purple":         "#BF5AF2",
    "teal":           "#64D2FF",
    "titlebar":       "#1C1C1E",
    "statusbar":      "#1C1C1E",
    "divider":        "#38383A",
    "traffic_close":  "#FF5F57",
    "traffic_min":    "#FEBC2E",
    "traffic_max":    "#28C840",
}

# ---------------------------------------------------------------------------
# Shared stylesheet fragments
# ---------------------------------------------------------------------------

_CARD_RADIUS = 12
_SIDEBAR_WIDTH = 72

_SCROLLBAR_STYLE = """
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical {
    width: 6px; background: transparent; margin: 0;
}
QScrollBar::handle:vertical {
    background: #48484A; border-radius: 3px; min-height: 30px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0; background: none;
}
QScrollBar:horizontal {
    height: 6px; background: transparent; margin: 0;
}
QScrollBar::handle:horizontal {
    background: #48484A; border-radius: 3px; min-width: 30px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0; background: none;
}
"""

_TABLE_STYLE = f"""
QTableWidget {{
    background-color: {_COLORS["card"]};
    color: {_COLORS["text"]};
    border: none;
    gridline-color: {_COLORS["divider"]};
    font-size: 13px;
    selection-background-color: {_COLORS["accent"]};
    selection-color: {_COLORS["text"]};
    outline: none;
}}
QTableWidget::item {{
    padding: 6px 10px;
    border-bottom: 1px solid {_COLORS["divider"]};
}}
QTableWidget::item:hover {{
    background-color: {_COLORS["sidebar_hover"]};
}}
QHeaderView::section {{
    background-color: {_COLORS["sidebar"]};
    color: {_COLORS["text_secondary"]};
    font-size: 12px;
    font-weight: 600;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid {_COLORS["divider"]};
}}
{_SCROLLBAR_STYLE}
"""


# =========================================================================
# Helper: metric card wrapper
# =========================================================================

def create_card(title: str, widget: QWidget, parent: QWidget | None = None) -> QWidget:
    """Wrap *widget* inside a styled rounded-corner card with a title label.

    The card matches the Apple-dark design language used throughout the app.
    """
    card = QFrame(parent)
    card.setObjectName("metricCard")
    card.setStyleSheet(f"""
        QFrame#metricCard {{
            background-color: {_COLORS["card"]};
            border: 1px solid {_COLORS["card_border"]};
            border-radius: {_CARD_RADIUS}px;
        }}
    """)

    layout = QVBoxLayout(card)
    layout.setContentsMargins(18, 14, 18, 14)
    layout.setSpacing(6)

    lbl = QLabel(title)
    lbl.setStyleSheet(f"""
        color: {_COLORS["text_secondary"]};
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.5px;
        background: transparent;
        border: none;
    """)
    layout.addWidget(lbl)

    widget.setStyleSheet(widget.styleSheet() + " border: none; background: transparent;")
    layout.addWidget(widget, stretch=1)

    return card


# =========================================================================
# Traffic-light button (macOS style)
# =========================================================================

class _TrafficButton(QPushButton):
    """Tiny circular button used for close / minimize / maximize."""

    def __init__(self, color: str, hover_symbol: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = color
        self._hover_symbol = hover_symbol
        self._hovered = False
        self.setFixedSize(14, 14)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self._update_style()

    # -- events -----------------------------------------------------------

    def enterEvent(self, event):  # noqa: N802
        self._hovered = True
        self._update_style()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self._hovered = False
        self._update_style()
        super().leaveEvent(event)

    # -- internal ---------------------------------------------------------

    def _update_style(self):
        symbol = self._hover_symbol if self._hovered else ""
        self.setText(symbol)
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {self._color};
                border: none;
                border-radius: 7px;
                color: #1C1C1ECC;
                font-size: 9px;
                font-weight: bold;
                padding: 0;
            }}
            QPushButton:pressed {{
                background-color: {self._color}BB;
            }}
        """)


# =========================================================================
# Custom title bar (frameless, draggable)
# =========================================================================

class _TitleBar(QWidget):
    """Frameless custom title bar with traffic-light buttons and drag support."""

    close_clicked = Signal()
    minimize_clicked = Signal()
    maximize_clicked = Signal()

    _TITLE_BAR_HEIGHT = 38

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(self._TITLE_BAR_HEIGHT)
        self.setStyleSheet(f"background-color: {_COLORS['titlebar']}; border: none;")
        self._drag_pos: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)

        # Traffic-light buttons
        btn_close = _TrafficButton(_COLORS["traffic_close"], "✕", self)
        btn_min = _TrafficButton(_COLORS["traffic_min"], "−", self)
        btn_max = _TrafficButton(_COLORS["traffic_max"], "⤢", self)

        btn_close.clicked.connect(self.close_clicked.emit)
        btn_min.clicked.connect(self.minimize_clicked.emit)
        btn_max.clicked.connect(self.maximize_clicked.emit)

        layout.addWidget(btn_close)
        layout.addWidget(btn_min)
        layout.addWidget(btn_max)

        layout.addStretch()

        # Centred title
        self._title_label = QLabel("系统监控 — Server Monitor")
        self._title_label.setStyleSheet(f"""
            color: {_COLORS["text_secondary"]};
            font-size: 13px;
            font-weight: 500;
            background: transparent;
        """)
        self._title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._title_label)

        layout.addStretch()

        # Placeholder spacer so title stays centred
        spacer = QWidget()
        spacer.setFixedWidth(14 * 3 + 8 * 2)  # same as 3 buttons + gaps
        spacer.setStyleSheet("background: transparent;")
        layout.addWidget(spacer)

    # -- drag support -----------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):  # noqa: N802
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):  # noqa: N802
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):  # noqa: N802
        self.maximize_clicked.emit()


# =========================================================================
# Sidebar navigation
# =========================================================================

class _SidebarButton(QPushButton):
    """Single sidebar navigation item with icon + label stacked vertically."""

    def __init__(self, icon_char: str, label: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._icon_char = icon_char
        self._label = label
        self._selected = False
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setFixedSize(_SIDEBAR_WIDTH - 8, 60)
        self.setCheckable(True)
        self._apply_style()

    def set_selected(self, selected: bool):
        self._selected = selected
        self.setChecked(selected)
        self._apply_style()

    def _apply_style(self):
        bg = _COLORS["accent"] if self._selected else "transparent"
        text_color = _COLORS["text"] if self._selected else _COLORS["text_secondary"]
        self.setText(f"{self._icon_char}\n{self._label}")
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg};
                border: none;
                border-radius: 10px;
                color: {text_color};
                font-size: 11px;
                font-weight: {'600' if self._selected else '400'};
                padding: 6px 0;
            }}
            QPushButton:hover {{
                background-color: {_COLORS["accent"] if self._selected else _COLORS["sidebar_hover"]};
            }}
            QPushButton:pressed {{
                background-color: {_COLORS["accent_dim"] if self._selected else _COLORS["sidebar_hover"]};
            }}
        """)


class _Sidebar(QWidget):
    """Left sidebar with vertically stacked icon + label navigation buttons."""

    page_changed = Signal(int)

    _ITEMS = [
        ("◎", "总览"),
        ("◈", "显卡"),
        ("▤", "进程"),
        ("◉", "网络"),
        ("⚙", "设置"),
    ]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedWidth(_SIDEBAR_WIDTH)
        self.setStyleSheet(f"background-color: {_COLORS['sidebar']}; border: none;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 12, 4, 12)
        layout.setSpacing(4)

        self._buttons: list[_SidebarButton] = []
        for idx, (icon, label) in enumerate(self._ITEMS):
            btn = _SidebarButton(icon, label, self)
            btn.clicked.connect(lambda _checked, i=idx: self._on_click(i))
            layout.addWidget(btn, alignment=Qt.AlignHCenter)
            self._buttons.append(btn)

        layout.addStretch()

        # Frosted blur effect (subtle)
        blur = QGraphicsBlurEffect(self)
        blur.setBlurRadius(0.5)  # very subtle
        self.setGraphicsEffect(blur)

        # Select first by default
        self._select(0)

    def _on_click(self, index: int):
        self._select(index)
        self.page_changed.emit(index)

    def _select(self, index: int):
        for i, btn in enumerate(self._buttons):
            btn.set_selected(i == index)


# =========================================================================
# Status bar
# =========================================================================

class _StatusBar(QWidget):
    """Bottom status bar showing at-a-glance metrics."""

    _HEIGHT = 30

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(self._HEIGHT)
        self.setStyleSheet(f"""
            background-color: {_COLORS["statusbar"]};
            border-top: 1px solid {_COLORS["divider"]};
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(0)

        self._label = QLabel("加载中…")
        self._label.setStyleSheet(f"""
            color: {_COLORS["text_tertiary"]};
            font-size: 11px;
            background: transparent;
            border: none;
        """)
        layout.addWidget(self._label)
        layout.addStretch()

    # Public API ----------------------------------------------------------

    def update_metrics(
        self,
        cpu: float,
        mem: float,
        gpu: float,
        temp: float,
        agent_count: int = 0,
    ):
        parts = [
            f"Agent: {agent_count}",
            f"CPU {cpu:.1f}%",
            f"内存 {mem:.1f}%",
            f"GPU {gpu:.1f}%",
            f"{temp:.0f}°C",
        ]
        self._label.setText("  ·  ".join(parts))


# =========================================================================
# Page: 总览 (Overview)
# =========================================================================

class _OverviewPage(QWidget):
    """Dashboard page with CPU, memory, GPU, disk, and network cards."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(_SCROLLBAR_STYLE + "background: transparent; border: none;")
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        container.setStyleSheet("background: transparent; border: none;")
        grid = QGridLayout(container)
        grid.setContentsMargins(20, 20, 20, 20)
        grid.setSpacing(16)

        # Row 0 — AI Agent (full width, prominent)
        agent_inner = QWidget()
        agent_inner.setStyleSheet("background: transparent; border: none;")
        agent_lay = QVBoxLayout(agent_inner)
        agent_lay.setContentsMargins(0, 0, 0, 0)
        agent_lay.setSpacing(4)

        self.agent_count_label = QLabel("0 个运行中")
        self.agent_count_label.setStyleSheet(f"""
            color: {_COLORS["accent"]};
            font-size: 28px;
            font-weight: 700;
            background: transparent;
            border: none;
        """)
        agent_lay.addWidget(self.agent_count_label)

        self.agent_parallel_label = QLabel("可并行: +0")
        self.agent_parallel_label.setStyleSheet(f"""
            color: {_COLORS["text_secondary"]};
            font-size: 13px;
            font-weight: 500;
            background: transparent;
            border: none;
        """)
        agent_lay.addWidget(self.agent_parallel_label)

        self.agent_list_label = QLabel("无运行中的 Agent")
        self.agent_list_label.setStyleSheet(f"""
            color: {_COLORS["text_secondary"]};
            font-size: 11px;
            background: transparent;
            border: none;
        """)
        self.agent_list_label.setWordWrap(True)
        agent_lay.addWidget(self.agent_list_label)

        self.agent_card = create_card("AI Agent", agent_inner)
        grid.addWidget(self.agent_card, 0, 0, 1, 2)

        # Row 1 — CPU + Memory (side by side)
        self.cpu_chart = SmoothLineChart(
            title="处理器",
            color=QColor(_COLORS["accent"]),
        )
        self.cpu_value_label = self._big_value_label("0%", _COLORS["accent"])
        cpu_inner = self._metric_widget(self.cpu_value_label, self.cpu_chart)
        self.cpu_card = create_card("处理器", cpu_inner)
        grid.addWidget(self.cpu_card, 1, 0)

        self.mem_chart = SmoothLineChart(
            title="内存",
            color=QColor(_COLORS["green"]),
        )
        self.mem_value_label = self._big_value_label("0%", _COLORS["green"])
        mem_inner = self._metric_widget(self.mem_value_label, self.mem_chart)
        self.mem_card = create_card("内存", mem_inner)
        grid.addWidget(self.mem_card, 1, 1)

        # Row 2 — GPU (full width)
        self.gpu_chart = SmoothLineChart(
            title="显卡",
            color=QColor(_COLORS["purple"]),
        )
        self.gpu_value_label = self._big_value_label("0%", _COLORS["purple"])
        self.gpu_info_label = QLabel("—")
        self.gpu_info_label.setStyleSheet(f"""
            color: {_COLORS["text_secondary"]}; font-size: 12px;
            background: transparent; border: none;
        """)
        gpu_inner = self._metric_widget(self.gpu_value_label, self.gpu_chart, self.gpu_info_label)
        self.gpu_card = create_card("显卡", gpu_inner)
        grid.addWidget(self.gpu_card, 2, 0, 1, 2)

        # Row 3 — Disk + Network
        self.disk_chart = SmoothLineChart(
            title="磁盘",
            color=QColor(_COLORS["orange"]),
        )
        self.disk_value_label = self._big_value_label("0%", _COLORS["orange"])
        disk_inner = self._metric_widget(self.disk_value_label, self.disk_chart)
        self.disk_card = create_card("磁盘", disk_inner)
        grid.addWidget(self.disk_card, 3, 0)

        self.net_chart = MultiLineChart(
            title="网络",
            colors=[QColor(_COLORS["teal"]), QColor(_COLORS["orange"])],
            labels=["下载", "上传"],
        )
        self.net_value_label = self._big_value_label("0 KB/s", _COLORS["teal"])
        net_inner = self._metric_widget(self.net_value_label, self.net_chart)
        self.net_card = create_card("网络", net_inner)
        grid.addWidget(self.net_card, 3, 1)

        # Uniform stretch
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 0)
        grid.setRowStretch(1, 1)
        grid.setRowStretch(2, 1)
        grid.setRowStretch(3, 1)

        scroll.setWidget(container)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _big_value_label(text: str, color: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"""
            color: {color};
            font-size: 28px;
            font-weight: 700;
            background: transparent;
            border: none;
        """)
        return lbl

    @staticmethod
    def _metric_widget(
        value_label: QLabel,
        chart: QWidget,
        secondary_label: QLabel | None = None,
    ) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent; border: none;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(value_label)
        if secondary_label is not None:
            lay.addWidget(secondary_label)
        lay.addWidget(chart, stretch=1)
        return w


# =========================================================================
# Page: 显卡 (GPU detail)
# =========================================================================

class _GPUPage(QWidget):
    """Detailed GPU page with utilization chart and process table."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(_SCROLLBAR_STYLE + " background: transparent; border: none;")
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        container.setStyleSheet("background: transparent; border: none;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # GPU info header
        self.gpu_name_label = QLabel("GPU")
        self.gpu_name_label.setStyleSheet(f"""
            color: {_COLORS["text"]}; font-size: 20px; font-weight: 700;
            background: transparent; border: none;
        """)
        layout.addWidget(self.gpu_name_label)

        # Stats row
        stats_row = QHBoxLayout()
        stats_row.setSpacing(24)
        self.gpu_util_label = self._stat_label("利用率", "0%")
        self.gpu_mem_label = self._stat_label("显存", "0 / 0 MB")
        self.gpu_temp_label = self._stat_label("温度", "—")
        self.gpu_power_label = self._stat_label("功耗", "—")
        for w in (self.gpu_util_label, self.gpu_mem_label, self.gpu_temp_label, self.gpu_power_label):
            stats_row.addWidget(w)
        stats_row.addStretch()
        stats_wrapper = QWidget()
        stats_wrapper.setLayout(stats_row)
        stats_wrapper.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(stats_wrapper)

        # Charts
        self.gpu_util_chart = SmoothLineChart(title="GPU 利用率", color=QColor(_COLORS["purple"]))
        self.gpu_mem_chart = SmoothLineChart(title="显存使用", color=QColor(_COLORS["teal"]))
        charts_layout = QHBoxLayout()
        charts_layout.setSpacing(16)
        charts_layout.addWidget(create_card("GPU 利用率", self.gpu_util_chart))
        charts_layout.addWidget(create_card("显存使用", self.gpu_mem_chart))
        charts_w = QWidget()
        charts_w.setLayout(charts_layout)
        charts_w.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(charts_w, stretch=1)

        # Process table
        self.process_table = QTableWidget(0, 4)
        self.process_table.setHorizontalHeaderLabels(["PID", "进程名", "显存 (MB)", "GPU %"])
        self.process_table.horizontalHeader().setStretchLastSection(True)
        self.process_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.process_table.verticalHeader().setVisible(False)
        self.process_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.process_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.process_table.setStyleSheet(_TABLE_STYLE)
        self.process_table.setMinimumHeight(180)
        proc_card = create_card("GPU 进程", self.process_table)
        layout.addWidget(proc_card, stretch=1)

        scroll.setWidget(container)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    # helpers
    @staticmethod
    def _stat_label(title: str, value: str) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent; border: none;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet(f"color: {_COLORS['text_secondary']}; font-size: 11px; font-weight: 500; background: transparent; border: none;")
        v = QLabel(value)
        v.setObjectName("value")
        v.setStyleSheet(f"color: {_COLORS['text']}; font-size: 16px; font-weight: 700; background: transparent; border: none;")
        lay.addWidget(t)
        lay.addWidget(v)
        return w


# =========================================================================
# Page: 进程 (Process list)
# =========================================================================

class _ProcessPage(QWidget):
    """Full-screen sortable process table."""

    _COLUMNS = ["PID", "用户", "进程名", "CPU %", "内存 %", "GPU MB", "状态"]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # Header row
        header_row = QHBoxLayout()
        title = QLabel("进程列表")
        title.setStyleSheet(f"""
            color: {_COLORS["text"]}; font-size: 18px; font-weight: 700;
            background: transparent; border: none;
        """)
        header_row.addWidget(title)
        header_row.addStretch()

        self.process_count_label = QLabel("0 个进程")
        self.process_count_label.setStyleSheet(f"""
            color: {_COLORS["text_secondary"]}; font-size: 13px;
            background: transparent; border: none;
        """)
        header_row.addWidget(self.process_count_label)
        layout.addLayout(header_row)

        # Table
        self.table = QTableWidget(0, len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setMinimumSectionSize(70)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setStyleSheet(_TABLE_STYLE)
        self.table.setAlternatingRowColors(False)

        card = create_card("", self.table)
        layout.addWidget(card, stretch=1)


# =========================================================================
# Page: 网络 (Network)
# =========================================================================

class _NetworkPage(QWidget):
    """Network page with upload / download charts and stats."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(_SCROLLBAR_STYLE + " background: transparent; border: none;")
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        container.setStyleSheet("background: transparent; border: none;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # Title
        title = QLabel("网络活动")
        title.setStyleSheet(f"""
            color: {_COLORS["text"]}; font-size: 20px; font-weight: 700;
            background: transparent; border: none;
        """)
        layout.addWidget(title)

        # Stats row
        stats_row = QHBoxLayout()
        stats_row.setSpacing(32)
        self.dl_label = self._speed_label("下载速度", "0 KB/s", _COLORS["teal"])
        self.ul_label = self._speed_label("上传速度", "0 KB/s", _COLORS["orange"])
        self.total_dl_label = self._speed_label("总下载", "0 MB", _COLORS["text_secondary"])
        self.total_ul_label = self._speed_label("总上传", "0 MB", _COLORS["text_secondary"])
        for w in (self.dl_label, self.ul_label, self.total_dl_label, self.total_ul_label):
            stats_row.addWidget(w)
        stats_row.addStretch()
        sw = QWidget()
        sw.setLayout(stats_row)
        sw.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(sw)

        # Charts
        self.dl_chart = SmoothLineChart(title="下载", color=QColor(_COLORS["teal"]))
        self.ul_chart = SmoothLineChart(title="上传", color=QColor(_COLORS["orange"]))
        charts_row = QHBoxLayout()
        charts_row.setSpacing(16)
        charts_row.addWidget(create_card("下载速度", self.dl_chart))
        charts_row.addWidget(create_card("上传速度", self.ul_chart))
        cw = QWidget()
        cw.setLayout(charts_row)
        cw.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(cw, stretch=1)

        # Combined chart
        self.combined_chart = MultiLineChart(
            title="网络总览",
            colors=[QColor(_COLORS["teal"]), QColor(_COLORS["orange"])],
            labels=["下载", "上传"],
        )
        combined_card = create_card("网络总览", self.combined_chart)
        layout.addWidget(combined_card, stretch=1)

        scroll.setWidget(container)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    @staticmethod
    def _speed_label(title: str, value: str, color: str) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent; border: none;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet(f"color: {_COLORS['text_secondary']}; font-size: 11px; font-weight: 500; background: transparent; border: none;")
        v = QLabel(value)
        v.setObjectName("value")
        v.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: 700; background: transparent; border: none;")
        lay.addWidget(t)
        lay.addWidget(v)
        return w


# =========================================================================
# Page: 设置 (Settings)
# =========================================================================

class _SettingsPage(QWidget):
    """Settings page with refresh interval, theme toggle, and startup options."""

    refresh_interval_changed = Signal(int)
    theme_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(_SCROLLBAR_STYLE + " background: transparent; border: none;")
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        container.setStyleSheet("background: transparent; border: none;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(24)

        # Title
        title = QLabel("设置")
        title.setStyleSheet(f"""
            color: {_COLORS["text"]}; font-size: 20px; font-weight: 700;
            background: transparent; border: none;
        """)
        layout.addWidget(title)

        # -- Refresh interval ------------------------------------------------
        layout.addWidget(self._section_title("刷新间隔"))

        interval_card = QFrame()
        interval_card.setStyleSheet(f"""
            QFrame {{
                background-color: {_COLORS["card"]};
                border: 1px solid {_COLORS["card_border"]};
                border-radius: {_CARD_RADIUS}px;
            }}
        """)
        ic_layout = QVBoxLayout(interval_card)
        ic_layout.setContentsMargins(18, 14, 18, 14)
        ic_layout.setSpacing(10)

        slider_row = QHBoxLayout()
        self.interval_slider = QSlider(Qt.Horizontal)
        self.interval_slider.setRange(500, 5000)
        self.interval_slider.setSingleStep(100)
        self.interval_slider.setValue(1000)
        self.interval_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                background: {_COLORS["divider"]};
                height: 4px;
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {_COLORS["accent"]};
                width: 16px; height: 16px;
                margin: -6px 0;
                border-radius: 8px;
            }}
            QSlider::sub-page:horizontal {{
                background: {_COLORS["accent"]};
                border-radius: 2px;
            }}
        """)
        self.interval_value_label = QLabel("1.0 秒")
        self.interval_value_label.setFixedWidth(60)
        self.interval_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.interval_value_label.setStyleSheet(f"""
            color: {_COLORS["text"]}; font-size: 14px; font-weight: 600;
            background: transparent; border: none;
        """)
        self.interval_slider.valueChanged.connect(self._on_interval_changed)
        slider_row.addWidget(self.interval_slider)
        slider_row.addWidget(self.interval_value_label)
        ic_layout.addLayout(slider_row)

        desc = QLabel("控制系统数据的采集频率，较低的值更实时但占用更多资源。")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"""
            color: {_COLORS["text_tertiary"]}; font-size: 12px;
            background: transparent; border: none;
        """)
        ic_layout.addWidget(desc)
        layout.addWidget(interval_card)

        # -- Theme -----------------------------------------------------------
        layout.addWidget(self._section_title("外观"))

        theme_card = QFrame()
        theme_card.setStyleSheet(f"""
            QFrame {{
                background-color: {_COLORS["card"]};
                border: 1px solid {_COLORS["card_border"]};
                border-radius: {_CARD_RADIUS}px;
            }}
        """)
        tc_layout = QHBoxLayout(theme_card)
        tc_layout.setContentsMargins(18, 14, 18, 14)
        tc_layout.setSpacing(12)

        theme_label = QLabel("深色模式")
        theme_label.setStyleSheet(f"""
            color: {_COLORS["text"]}; font-size: 14px;
            background: transparent; border: none;
        """)
        tc_layout.addWidget(theme_label)
        tc_layout.addStretch()

        self.theme_toggle = QPushButton("开启")
        self.theme_toggle.setCheckable(True)
        self.theme_toggle.setChecked(True)
        self.theme_toggle.setFixedSize(52, 28)
        self.theme_toggle.setStyleSheet(f"""
            QPushButton {{
                background-color: {_COLORS["accent"]};
                border: none;
                border-radius: 14px;
                color: {_COLORS["text"]};
                font-size: 12px;
                font-weight: 600;
            }}
            QPushButton:!checked {{
                background-color: {_COLORS["divider"]};
            }}
        """)
        self.theme_toggle.clicked.connect(self._on_theme_toggle)
        tc_layout.addWidget(self.theme_toggle)
        layout.addWidget(theme_card)

        # -- Startup ---------------------------------------------------------
        layout.addWidget(self._section_title("启动选项"))

        startup_card = QFrame()
        startup_card.setStyleSheet(f"""
            QFrame {{
                background-color: {_COLORS["card"]};
                border: 1px solid {_COLORS["card_border"]};
                border-radius: {_CARD_RADIUS}px;
            }}
        """)
        sc_layout = QVBoxLayout(startup_card)
        sc_layout.setContentsMargins(18, 14, 18, 14)
        sc_layout.setSpacing(10)

        self.autostart_btn = self._toggle_row("开机自启动", False, sc_layout)
        self.minimize_btn = self._toggle_row("启动时最小化到托盘", False, sc_layout)
        layout.addWidget(startup_card)

        layout.addStretch()

        scroll.setWidget(container)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    # -- helpers ----------------------------------------------------------

    def _section_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"""
            color: {_COLORS["text_secondary"]}; font-size: 12px; font-weight: 600;
            letter-spacing: 0.5px; text-transform: uppercase;
            background: transparent; border: none;
        """)
        return lbl

    def _toggle_row(self, label_text: str, initial: bool, parent_layout: QVBoxLayout) -> QPushButton:
        row = QHBoxLayout()
        lbl = QLabel(label_text)
        lbl.setStyleSheet(f"""
            color: {_COLORS["text"]}; font-size: 14px;
            background: transparent; border: none;
        """)
        row.addWidget(lbl)
        row.addStretch()
        btn = QPushButton("开启" if initial else "关闭")
        btn.setCheckable(True)
        btn.setChecked(initial)
        btn.setFixedSize(52, 28)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {_COLORS["divider"]};
                border: none; border-radius: 14px;
                color: {_COLORS["text"]}; font-size: 12px; font-weight: 600;
            }}
            QPushButton:checked {{
                background-color: {_COLORS["accent"]};
            }}
        """)
        btn.clicked.connect(lambda checked: btn.setText("开启" if checked else "关闭"))
        row.addWidget(btn)
        parent_layout.addLayout(row)
        return btn

    @Slot(int)
    def _on_interval_changed(self, value: int):
        self.interval_value_label.setText(f"{value / 1000:.1f} 秒")
        self.refresh_interval_changed.emit(value)

    @Slot()
    def _on_theme_toggle(self):
        checked = self.theme_toggle.isChecked()
        self.theme_toggle.setText("开启" if checked else "关闭")
        self.theme_changed.emit("dark" if checked else "light")


# =========================================================================
# Main Window
# =========================================================================

class MonitorMainWindow(QMainWindow):
    """Apple-inspired frameless system monitor main window.

    Assembles the custom title bar, sidebar, stacked content pages,
    and status bar.  Connects to ``SystemCollector`` via a 1-second
    ``QTimer`` refresh loop.
    """

    _DEFAULT_SIZE = QSize(1200, 800)
    _MIN_SIZE = QSize(900, 600)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        # Frameless
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setMinimumSize(self._MIN_SIZE)
        self.resize(self._DEFAULT_SIZE)
        self.setWindowTitle("系统监控 — Server Monitor")

        # Root styling
        self.setStyleSheet(f"""
            QMainWindow {{
                background-color: {_COLORS["bg"]};
            }}
        """)

        # Central widget
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # -- Title bar -------------------------------------------------------
        self._title_bar = _TitleBar(self)
        self._title_bar.close_clicked.connect(self.close)
        self._title_bar.minimize_clicked.connect(self.showMinimized)
        self._title_bar.maximize_clicked.connect(self._toggle_maximize)
        root_layout.addWidget(self._title_bar)

        # Divider
        root_layout.addWidget(self._divider())

        # -- Body (sidebar + pages) ------------------------------------------
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._sidebar = _Sidebar(self)
        body.addWidget(self._sidebar)

        # Vertical divider
        vdiv = QFrame()
        vdiv.setFixedWidth(1)
        vdiv.setStyleSheet(f"background-color: {_COLORS['divider']}; border: none;")
        body.addWidget(vdiv)

        # Stacked pages
        self._pages = QStackedWidget(self)
        self._pages.setStyleSheet(f"background-color: {_COLORS['bg']}; border: none;")

        self._overview_page = _OverviewPage()
        self._gpu_page = _GPUPage()
        self._process_page = _ProcessPage()
        self._network_page = _NetworkPage()
        self._settings_page = _SettingsPage()

        self._pages.addWidget(self._overview_page)    # 0
        self._pages.addWidget(self._gpu_page)          # 1
        self._pages.addWidget(self._process_page)      # 2
        self._pages.addWidget(self._network_page)      # 3
        self._pages.addWidget(self._settings_page)     # 4

        body.addWidget(self._pages, stretch=1)

        body_widget = QWidget()
        body_widget.setLayout(body)
        body_widget.setStyleSheet("background: transparent; border: none;")
        root_layout.addWidget(body_widget, stretch=1)

        # Divider
        root_layout.addWidget(self._divider())

        # -- Status bar ------------------------------------------------------
        self._status_bar = _StatusBar(self)
        root_layout.addWidget(self._status_bar)

        # -- Connections -----------------------------------------------------
        self._sidebar.page_changed.connect(self._pages.setCurrentIndex)
        self._settings_page.refresh_interval_changed.connect(self._set_refresh_interval)

        # -- Data collector --------------------------------------------------
        self._collector = SystemCollector()

        # -- Refresh timer ---------------------------------------------------
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        # Initial fetch
        QTimer.singleShot(0, self._refresh)

        # -- Edge-resize support state ---------------------------------------
        self._resize_edge: Optional[Qt.Edge] = None
        self._resize_start_pos: Optional[QPoint] = None
        self._resize_start_geo = None
        self.setMouseTracking(True)
        central.setMouseTracking(True)

    # ------------------------------------------------------------------ #
    # Public helpers                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def create_card(title: str, widget: QWidget) -> QWidget:
        """Module-level ``create_card`` exposed as a static method for convenience."""
        return create_card(title, widget)

    # ------------------------------------------------------------------ #
    # Private: refresh loop                                               #
    # ------------------------------------------------------------------ #

    @Slot()
    def _refresh(self):
        """Pull latest data from the collector and push into widgets."""
        try:
            data = self._collector.collect()
        except Exception:
            return  # silently skip on transient collector errors

        # -- Agent section on overview page ----------------------------------
        agents = data.get("agents", [])
        capacity = data.get("capacity", {})

        # -- Status bar ------------------------------------------------------
        self._status_bar.update_metrics(
            cpu=data.get("cpu_percent", 0.0),
            mem=data.get("memory_percent", 0.0),
            gpu=data.get("gpu_util", 0.0),
            temp=data.get("gpu_temp", 0.0),
            agent_count=len(agents),
        )

        # -- Overview page ---------------------------------------------------
        ov = self._overview_page

        ov.agent_count_label.setText(f"{len(agents)} 个运行中")
        ov.agent_parallel_label.setText(f"可并行: +{capacity.get('recommended_parallel', 0)}")

        # Update agent list
        agent_text_parts = []
        for a in agents[:8]:  # show max 8
            gpu_str = f"{a['gpu_memory_mb']:.0f}MB" if a['gpu_memory_mb'] > 0 else "—"
            agent_text_parts.append(
                f"{a['name']}  ·  CPU {a['cpu_percent']:.1f}%  ·  "
                f"内存 {a['memory_mb']:.0f}MB  ·  GPU {gpu_str}"
            )
        ov.agent_list_label.setText("\n".join(agent_text_parts) if agent_text_parts else "无运行中的 Agent")

        cpu_pct = data.get("cpu_percent", 0.0)
        mem_pct = data.get("memory_percent", 0.0)
        gpu_pct = data.get("gpu_util", 0.0)
        disk_pct = data.get("disk_percent", 0.0)

        ov.cpu_value_label.setText(f"{cpu_pct:.1f}%")
        ov.cpu_chart.add_point(cpu_pct)

        ov.mem_value_label.setText(f"{mem_pct:.1f}%")
        ov.mem_chart.add_point(mem_pct)

        ov.gpu_value_label.setText(f"{gpu_pct:.1f}%")
        ov.gpu_chart.add_point(gpu_pct)
        gpu_name = data.get("gpu_name", "—")
        gpu_mem_used = data.get("gpu_memory_used", 0)
        gpu_mem_total = data.get("gpu_memory_total", 0)
        ov.gpu_info_label.setText(f"{gpu_name}  ·  {gpu_mem_used} / {gpu_mem_total} MB")

        ov.disk_value_label.setText(f"{disk_pct:.1f}%")
        ov.disk_chart.add_point(disk_pct)

        dl_speed = data.get("net_download_speed", 0.0)
        ul_speed = data.get("net_upload_speed", 0.0)
        ov.net_value_label.setText(self._format_speed(dl_speed))
        ov.net_chart.add_points([dl_speed, ul_speed])

        # -- GPU page --------------------------------------------------------
        gp = self._gpu_page
        gp.gpu_name_label.setText(gpu_name)
        self._set_child_value(gp.gpu_util_label, f"{gpu_pct:.1f}%")
        self._set_child_value(gp.gpu_mem_label, f"{gpu_mem_used} / {gpu_mem_total} MB")
        self._set_child_value(gp.gpu_temp_label, f"{data.get('gpu_temp', 0):.0f}°C")
        self._set_child_value(gp.gpu_power_label, f"{data.get('gpu_power', 0):.0f} W")
        gp.gpu_util_chart.add_point(gpu_pct)
        mem_pct_gpu = (gpu_mem_used / gpu_mem_total * 100) if gpu_mem_total else 0
        gp.gpu_mem_chart.add_point(mem_pct_gpu)

        gpu_procs = data.get("gpu_processes", [])
        gp.process_table.setRowCount(len(gpu_procs))
        for row, proc in enumerate(gpu_procs):
            gp.process_table.setItem(row, 0, QTableWidgetItem(str(proc.get("pid", ""))))
            gp.process_table.setItem(row, 1, QTableWidgetItem(proc.get("name", "")))
            gp.process_table.setItem(row, 2, QTableWidgetItem(str(proc.get("gpu_memory", 0))))
            gp.process_table.setItem(row, 3, QTableWidgetItem(f"{proc.get('gpu_util', 0):.1f}"))

        # -- Process page ----------------------------------------------------
        procs = data.get("processes", [])
        pp = self._process_page
        pp.process_count_label.setText(f"{len(procs)} 个进程")
        pp.table.setSortingEnabled(False)
        pp.table.setRowCount(len(procs))
        for row, proc in enumerate(procs):
            pp.table.setItem(row, 0, QTableWidgetItem(str(proc.get("pid", ""))))
            pp.table.setItem(row, 1, QTableWidgetItem(proc.get("user", "")))
            pp.table.setItem(row, 2, QTableWidgetItem(proc.get("name", "")))
            pp.table.setItem(row, 3, QTableWidgetItem(f"{proc.get('cpu_percent', 0):.1f}"))
            pp.table.setItem(row, 4, QTableWidgetItem(f"{proc.get('memory_percent', 0):.1f}"))
            pp.table.setItem(row, 5, QTableWidgetItem(str(proc.get("gpu_memory", 0))))
            pp.table.setItem(row, 6, QTableWidgetItem(proc.get("status", "")))
        pp.table.setSortingEnabled(True)

        # -- Network page ----------------------------------------------------
        np_ = self._network_page
        self._set_child_value(np_.dl_label, self._format_speed(dl_speed))
        self._set_child_value(np_.ul_label, self._format_speed(ul_speed))
        self._set_child_value(np_.total_dl_label, self._format_bytes(data.get("net_bytes_recv", 0)))
        self._set_child_value(np_.total_ul_label, self._format_bytes(data.get("net_bytes_sent", 0)))
        np_.dl_chart.add_point(dl_speed / 1024)  # chart in KB/s
        np_.ul_chart.add_point(ul_speed / 1024)
        np_.combined_chart.add_points([dl_speed / 1024, ul_speed / 1024])

    # ------------------------------------------------------------------ #
    # Private: window controls                                            #
    # ------------------------------------------------------------------ #

    @Slot()
    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    @Slot(int)
    def _set_refresh_interval(self, ms: int):
        self._timer.setInterval(ms)

    # ------------------------------------------------------------------ #
    # Private: edge-resize support for frameless window                   #
    # ------------------------------------------------------------------ #

    _EDGE_MARGIN = 6

    def _edge_at(self, pos: QPoint) -> Optional[Qt.Edge]:
        """Return the Qt.Edge the mouse is closest to, or None."""
        rect = self.rect()
        edges = Qt.Edge(0)
        if pos.x() <= self._EDGE_MARGIN:
            edges |= Qt.LeftEdge
        elif pos.x() >= rect.width() - self._EDGE_MARGIN:
            edges |= Qt.RightEdge
        if pos.y() <= self._EDGE_MARGIN:
            edges |= Qt.TopEdge
        elif pos.y() >= rect.height() - self._EDGE_MARGIN:
            edges |= Qt.BottomEdge
        return edges if edges else None

    def mousePressEvent(self, event: QMouseEvent):  # noqa: N802
        edge = self._edge_at(event.position().toPoint())
        if edge and event.button() == Qt.LeftButton:
            self._resize_edge = edge
            self._resize_start_pos = event.globalPosition().toPoint()
            self._resize_start_geo = self.geometry()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):  # noqa: N802
        if self._resize_edge and self._resize_start_pos:
            delta = event.globalPosition().toPoint() - self._resize_start_pos
            geo = self._resize_start_geo
            new_geo = geo.adjusted(0, 0, 0, 0)
            if self._resize_edge & Qt.LeftEdge:
                new_geo.setLeft(geo.left() + delta.x())
            if self._resize_edge & Qt.RightEdge:
                new_geo.setRight(geo.right() + delta.x())
            if self._resize_edge & Qt.TopEdge:
                new_geo.setTop(geo.top() + delta.y())
            if self._resize_edge & Qt.BottomEdge:
                new_geo.setBottom(geo.bottom() + delta.y())
            if new_geo.width() >= self._MIN_SIZE.width() and new_geo.height() >= self._MIN_SIZE.height():
                self.setGeometry(new_geo)
            event.accept()
        else:
            edge = self._edge_at(event.position().toPoint())
            if edge:
                if edge in (Qt.LeftEdge, Qt.RightEdge):
                    self.setCursor(Qt.SizeHorCursor)
                elif edge in (Qt.TopEdge, Qt.BottomEdge):
                    self.setCursor(Qt.SizeVerCursor)
                else:
                    # diagonal
                    if (edge & Qt.LeftEdge and edge & Qt.TopEdge) or (edge & Qt.RightEdge and edge & Qt.BottomEdge):
                        self.setCursor(Qt.SizeFDiagCursor)
                    else:
                        self.setCursor(Qt.SizeBDiagCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):  # noqa: N802
        self._resize_edge = None
        self._resize_start_pos = None
        self._resize_start_geo = None
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------ #
    # Private: formatting utilities                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_speed(bytes_per_sec: float) -> str:
        """Human-readable speed string."""
        if bytes_per_sec < 1024:
            return f"{bytes_per_sec:.0f} B/s"
        elif bytes_per_sec < 1024 * 1024:
            return f"{bytes_per_sec / 1024:.1f} KB/s"
        elif bytes_per_sec < 1024 ** 3:
            return f"{bytes_per_sec / 1024 / 1024:.2f} MB/s"
        else:
            return f"{bytes_per_sec / 1024 / 1024 / 1024:.2f} GB/s"

    @staticmethod
    def _format_bytes(total_bytes: float) -> str:
        """Human-readable total-bytes string."""
        if total_bytes < 1024:
            return f"{total_bytes:.0f} B"
        elif total_bytes < 1024 ** 2:
            return f"{total_bytes / 1024:.1f} KB"
        elif total_bytes < 1024 ** 3:
            return f"{total_bytes / 1024 ** 2:.1f} MB"
        else:
            return f"{total_bytes / 1024 ** 3:.2f} GB"

    @staticmethod
    def _set_child_value(container: QWidget, text: str):
        """Find a child QLabel named ``value`` inside *container* and set its text."""
        lbl = container.findChild(QLabel, "value")
        if lbl is not None:
            lbl.setText(text)

    @staticmethod
    def _divider() -> QFrame:
        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet(f"background-color: {_COLORS['divider']}; border: none;")
        return div
