"""
Apple 风格自定义图表组件
========================
基于 PySide6 QPainter 绘制的高质量实时图表，适用于商业级系统监控应用。
包含两个核心组件：
  - SmoothLineChart: 单线实时平滑曲线图
  - MultiLineChart:  多线叠加曲线图

设计语言参考 macOS / iOS 系统监视器，支持深色/浅色模式自适应。
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from PySide6.QtCore import (
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Property,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QApplication, QWidget


# ---------------------------------------------------------------------------
#  工具函数
# ---------------------------------------------------------------------------

def _is_dark_mode() -> bool:
    """检测当前是否处于深色模式（基于应用调色板亮度判断）"""
    app = QApplication.instance()
    if app is None:
        return True  # 默认深色
    palette = app.palette()
    bg = palette.color(palette.ColorRole.Window)
    # 亮度低于 128 视为深色模式
    return bg.lightnessF() < 0.5


def _text_color(alpha: int = 255) -> QColor:
    """根据深浅模式返回适配的文本颜色"""
    if _is_dark_mode():
        return QColor(255, 255, 255, alpha)
    else:
        return QColor(0, 0, 0, alpha)


def _grid_color() -> QColor:
    """网格线颜色：极浅灰，几乎不可见"""
    if _is_dark_mode():
        return QColor(255, 255, 255, 18)
    else:
        return QColor(0, 0, 0, 15)


def _catmull_rom_to_bezier(
    p0: QPointF, p1: QPointF, p2: QPointF, p3: QPointF, alpha: float = 0.5
) -> Tuple[QPointF, QPointF]:
    """
    将 Catmull-Rom 样条段 (p1→p2) 转换为三次贝塞尔控制点。
    alpha=0.5 为向心参数化，曲线更平滑、无尖角。
    返回 (cp1, cp2)，即贝塞尔的两个控制点。
    """
    def _t(ti: float, pi: QPointF, pj: QPointF) -> float:
        dx = pj.x() - pi.x()
        dy = pj.y() - pi.y()
        return ti + math.pow(dx * dx + dy * dy, alpha * 0.5)

    t0 = 0.0
    t1 = _t(t0, p0, p1)
    t2 = _t(t1, p1, p2)
    t3 = _t(t2, p2, p3)

    # 避免除零
    eps = 1e-9
    d1 = max(t1 - t0, eps)
    d2 = max(t2 - t1, eps)
    d3 = max(t3 - t2, eps)
    d12 = max(t2 - t0, eps)
    d23 = max(t3 - t1, eps)

    # Catmull-Rom 切线
    m1_x = (t2 - t1) * ((p1.x() - p0.x()) / d1 - (p2.x() - p0.x()) / d12 + (p2.x() - p1.x()) / d2)
    m1_y = (t2 - t1) * ((p1.y() - p0.y()) / d1 - (p2.y() - p0.y()) / d12 + (p2.y() - p1.y()) / d2)
    m2_x = (t2 - t1) * ((p2.x() - p1.x()) / d2 - (p3.x() - p1.x()) / d23 + (p3.x() - p2.x()) / d3)
    m2_y = (t2 - t1) * ((p2.y() - p1.y()) / d2 - (p3.y() - p1.y()) / d23 + (p3.y() - p2.y()) / d3)

    cp1 = QPointF(p1.x() + m1_x / 3.0, p1.y() + m1_y / 3.0)
    cp2 = QPointF(p2.x() - m2_x / 3.0, p2.y() - m2_y / 3.0)
    return cp1, cp2


def _build_smooth_path(points: List[QPointF]) -> QPainterPath:
    """
    根据数据点列表构建 Catmull-Rom 平滑曲线路径。
    点数 < 2 时返回空路径。
    """
    path = QPainterPath()
    n = len(points)
    if n < 2:
        if n == 1:
            path.moveTo(points[0])
        return path

    path.moveTo(points[0])

    if n == 2:
        # 仅两个点，直线连接
        path.lineTo(points[1])
        return path

    # 对每段 (i, i+1) 计算贝塞尔控制点
    for i in range(n - 1):
        # 取前后各一个邻居，边界处镜像延伸
        p0 = points[max(i - 1, 0)]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[min(i + 2, n - 1)]

        cp1, cp2 = _catmull_rom_to_bezier(p0, p1, p2, p3)
        path.cubicTo(cp1, cp2, p2)

    return path


def _format_value(value: float, unit: str) -> str:
    """格式化数值显示，自动选择合适精度"""
    if abs(value) >= 1000:
        text = f"{value:,.0f}"
    elif abs(value) >= 100:
        text = f"{value:.0f}"
    elif abs(value) >= 10:
        text = f"{value:.1f}"
    else:
        text = f"{value:.2f}"
    if unit:
        text += f" {unit}"
    return text


# ---------------------------------------------------------------------------
#  SmoothLineChart — 单线实时平滑曲线图
# ---------------------------------------------------------------------------

class SmoothLineChart(QWidget):
    """
    Apple 风格单线平滑实时图表。
    特性：
      - Catmull-Rom 样条插值，曲线丝滑
      - 曲线下方渐变填充
      - 自适应 Y 轴缩放
      - 添加数据时平滑动画过渡
      - 深色/浅色模式自适应
    """

    # 动画帧率与时长
    _ANIM_FPS = 60
    _ANIM_DURATION_MS = 300  # 动画总时长（毫秒）

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        title: str = "",
        color: Optional[QColor] = None,
        unit: str = "",
        y_min: Optional[float] = None,
        y_max: Optional[float] = None,
    ) -> None:
        super().__init__(parent)

        # --- 数据 ---
        self._data: List[float] = []
        self._display_data: List[float] = []  # 动画用的插值数据
        self._prev_data: List[float] = []     # 动画起始帧数据

        # --- 外观 ---
        self._color = color if color is not None else QColor("#30D158")
        self._title = title
        self._unit = unit

        # --- Y 轴范围 ---
        self._fixed_min: Optional[float] = y_min
        self._fixed_max: Optional[float] = y_max

        # --- 动画 ---
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(int(1000 / self._ANIM_FPS))
        self._anim_timer.timeout.connect(self._on_anim_tick)
        self._anim_progress = 1.0  # 0→1，1 表示动画完成
        self._anim_step = 1.0 / max(1, self._ANIM_DURATION_MS / (1000 / self._ANIM_FPS))

        # --- 布局 ---
        self.setMinimumSize(QSize(200, 120))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    # ----- 公开接口 -----

    def set_data(self, data: List[float]) -> None:
        """设置完整数据序列，触发平滑动画过渡"""
        self._prev_data = list(self._display_data) if self._display_data else list(data)
        self._data = list(data)
        self._anim_progress = 0.0
        if not self._anim_timer.isActive():
            self._anim_timer.start()

    def set_color(self, color: QColor) -> None:
        """设置曲线颜色"""
        self._color = QColor(color)
        self.update()

    def set_title(self, title: str) -> None:
        """设置图表左上角标题"""
        self._title = title
        self.update()

    def set_y_range(self, min_val: Optional[float], max_val: Optional[float]) -> None:
        """设置固定 Y 轴范围。传 None 取消固定，启用自动缩放"""
        self._fixed_min = min_val
        self._fixed_max = max_val
        self.update()

    def set_unit(self, unit: str) -> None:
        """设置数值单位（如 '%', 'MB/s', '°C'）"""
        self._unit = unit
        self.update()

    # ----- 内部方法 -----

    def _on_anim_tick(self) -> None:
        """动画定时器回调：逐步更新 display_data"""
        self._anim_progress = min(1.0, self._anim_progress + self._anim_step)

        # 缓动函数 (ease-out cubic)，让动画更自然
        t = 1.0 - (1.0 - self._anim_progress) ** 3

        # 对齐两组数据长度（短的用最后一个值或 0 填充）
        prev = self._prev_data
        curr = self._data
        max_len = max(len(prev), len(curr))
        if max_len == 0:
            self._display_data = []
        else:
            result = []
            for i in range(max_len):
                v_prev = prev[i] if i < len(prev) else (prev[-1] if prev else 0.0)
                v_curr = curr[i] if i < len(curr) else (curr[-1] if curr else 0.0)
                result.append(v_prev + (v_curr - v_prev) * t)
            self._display_data = result

        self.update()

        if self._anim_progress >= 1.0:
            self._anim_timer.stop()
            self._display_data = list(self._data)

    def _compute_y_range(self, data: List[float]) -> Tuple[float, float]:
        """
        智能计算 Y 轴显示范围。
        - 有固定范围时优先使用，但数据集中在小区间时自动缩放以提升可读性
        - 无固定范围时根据数据自动计算，留 10% 余量
        """
        if not data:
            lo = self._fixed_min if self._fixed_min is not None else 0.0
            hi = self._fixed_max if self._fixed_max is not None else 100.0
            return (lo, hi)

        data_min = min(data)
        data_max = max(data)

        if self._fixed_min is not None and self._fixed_max is not None:
            full_range = self._fixed_max - self._fixed_min
            data_range = data_max - data_min
            # 当数据范围不到固定范围的 20% 时，缩放以突出变化细节
            if full_range > 0 and data_range < full_range * 0.20:
                margin = max(data_range * 0.5, full_range * 0.05)
                lo = max(self._fixed_min, data_min - margin)
                hi = min(self._fixed_max, data_max + margin)
                if hi - lo < full_range * 0.1:
                    mid = (data_min + data_max) / 2
                    half = full_range * 0.05
                    lo = max(self._fixed_min, mid - half)
                    hi = min(self._fixed_max, mid + half)
                return (lo, hi)
            return (self._fixed_min, self._fixed_max)

        # 自动范围：留 10% 上下余量
        if data_min == data_max:
            margin = max(abs(data_min) * 0.1, 1.0)
        else:
            margin = (data_max - data_min) * 0.1
        lo = self._fixed_min if self._fixed_min is not None else data_min - margin
        hi = self._fixed_max if self._fixed_max is not None else data_max + margin
        return (lo, hi)

    def _chart_rect(self) -> QRectF:
        """计算实际绑图区域（去除标题、标签等边距）"""
        left_margin = 42.0   # Y 轴标签空间
        right_margin = 12.0
        top_margin = 30.0    # 标题行
        bottom_margin = 8.0
        return QRectF(
            left_margin,
            top_margin,
            self.width() - left_margin - right_margin,
            self.height() - top_margin - bottom_margin,
        )

    def _data_to_points(
        self, data: List[float], rect: QRectF, y_min: float, y_max: float
    ) -> List[QPointF]:
        """将数据序列映射为绘图坐标点"""
        n = len(data)
        if n == 0:
            return []
        y_range = y_max - y_min
        if y_range == 0:
            y_range = 1.0

        points = []
        for i, val in enumerate(data):
            x = rect.left() + (i / max(1, n - 1)) * rect.width() if n > 1 else rect.center().x()
            y = rect.bottom() - ((val - y_min) / y_range) * rect.height()
            # 限制在绘图区内
            y = max(rect.top(), min(rect.bottom(), y))
            points.append(QPointF(x, y))
        return points

    # ----- 绘制 -----

    def paintEvent(self, event) -> None:  # noqa: N802
        """核心绑制逻辑"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        # 圆角裁剪
        clip_path = QPainterPath()
        clip_path.addRoundedRect(QRectF(self.rect()), 12.0, 12.0)
        painter.setClipPath(clip_path)

        # 背景透明（不绘制背景，由父组件负责）

        data = self._display_data if self._display_data else self._data
        y_min, y_max = self._compute_y_range(data)
        chart = self._chart_rect()

        self._draw_grid(painter, chart, y_min, y_max)
        self._draw_y_labels(painter, chart, y_min, y_max)
        self._draw_title(painter)
        self._draw_current_value(painter, data)
        self._draw_curve(painter, data, chart, y_min, y_max)

        painter.end()

    def _draw_grid(
        self, painter: QPainter, rect: QRectF, y_min: float, y_max: float
    ) -> None:
        """绘制水平网格线（25%、50%、75%）"""
        pen = QPen(_grid_color())
        pen.setWidthF(1.0)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([4, 6])
        painter.setPen(pen)

        for frac in (0.25, 0.50, 0.75):
            y = rect.bottom() - frac * rect.height()
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

    def _draw_y_labels(
        self, painter: QPainter, rect: QRectF, y_min: float, y_max: float
    ) -> None:
        """在左侧绘制 Y 轴刻度标签（最小值、中间值、最大值）"""
        font = QFont()
        font.setPointSizeF(9.0)
        painter.setFont(font)
        painter.setPen(_text_color(120))

        fm = QFontMetrics(font)
        label_x = 2.0
        label_w = rect.left() - 6.0

        for frac, val in [(0.0, y_min), (0.5, (y_min + y_max) / 2), (1.0, y_max)]:
            y = rect.bottom() - frac * rect.height()
            text = _format_value(val, "")
            text_rect = QRectF(label_x, y - fm.height() / 2, label_w, fm.height())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)

    def _draw_title(self, painter: QPainter) -> None:
        """左上角标题"""
        if not self._title:
            return
        font = QFont()
        font.setPointSizeF(11.0)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(_text_color(180))
        painter.drawText(QRectF(12, 6, self.width() - 24, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._title)

    def _draw_current_value(self, painter: QPainter, data: List[float]) -> None:
        """右上角显示当前（最新）值"""
        if not data:
            return
        value = data[-1]
        text = _format_value(value, self._unit)

        font = QFont()
        font.setPointSizeF(20.0)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(self._color))
        painter.drawText(
            QRectF(0, 2, self.width() - 12, 28),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            text,
        )

    def _draw_curve(
        self,
        painter: QPainter,
        data: List[float],
        rect: QRectF,
        y_min: float,
        y_max: float,
    ) -> None:
        """绘制平滑曲线及其下方渐变填充"""
        if len(data) < 2:
            # 只有一个点时画一个圆点
            if data:
                pts = self._data_to_points(data, rect, y_min, y_max)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(self._color))
                painter.drawEllipse(pts[0], 3.0, 3.0)
            return

        points = self._data_to_points(data, rect, y_min, y_max)
        curve_path = _build_smooth_path(points)

        # --- 渐变填充 ---
        fill_path = QPainterPath(curve_path)
        # 从曲线末端沿底部封闭
        fill_path.lineTo(QPointF(points[-1].x(), rect.bottom()))
        fill_path.lineTo(QPointF(points[0].x(), rect.bottom()))
        fill_path.closeSubpath()

        gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
        fill_color_top = QColor(self._color)
        fill_color_top.setAlpha(77)  # ≈30% 不透明度
        fill_color_bottom = QColor(self._color)
        fill_color_bottom.setAlpha(0)
        gradient.setColorAt(0.0, fill_color_top)
        gradient.setColorAt(1.0, fill_color_bottom)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawPath(fill_path)

        # --- 曲线本体 ---
        pen = QPen(self._color)
        pen.setWidthF(2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(curve_path)


# ---------------------------------------------------------------------------
#  MultiLineChart — 多线叠加曲线图
# ---------------------------------------------------------------------------

class MultiLineChart(QWidget):
    """
    Apple 风格多线叠加曲线图。
    支持多个数据系列同时展示，顶部带彩色图例。
    仅第一条曲线带渐变填充（保持视觉清晰）。
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        title: str = "",
        colors: Optional[List[QColor]] = None,
        labels: Optional[List[str]] = None,
        unit: str = "",
        y_min: Optional[float] = None,
        y_max: Optional[float] = None,
    ) -> None:
        super().__init__(parent)

        # 系列数据：[(data, color, label), ...]
        self._series: List[Tuple[List[float], QColor, str]] = []
        self._default_colors = colors or []
        self._default_labels = labels or []
        self._title = title
        self._unit = unit

        # Y 轴范围
        self._fixed_min: Optional[float] = y_min
        self._fixed_max: Optional[float] = y_max

        self.setMinimumSize(QSize(200, 120))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    # ----- 公开接口 -----

    def set_series(self, series: List[Tuple[List[float], QColor, str]]) -> None:
        """
        设置所有数据系列。
        每个元素为 (data_list, color, label)。
        """
        self._series = [(list(d), QColor(c), l) for d, c, l in series]
        self.update()

    def set_title(self, title: str) -> None:
        self._title = title
        self.update()

    def set_unit(self, unit: str) -> None:
        self._unit = unit
        self.update()

    def set_y_range(self, min_val: Optional[float], max_val: Optional[float]) -> None:
        self._fixed_min = min_val
        self._fixed_max = max_val
        self.update()

    # ----- 内部方法 -----

    def _compute_y_range(self) -> Tuple[float, float]:
        """综合所有系列计算 Y 轴范围"""
        all_vals: List[float] = []
        for data, _, _ in self._series:
            all_vals.extend(data)

        if not all_vals:
            lo = self._fixed_min if self._fixed_min is not None else 0.0
            hi = self._fixed_max if self._fixed_max is not None else 100.0
            return (lo, hi)

        data_min = min(all_vals)
        data_max = max(all_vals)

        if self._fixed_min is not None and self._fixed_max is not None:
            return (self._fixed_min, self._fixed_max)

        if data_min == data_max:
            margin = max(abs(data_min) * 0.1, 1.0)
        else:
            margin = (data_max - data_min) * 0.1

        lo = self._fixed_min if self._fixed_min is not None else data_min - margin
        hi = self._fixed_max if self._fixed_max is not None else data_max + margin
        return (lo, hi)

    def _chart_rect(self) -> QRectF:
        """绘图区域（为图例和标签留出空间）"""
        left_margin = 42.0
        right_margin = 12.0
        top_margin = 50.0 if self._series else 30.0  # 图例需要额外空间
        bottom_margin = 8.0
        return QRectF(
            left_margin,
            top_margin,
            self.width() - left_margin - right_margin,
            self.height() - top_margin - bottom_margin,
        )

    def _data_to_points(
        self, data: List[float], rect: QRectF, y_min: float, y_max: float
    ) -> List[QPointF]:
        """将数据映射为绘图坐标"""
        n = len(data)
        if n == 0:
            return []
        y_range = y_max - y_min
        if y_range == 0:
            y_range = 1.0

        points = []
        for i, val in enumerate(data):
            x = rect.left() + (i / max(1, n - 1)) * rect.width() if n > 1 else rect.center().x()
            y = rect.bottom() - ((val - y_min) / y_range) * rect.height()
            y = max(rect.top(), min(rect.bottom(), y))
            points.append(QPointF(x, y))
        return points

    # ----- 绘制 -----

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        # 圆角裁剪
        clip_path = QPainterPath()
        clip_path.addRoundedRect(QRectF(self.rect()), 12.0, 12.0)
        painter.setClipPath(clip_path)

        y_min, y_max = self._compute_y_range()
        chart = self._chart_rect()

        self._draw_grid(painter, chart)
        self._draw_y_labels(painter, chart, y_min, y_max)
        self._draw_title(painter)
        self._draw_legend(painter)
        self._draw_all_curves(painter, chart, y_min, y_max)

        painter.end()

    def _draw_grid(self, painter: QPainter, rect: QRectF) -> None:
        """水平网格线"""
        pen = QPen(_grid_color())
        pen.setWidthF(1.0)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([4, 6])
        painter.setPen(pen)

        for frac in (0.25, 0.50, 0.75):
            y = rect.bottom() - frac * rect.height()
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

    def _draw_y_labels(
        self, painter: QPainter, rect: QRectF, y_min: float, y_max: float
    ) -> None:
        """Y 轴刻度标签"""
        font = QFont()
        font.setPointSizeF(9.0)
        painter.setFont(font)
        painter.setPen(_text_color(120))

        fm = QFontMetrics(font)
        label_x = 2.0
        label_w = rect.left() - 6.0

        for frac, val in [(0.0, y_min), (0.5, (y_min + y_max) / 2), (1.0, y_max)]:
            y = rect.bottom() - frac * rect.height()
            text = _format_value(val, "")
            text_rect = QRectF(label_x, y - fm.height() / 2, label_w, fm.height())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)

    def _draw_title(self, painter: QPainter) -> None:
        """左上角标题"""
        if not self._title:
            return
        font = QFont()
        font.setPointSizeF(11.0)
        painter.setFont(font)
        painter.setPen(_text_color(180))
        painter.drawText(
            QRectF(12, 6, self.width() - 24, 22),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._title,
        )

    def _draw_legend(self, painter: QPainter) -> None:
        """顶部区域绘制图例：彩色圆点 + 系列名称"""
        if not self._series:
            return

        font = QFont()
        font.setPointSizeF(9.5)
        painter.setFont(font)
        fm = QFontMetrics(font)

        x_offset = 12.0
        y_center = 36.0  # 图例行的垂直中心
        dot_radius = 4.0
        gap_after_dot = 5.0
        gap_between_items = 16.0

        for data, color, label in self._series:
            # 彩色圆点
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawEllipse(QPointF(x_offset + dot_radius, y_center), dot_radius, dot_radius)
            x_offset += dot_radius * 2 + gap_after_dot

            # 标签文字
            painter.setPen(_text_color(160))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            text_width = fm.horizontalAdvance(label)
            painter.drawText(
                QRectF(x_offset, y_center - fm.height() / 2, text_width + 2, fm.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            x_offset += text_width + gap_between_items

            # 最新值（紧跟标签后面，用对应颜色显示）
            if data:
                val_text = _format_value(data[-1], self._unit)
                painter.setPen(color)
                val_width = fm.horizontalAdvance(val_text)
                painter.drawText(
                    QRectF(x_offset, y_center - fm.height() / 2, val_width + 2, fm.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    val_text,
                )
                x_offset += val_width + gap_between_items

    def _draw_all_curves(
        self,
        painter: QPainter,
        rect: QRectF,
        y_min: float,
        y_max: float,
    ) -> None:
        """绘制所有系列的曲线"""
        for idx, (data, color, label) in enumerate(self._series):
            if len(data) < 2:
                # 单点绘制圆点
                if data:
                    pts = self._data_to_points(data, rect, y_min, y_max)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(color))
                    painter.drawEllipse(pts[0], 3.0, 3.0)
                continue

            points = self._data_to_points(data, rect, y_min, y_max)
            curve_path = _build_smooth_path(points)

            # 仅第一条系列绘制渐变填充
            if idx == 0:
                fill_path = QPainterPath(curve_path)
                fill_path.lineTo(QPointF(points[-1].x(), rect.bottom()))
                fill_path.lineTo(QPointF(points[0].x(), rect.bottom()))
                fill_path.closeSubpath()

                gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
                fill_top = QColor(color)
                fill_top.setAlpha(50)  # 略低透明度，避免遮挡其他曲线
                fill_bottom = QColor(color)
                fill_bottom.setAlpha(0)
                gradient.setColorAt(0.0, fill_top)
                gradient.setColorAt(1.0, fill_bottom)

                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(gradient))
                painter.drawPath(fill_path)

            # 曲线
            pen = QPen(color)
            pen.setWidthF(2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(curve_path)
