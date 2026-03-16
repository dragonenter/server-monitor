"""Braille 平滑曲线图 Widget — 高分辨率终端曲线渲染.

每个终端字符 = 2x4 braille 点阵 (宽2像素 x 高4像素)
支持：Catmull-Rom 插值平滑、曲线下方填充、自动 Y 轴缩放、底部基线。
"""

from __future__ import annotations

from textual.widget import Widget
from textual.reactive import reactive
from rich.text import Text

BRAILLE_BASE = 0x2800
BRAILLE_DOTS = [
    [0x01, 0x08],  # row 0
    [0x02, 0x10],  # row 1
    [0x04, 0x20],  # row 2
    [0x40, 0x80],  # row 3
]


def _set_pixel(canvas: list[list[int]], chart_w: int, chart_h: int, px_x: int, py: int):
    """在 braille 画布上点亮一个像素."""
    cc = px_x // 2
    dc = px_x % 2
    cr = py // 4
    dr = py % 4
    if 0 <= cc < chart_w and 0 <= cr < chart_h:
        canvas[cr][cc] |= BRAILLE_DOTS[dr][dc]


def _draw_curve(
    canvas: list[list[int]], chart_w: int, chart_h: int,
    resampled: list[int], px_w: int,
):
    """绘制曲线（含垂直间隙填补）."""
    for px_x in range(px_w):
        _set_pixel(canvas, chart_w, chart_h, px_x, resampled[px_x])

    # 补间隙
    for px_x in range(px_w - 1):
        y1, y2 = resampled[px_x], resampled[px_x + 1]
        if abs(y2 - y1) > 1:
            step = 1 if y2 > y1 else -1
            for py in range(y1, y2, step):
                _set_pixel(canvas, chart_w, chart_h, px_x, py)


def _fill_under_curve(
    canvas_fill: list[list[int]], chart_w: int, chart_h: int,
    resampled: list[int], px_w: int, px_h: int,
):
    """填充曲线下方区域（用于半透明填充层）."""
    for px_x in range(px_w):
        curve_y = resampled[px_x]
        # 从曲线位置向下（y 增大 = 值减小）每隔一行点一个点，产生半透明效果
        for py in range(curve_y, px_h, 2):
            _set_pixel(canvas_fill, chart_w, chart_h, px_x, py)


def _draw_baseline(
    canvas: list[list[int]], chart_w: int, chart_h: int, px_h: int,
):
    """绘制底部基线（虚线）."""
    bottom_y = px_h - 1
    for px_x in range(0, chart_w * 2, 4):  # 每隔4像素画一个点 = 虚线
        _set_pixel(canvas, chart_w, chart_h, px_x, bottom_y)


def _resample(
    data: list[float], target_len: int,
    y_lo: float, y_hi: float, px_h: int,
) -> list[int]:
    """Catmull-Rom 插值重采样 + 映射到像素坐标."""
    n = len(data)
    result = []
    y_range = y_hi - y_lo if y_hi != y_lo else 1.0

    for i in range(target_len):
        t = i / max(target_len - 1, 1) * (n - 1)
        idx = min(int(t), n - 1)
        frac = t - idx

        p0 = data[max(idx - 1, 0)]
        p1 = data[idx]
        p2 = data[min(idx + 1, n - 1)]
        p3 = data[min(idx + 2, n - 1)]

        t2 = frac * frac
        t3 = t2 * frac
        val = 0.5 * (
            (2 * p1) +
            (-p0 + p2) * frac +
            (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2 +
            (-p0 + 3 * p1 - 3 * p2 + p3) * t3
        )
        val = max(y_lo, min(y_hi, val))

        # 0=顶部(最大), px_h-1=底部(最小)
        py = int((1.0 - (val - y_lo) / y_range) * (px_h - 1))
        py = max(0, min(px_h - 1, py))
        result.append(py)

    return result


def _auto_y_range(
    data: list[float], fixed_min: float | None, fixed_max: float | None,
) -> tuple[float, float]:
    """智能 Y 轴范围：如果有固定范围就用，否则自动缩放让曲线占满图表.

    即使给了固定范围，当数据波动很小时也会局部放大以显示细节。
    非负数据保证 Y 轴不会出现负值。
    """
    d_min = min(data)
    d_max = max(data)
    all_non_negative = d_min >= 0

    if fixed_min is not None and fixed_max is not None:
        full_range = fixed_max - fixed_min
        data_range = d_max - d_min
        data_center = (d_max + d_min) / 2

        if full_range > 0 and data_range / full_range < 0.2:
            padding = max(data_range * 2, full_range * 0.05)
            y_lo = max(fixed_min, data_center - padding)
            y_hi = min(fixed_max, data_center + padding)
            if y_lo == y_hi:
                y_lo = max(fixed_min, y_lo - 1)
                y_hi = min(fixed_max, y_hi + 1)
            return y_lo, y_hi
        return fixed_min, fixed_max

    # 无固定范围：自动缩放，上方留 15% padding，下方看情况
    data_range = d_max - d_min
    if data_range == 0:
        data_range = max(abs(d_max) * 0.2, 1)

    y_hi = d_max + data_range * 0.15
    y_lo = d_min - data_range * 0.1

    # 非负数据：底部从 0 开始
    if all_non_negative:
        y_lo = 0

    if y_lo == y_hi:
        y_hi = y_lo + 1

    return y_lo, y_hi


def _render_y_label(val: float) -> str:
    """格式化 Y 轴标签，自适应单位."""
    if abs(val) >= 1_000_000:
        return f"{val/1_000_000:>5.1f}M"
    elif abs(val) >= 1_000:
        return f"{val/1_000:>5.1f}K"
    elif abs(val) >= 100:
        return f"{val:>6.0f}"
    elif abs(val) >= 1:
        return f"{val:>6.1f}"
    else:
        return f"{val:>6.2f}"


class SmoothChart(Widget):
    """高分辨率 braille 曲线图，带填充效果."""

    DEFAULT_CSS = """
    SmoothChart {
        height: 8;
        padding: 0 1;
    }
    """

    data: reactive[list[float]] = reactive(list, always_update=True)
    chart_color: reactive[str] = reactive("#30D158")
    fill_color: reactive[str] = reactive("")  # 空=使用 chart_color dim
    title: reactive[str] = reactive("")
    y_min: reactive[float | None] = reactive(None)
    y_max: reactive[float | None] = reactive(None)
    show_labels: reactive[bool] = reactive(True)
    show_fill: reactive[bool] = reactive(True)

    def render(self) -> Text:
        data = self.data
        width = self.size.width
        height = self.size.height

        if height < 1 or width < 4:
            return Text("")

        label_w = 7 if self.show_labels else 0
        chart_w = max(width - label_w, 4)
        px_w = chart_w * 2
        px_h = height * 4

        # 标题占一行
        title_offset = 1 if self.title and height > 3 else 0
        draw_h = height - title_offset
        draw_px_h = draw_h * 4

        if not data or len(data) < 2:
            return self._render_empty(width, height)

        # Y 轴范围（智能缩放）
        y_lo, y_hi = _auto_y_range(data, self.y_min, self.y_max)

        # 重采样
        px_w_draw = chart_w * 2
        resampled = _resample(data, px_w_draw, y_lo, y_hi, draw_px_h)

        # 主曲线画布
        canvas = [[0] * chart_w for _ in range(draw_h)]
        _draw_curve(canvas, chart_w, draw_h, resampled, px_w_draw)

        # 填充画布（单独层，用 dim 色渲染）
        canvas_fill = [[0] * chart_w for _ in range(draw_h)]
        if self.show_fill:
            _fill_under_curve(canvas_fill, chart_w, draw_h, resampled, px_w_draw, draw_px_h)

        # 基线
        _draw_baseline(canvas_fill, chart_w, draw_h, draw_px_h)

        # 渲染
        result = Text()

        if title_offset:
            # 标题行 + 当前值
            cur_val = data[-1]
            title_str = f"  {self.title}  {cur_val:.1f}"
            result.append(title_str.ljust(width)[:width], style="bold")
            result.append("\n")

        fill_style = self.fill_color if self.fill_color else f"{self.chart_color}"

        for row in range(draw_h):
            # Y 轴标签
            if self.show_labels:
                if row == 0:
                    lbl = _render_y_label(y_hi) + "│"
                elif row == draw_h - 1:
                    lbl = _render_y_label(y_lo) + "│"
                elif row == draw_h // 2:
                    lbl = _render_y_label((y_hi + y_lo) / 2) + "│"
                else:
                    lbl = " " * (label_w - 1) + "│"
                result.append(lbl[:label_w], style="dim")

            # 合成：曲线层优先，填充层次之
            for col in range(chart_w):
                curve_val = canvas[row][col]
                fill_val = canvas_fill[row][col]

                if curve_val:
                    result.append(chr(BRAILLE_BASE + curve_val), style=self.chart_color)
                elif fill_val:
                    result.append(chr(BRAILLE_BASE + fill_val), style=f"{fill_style} dim")
                else:
                    result.append(chr(BRAILLE_BASE), style="")

            if row < draw_h - 1:
                result.append("\n")

        return result

    def _render_empty(self, width: int, height: int) -> Text:
        result = Text()
        if self.title:
            result.append(f"  {self.title}  等待数据...".ljust(width)[:width], style="dim")
            result.append("\n")
        for i in range(height - (1 if self.title else 0)):
            result.append(" " * width)
            if i < height - 2:
                result.append("\n")
        return result


class MultiLineChart(Widget):
    """多条曲线叠加的 braille 图表."""

    DEFAULT_CSS = """
    MultiLineChart {
        height: 8;
        padding: 0 1;
    }
    """

    series: reactive[list[tuple[list[float], str, str]]] = reactive(list, always_update=True)
    title: reactive[str] = reactive("")
    y_min: reactive[float | None] = reactive(None)
    y_max: reactive[float | None] = reactive(None)
    show_labels: reactive[bool] = reactive(True)

    def render(self) -> Text:
        all_series = self.series
        width = self.size.width
        height = self.size.height

        if height < 1 or width < 4:
            return Text("")

        label_w = 7 if self.show_labels else 0
        chart_w = max(width - label_w, 4)

        valid_series = [(d, c, l) for d, c, l in all_series if d and len(d) >= 2]

        if not valid_series:
            result = Text()
            if self.title:
                result.append(f"  {self.title}  等待数据...".ljust(width)[:width], style="dim")
                result.append("\n")
            for i in range(height - (1 if self.title else 0)):
                result.append(" " * width)
                if i < height - 2:
                    result.append("\n")
            return result

        title_offset = 1 if self.title and height > 3 else 0
        draw_h = height - title_offset
        draw_px_h = draw_h * 4
        px_w = chart_w * 2

        # 合并所有数据计算 Y 范围
        all_data = []
        for d, _, _ in valid_series:
            all_data.extend(d)
        y_lo, y_hi = _auto_y_range(all_data, self.y_min, self.y_max)

        # 为每条线创建画布
        layers: list[tuple[list[list[int]], str, str]] = []
        for data, color, label in valid_series:
            canvas = [[0] * chart_w for _ in range(draw_h)]
            resampled = _resample(data, px_w, y_lo, y_hi, draw_px_h)
            _draw_curve(canvas, chart_w, draw_h, resampled, px_w)
            layers.append((canvas, color, label))

        # 基线层
        baseline = [[0] * chart_w for _ in range(draw_h)]
        _draw_baseline(baseline, chart_w, draw_h, draw_px_h)

        # 渲染
        result = Text()

        if title_offset:
            parts = [f"  {self.title}"]
            for _, c, l in layers:
                parts.append(f"  [{c}]━ {l}[/{c}]")
            legend_str = "".join(parts)
            result.append_text(Text.from_markup(legend_str.ljust(width)[:width]))
            result.append("\n")

        for row in range(draw_h):
            if self.show_labels:
                if row == 0:
                    lbl = _render_y_label(y_hi) + "│"
                elif row == draw_h - 1:
                    lbl = _render_y_label(y_lo) + "│"
                elif row == draw_h // 2:
                    lbl = _render_y_label((y_hi + y_lo) / 2) + "│"
                else:
                    lbl = " " * (label_w - 1) + "│"
                result.append(lbl[:label_w], style="dim")

            for col in range(chart_w):
                # 合并所有层的 braille 值
                ch_val = 0
                ch_color = ""
                for canvas, color, _ in layers:
                    cell = canvas[row][col]
                    if cell:
                        ch_val |= cell
                        ch_color = color  # 最后一个有值的颜色

                if ch_val:
                    result.append(chr(BRAILLE_BASE + ch_val), style=ch_color)
                elif baseline[row][col]:
                    result.append(chr(BRAILLE_BASE + baseline[row][col]), style="dim")
                else:
                    result.append(chr(BRAILLE_BASE), style="")

            if row < draw_h - 1:
                result.append("\n")

        return result
