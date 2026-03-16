"""Server Monitor - Apple 风格 Textual TUI 应用."""

import os
import platform
import signal
import socket
import time
from datetime import datetime, timedelta

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    Rule,
    Static,
    TabbedContent,
    TabPane,
)

from .chart import SmoothChart, MultiLineChart
from .collectors import MetricsCollector


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def fmt_bytes(n: int, suffix: str = "B") -> str:
    for unit in ("", "K", "M", "G", "T", "P"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}{suffix}"
        n /= 1024  # type: ignore
    return f"{n:.1f}E{suffix}"


def fmt_rate(n: float) -> str:
    return fmt_bytes(int(n), "B/s")


def pct_color(val: float) -> str:
    """苹果风格：柔和的渐变色调."""
    if val >= 90:
        return "#FF453A"  # 苹果红
    elif val >= 70:
        return "#FF9F0A"  # 苹果橙
    elif val >= 50:
        return "#FFD60A"  # 苹果黄
    return "#30D158"  # 苹果绿


def temp_color(val: float) -> str:
    if val >= 85:
        return "#FF453A"
    elif val >= 70:
        return "#FF9F0A"
    return "#30D158"


def bar_text(val: float, width: int = 20) -> str:
    """苹果风格进度条：圆润的字符."""
    filled = int(val / 100 * width)
    empty = width - filled
    return "●" * filled + "○" * empty


# ---------------------------------------------------------------------------
# CSS - 苹果设计语言：大量留白、圆角、柔和阴影感
# ---------------------------------------------------------------------------
CSS = """
Screen {
    background: $surface;
}

.header-bar {
    text-align: center;
    padding: 0 2;
    height: 1;
    background: $boost;
    color: $text-muted;
}

.section-title {
    text-style: bold;
    color: $accent;
    padding: 0 1;
    margin: 0 0 0 0;
}

.metric-card {
    border: round $primary-lighten-2;
    padding: 1 2;
    margin: 0 0;
    height: auto;
}

.metric-card-full {
    border: round $primary-lighten-2;
    padding: 1 2;
    margin: 0 0;
    height: auto;
    column-span: 2;
}

.overview-grid {
    layout: grid;
    grid-size: 2;
    grid-gutter: 1;
    padding: 1 2;
}

.metric-text {
    padding: 0 1;
}

.chart-box {
    height: 8;
    margin: 0 1;
}

.chart-box-large {
    height: 12;
    margin: 0 1;
}

.alert-banner {
    background: #FF453A;
    color: white;
    text-style: bold;
    text-align: center;
    display: none;
    height: 1;
}

.alert-visible {
    display: block;
}

.sort-hint {
    color: $text-muted;
    padding: 0 2;
    height: 1;
}

DataTable {
    height: 1fr;
}

#agent-detail-scroll, #gpu-detail-scroll, #net-detail-scroll {
    padding: 1 2;
}

Footer {
    background: $primary-darken-2;
}
"""


class AlertBanner(Static):
    pass


class ServerMonitorApp(App):
    """苹果风格服务器监控面板."""

    TITLE = "系统监控"
    SUB_TITLE = ""
    CSS = CSS

    BINDINGS = [
        Binding("q", "quit", "退出", show=True),
        Binding("t", "toggle_theme", "主题", show=True),
        Binding("1", "tab_overview", "总览", show=True),
        Binding("2", "tab_agent", "Agent", show=True),
        Binding("3", "tab_gpu", "显卡", show=True),
        Binding("4", "tab_process", "进程", show=True),
        Binding("5", "tab_network", "网络", show=True),
        Binding("s", "cycle_sort", "排序", show=True),
        Binding("k", "kill_process", "终止", show=True),
        Binding("r", "refresh_now", "刷新", show=True),
        Binding("?", "show_help", "帮助", show=True),
    ]

    theme_idx = reactive(0)
    THEMES = [
        "textual-dark", "monokai", "dracula", "catppuccin-mocha",
        "tokyo-night", "nord", "gruvbox", "textual-light", "solarized-light",
    ]

    def __init__(self):
        super().__init__()
        self.collector = MetricsCollector()
        self._uptime_start = time.time()
        self._proc_sort = "cpu"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield AlertBanner("", classes="alert-banner", id="alert-banner")

        hostname = socket.gethostname()
        os_info = f"{platform.system()} {platform.release()}"
        gpu_back = {"nvidia": "NVIDIA", "apple": "Apple GPU", "none": "无"}.get(
            self.collector.gpu_backend, "未知"
        )
        yield Label(
            f"  {hostname}  ·  {os_info}  ·  {gpu_back}",
            classes="header-bar",
            id="header-bar",
        )

        with TabbedContent(id="tabs"):
            # ---- 总览 ----
            with TabPane("总览", id="overview-tab"):
                with Container(classes="overview-grid"):
                    with Vertical(classes="metric-card", id="cpu-card"):
                        yield Label("处理器", classes="section-title")
                        yield Label("", id="cpu-text", classes="metric-text")
                        yield SmoothChart(id="cpu-chart", classes="chart-box")

                    with Vertical(classes="metric-card", id="mem-card"):
                        yield Label("内存", classes="section-title")
                        yield Label("", id="mem-text", classes="metric-text")
                        yield SmoothChart(id="mem-chart", classes="chart-box")

                    with Vertical(classes="metric-card-full", id="agent-card"):
                        yield Label("AI Agent", classes="section-title")
                        yield Label("", id="agent-text", classes="metric-text")

                    with Vertical(classes="metric-card-full", id="gpu-card"):
                        yield Label("显卡", classes="section-title")
                        yield Label("", id="gpu-text", classes="metric-text")
                        yield MultiLineChart(id="gpu-chart", classes="chart-box")

                    with Vertical(classes="metric-card", id="disk-card"):
                        yield Label("磁盘", classes="section-title")
                        yield Label("", id="disk-text", classes="metric-text")

                    with Vertical(classes="metric-card", id="net-card"):
                        yield Label("网络", classes="section-title")
                        yield Label("", id="net-text", classes="metric-text")
                        yield SmoothChart(id="net-chart", classes="chart-box")

            # ---- Agent 详情 ----
            with TabPane("Agent", id="agent-tab"):
                with VerticalScroll(id="agent-detail-scroll"):
                    yield Label("", id="agent-detail-text")

            # ---- 显卡详情 ----
            with TabPane("显卡详情", id="gpu-tab"):
                with VerticalScroll(id="gpu-detail-scroll"):
                    yield Label("", id="gpu-detail-text")
                    yield MultiLineChart(id="gpu-detail-chart", classes="chart-box-large")

            # ---- 进程 ----
            with TabPane("进程", id="proc-tab"):
                with Vertical():
                    yield Label(
                        "排序: [bold]CPU%[/bold]  ·  [s] 切换排序  ·  [k] 终止选中进程",
                        classes="sort-hint",
                        id="sort-hint",
                    )
                    yield DataTable(id="proc-table", cursor_type="row")

            # ---- 网络 ----
            with TabPane("网络", id="net-tab"):
                with VerticalScroll(id="net-detail-scroll"):
                    yield Label("", id="net-detail-text")
                    yield SmoothChart(id="net-send-chart", classes="chart-box-large")
                    yield SmoothChart(id="net-recv-chart", classes="chart-box-large")

        yield Footer()

    def on_mount(self) -> None:
        self.theme = self.THEMES[0]
        table = self.query_one("#proc-table", DataTable)
        table.add_columns("PID", "用户", "名称", "CPU%", "内存%", "显存(MB)", "状态")
        self.collector.collect_cpu()
        self.set_interval(1.0, self._refresh_metrics)
        self.set_timer(0.5, self._refresh_metrics)

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------

    def _refresh_metrics(self) -> None:
        try:
            cpu = self.collector.collect_cpu()
            mem = self.collector.collect_memory()
            gpus = self.collector.collect_gpu()
            disks = self.collector.collect_disk()
            net = self.collector.collect_network()
            procs = self.collector.collect_processes(limit=30)

            gpu_procs = self.collector.collect_gpu_processes()
            agents = self.collector.collect_agents()
            capacity = self.collector.calculate_capacity()

            self._update_agents(agents, capacity)
            self._update_agent_detail(agents, capacity)
            self._update_cpu(cpu)
            self._update_memory(mem)
            self._update_gpu_overview(gpus, gpu_procs)
            self._update_gpu_detail(gpus, gpu_procs)
            self._update_disk(disks)
            self._update_network(net)
            self._update_net_detail(net)
            self._update_processes(procs)
            self._check_alerts(cpu, mem, gpus)
            self._update_header()
        except Exception as e:
            self.log.error(f"刷新出错: {e}")

    def _update_header(self) -> None:
        try:
            elapsed = time.time() - self._uptime_start
            uptime = str(timedelta(seconds=int(elapsed)))
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            hostname = socket.gethostname()
            self.query_one("#header-bar", Label).update(
                f"  {hostname}  ·  监控时长 {uptime}  ·  {now}"
            )
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # AI Agent
    # ------------------------------------------------------------------

    @staticmethod
    def _pad_display(text: str, width: int, align: str = "left") -> str:
        """按显示宽度对齐（中文字符占 2 宽度）."""
        display_w = 0
        for ch in text:
            display_w += 2 if ord(ch) > 0x7F else 1
        pad = max(0, width - display_w)
        if align == "right":
            return " " * pad + text
        return text + " " * pad

    def _update_agents(self, agents, capacity) -> None:
        try:
            count = len(agents)
            parallel = capacity.get("recommended_parallel", 0)
            gpu_free = capacity.get("gpu_free_mb", 0)
            ram_free = capacity.get("ram_free_mb", 0)

            lines = [
                f"  运行中: [{pct_color(0)}]{count}[/{pct_color(0)}] 个 Agent  ·  "
                f"可并行: [#0A84FF]+{parallel}[/#0A84FF]  ·  "
                f"空闲显存: {gpu_free:.0f}MB  ·  空闲内存: {ram_free:.0f}MB"
            ]

            _p = self._pad_display
            if agents:
                lines.append("")
                lines.append(
                    f"  {_p('名称', 20)} {_p('PID', 10)} {_p('CPU%', 7)} "
                    f"{_p('内存', 9)} {_p('GPU显存', 10)} {_p('运行时长', 10)}"
                )
                lines.append(f"  {'─'*20} {'─'*10} {'─'*7} {'─'*9} {'─'*10} {'─'*10}")
                for a in agents[:10]:
                    h, rem = divmod(int(a.uptime_seconds), 3600)
                    m, s = divmod(rem, 60)
                    uptime_str = f"{h}h{m:02d}m" if h > 0 else f"{m}m{s:02d}s"
                    gpu_str = f"{a.gpu_memory_mb:.0f}MB" if a.gpu_memory_mb > 0 else "—"
                    cc = pct_color(a.cpu_percent)
                    lines.append(
                        f"  {_p(a.name, 20)} {_p(str(a.pid), 10)} "
                        f"[{cc}]{_p(f'{a.cpu_percent:.1f}%', 7, 'right')}[/{cc}] "
                        f"{_p(f'{a.memory_mb:.0f}M', 9, 'right')} "
                        f"{_p(gpu_str, 10, 'right')} "
                        f"{_p(uptime_str, 10, 'right')}"
                    )
            else:
                lines.append("  无运行中的 Agent")

            self.query_one("#agent-text", Label).update("\n".join(lines))
        except NoMatches:
            pass

    def _update_agent_detail(self, agents, capacity) -> None:
        try:
            _p = self._pad_display
            lines = []

            # Header summary
            count = len(agents)
            parallel = capacity.get("recommended_parallel", 0)
            lines.append(f"  [bold]AI Agent 运行状态[/bold]")
            lines.append(f"  运行中: [#0A84FF]{count}[/#0A84FF] 个  ·  可并行: [#30D158]+{parallel}[/#30D158]")
            lines.append(f"  GPU空闲: {capacity.get('gpu_free_mb', 0):.0f}MB  ·  "
                         f"内存空闲: {capacity.get('ram_free_mb', 0):.0f}MB  ·  "
                         f"CPU空闲: {capacity.get('cpu_free_cores', 0):.1f} 核")
            lines.append("")

            if not agents:
                lines.append("  [dim]当前无运行中的 AI Agent[/dim]")

            for a in agents:
                # Format uptime
                h, rem = divmod(int(a.uptime_seconds), 3600)
                m, s = divmod(rem, 60)
                uptime_str = f"{h}h{m:02d}m{s:02d}s" if h > 0 else f"{m}m{s:02d}s"

                # Agent type color
                type_color = {"推理服务": "#FF9F0A", "CLI工具": "#0A84FF", "SDK应用": "#30D158", "框架": "#BF5AF2"}.get(a.agent_type, "#8E8E93")

                # Memory trend indicator
                if a.mem_trend_mb_per_min > 1:
                    trend = f"[#FF453A]↑ {a.mem_trend_mb_per_min:.1f}MB/min[/#FF453A]"
                elif a.mem_trend_mb_per_min < -1:
                    trend = f"[#30D158]↓ {abs(a.mem_trend_mb_per_min):.1f}MB/min[/#30D158]"
                else:
                    trend = "[dim]稳定[/dim]"

                gpu_str = f"{a.gpu_memory_mb:.0f}MB" if a.gpu_memory_mb > 0 else "—"
                model_str = a.model_name if a.model_name else "—"
                ports_str = ", ".join(str(p) for p in a.listen_ports[:5]) if a.listen_ports else "—"

                lines.append(f"  ──────────────────────────────────────────────────")
                lines.append(f"  [bold]{a.name}[/bold]  [{type_color}]{a.agent_type}[/{type_color}]  ·  PID {a.pid}  ·  运行 {uptime_str}")
                if a.model_name:
                    lines.append(f"  模型: [bold]{model_str}[/bold]")
                lines.append(f"  命令: [dim]{a.cmdline[:80]}{'...' if len(a.cmdline) > 80 else ''}[/dim]")
                lines.append("")
                lines.append(f"    CPU   {a.cpu_percent:>6.1f}%   (含子进程: {a.total_cpu_percent:.1f}%)")
                lines.append(f"    内存  {a.memory_mb:>6.0f}MB  (含子进程: {a.total_memory_mb:.0f}MB)  趋势: {trend}")
                lines.append(f"    GPU   {gpu_str:>6s}")
                lines.append(f"    线程  {a.thread_count:>6d}     子进程: {a.children_count}")
                if a.io_read_mb > 0 or a.io_write_mb > 0:
                    lines.append(f"    IO    读 {a.io_read_mb:.1f}MB  写 {a.io_write_mb:.1f}MB")
                lines.append(f"    网络  {a.connections_count} 个连接  ·  监听端口: {ports_str}")
                lines.append("")

            self.query_one("#agent-detail-text", Label).update("\n".join(lines))
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # 处理器
    # ------------------------------------------------------------------

    def _update_cpu(self, cpu) -> None:
        try:
            c = pct_color(cpu.percent_total)
            temp = f"  温度 {cpu.temp:.0f}°C" if cpu.temp else ""
            freq = f"  频率 {cpu.freq_current:.0f}MHz" if cpu.freq_current else ""

            # 每核心状态，紧凑排列
            cores_parts = []
            for i, pct in enumerate(cpu.percent_per_core):
                cc = pct_color(pct)
                cores_parts.append(f"[{cc}]{pct:4.0f}%[/{cc}]")
            cores_line = "  ".join(cores_parts)

            self.query_one("#cpu-text", Label).update(
                f"  使用率 [{c}]{cpu.percent_total:.1f}%[/{c}]  ·  "
                f"{cpu.core_count} 核心  ·  "
                f"负载 {cpu.load_1:.1f} / {cpu.load_5:.1f} / {cpu.load_15:.1f}"
                f"{temp}{freq}\n"
                f"  {cores_line}"
            )

            # 曲线图
            self._update_chart(
                "#cpu-chart",
                self.collector.get_cpu_history(),
                title="处理器使用率 (%)",
                color="#30D158",
                y_min=0, y_max=100,
            )
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # 内存
    # ------------------------------------------------------------------

    def _update_memory(self, mem) -> None:
        try:
            c = pct_color(mem.percent)
            sc = pct_color(mem.swap_percent)
            self.query_one("#mem-text", Label).update(
                f"  已用 [{c}]{mem.percent:.1f}%[/{c}]  ·  "
                f"{fmt_bytes(mem.used)} / {fmt_bytes(mem.total)}\n"
                f"  交换 [{sc}]{mem.swap_percent:.1f}%[/{sc}]  ·  "
                f"{fmt_bytes(mem.swap_used)} / {fmt_bytes(mem.swap_total)}"
            )

            self._update_chart(
                "#mem-chart",
                self.collector.get_mem_history(),
                title="内存使用率 (%)",
                color="#5E5CE6",
                y_min=0, y_max=100,
            )
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # 显卡总览
    # ------------------------------------------------------------------

    def _update_gpu_overview(self, gpus, gpu_procs=None) -> None:
        try:
            if not gpus:
                self.query_one("#gpu-text", Label).update("  未检测到显卡")
                return

            lines = []
            for gpu in gpus:
                uc = pct_color(gpu.utilization)
                mc = pct_color(gpu.memory_percent)
                tc = temp_color(gpu.temperature)
                power = ""
                if gpu.power_limit > 0:
                    power = f"  ·  功耗 {gpu.power_draw:.0f}W / {gpu.power_limit:.0f}W"
                fan = f"  ·  风扇 {gpu.fan_speed:.0f}%" if gpu.fan_speed is not None else ""
                lines.append(
                    f"  GPU {gpu.index}  {gpu.name}\n"
                    f"    利用率 [{uc}]{gpu.utilization:.1f}%[/{uc}]  ·  "
                    f"显存 [{mc}]{gpu.memory_percent:.1f}%[/{mc}] "
                    f"({gpu.memory_used:.0f} / {gpu.memory_total:.0f} MB)  ·  "
                    f"温度 [{tc}]{gpu.temperature:.0f}°C[/{tc}]"
                    f"{power}{fan}"
                )
                # 显示该 GPU 上的进程
                if gpu_procs:
                    procs_on_gpu = [p for p in gpu_procs if p.gpu_index == gpu.index]
                    if procs_on_gpu:
                        lines.append("    ┌ 显存占用进程:")
                        for p in procs_on_gpu[:5]:
                            lines.append(
                                f"    │  PID {p.pid:<8d} {p.name:<20s} {p.gpu_memory:>8.0f} MB"
                            )
                        if len(procs_on_gpu) > 5:
                            lines.append(f"    └ ... 共 {len(procs_on_gpu)} 个进程")
                        else:
                            lines.append("    └")

            self.query_one("#gpu-text", Label).update("\n".join(lines))

            # 曲线图 - 所有 GPU 利用率叠加
            self._update_multi_chart("#gpu-chart", gpus)
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # 显卡详情
    # ------------------------------------------------------------------

    def _update_gpu_detail(self, gpus, gpu_procs=None) -> None:
        try:
            if not gpus:
                self.query_one("#gpu-detail-text", Label).update("  未检测到显卡")
                return

            lines = []
            for gpu in gpus:
                uc = pct_color(gpu.utilization)
                mc = pct_color(gpu.memory_percent)
                tc = temp_color(gpu.temperature)
                hist = self.collector.get_gpu_history(gpu.index)
                avg = sum(hist) / len(hist) if hist else 0
                peak = max(hist) if hist else 0

                power_line = ""
                if gpu.power_limit > 0:
                    pp = gpu.power_draw / gpu.power_limit * 100
                    power_line = (
                        f"  功耗    {gpu.power_draw:.1f}W / {gpu.power_limit:.1f}W ({pp:.0f}%)\n"
                        f"          {bar_text(pp, 30)}\n"
                    )
                fan_line = ""
                if gpu.fan_speed is not None:
                    fan_line = f"  风扇    {gpu.fan_speed:.0f}%  {bar_text(gpu.fan_speed, 30)}\n"

                # 该 GPU 上的进程列表
                proc_lines = ""
                if gpu_procs:
                    procs_on_gpu = [p for p in gpu_procs if p.gpu_index == gpu.index]
                    if procs_on_gpu:
                        proc_lines = "\n  显存占用进程:\n"
                        proc_lines += f"  {'PID':<10s} {'进程名':<25s} {'显存占用':>10s}\n"
                        proc_lines += f"  {'─'*10} {'─'*25} {'─'*10}\n"
                        for p in procs_on_gpu:
                            proc_lines += f"  {p.pid:<10d} {p.name:<25s} {p.gpu_memory:>8.0f} MB\n"
                    else:
                        proc_lines = "\n  无进程占用显存\n"

                lines.append(
                    f"  ──────────────────────────────────────────\n"
                    f"  GPU {gpu.index} · [bold]{gpu.name}[/bold]\n"
                    f"  ──────────────────────────────────────────\n"
                    f"\n"
                    f"  利用率  [{uc}]{gpu.utilization:.1f}%[/{uc}]    "
                    f"均值 {avg:.1f}%  ·  峰值 {peak:.1f}%\n"
                    f"          {bar_text(gpu.utilization, 30)}\n"
                    f"\n"
                    f"  显存    [{mc}]{gpu.memory_percent:.1f}%[/{mc}]    "
                    f"{gpu.memory_used:.0f} MB / {gpu.memory_total:.0f} MB\n"
                    f"          {bar_text(gpu.memory_percent, 30)}\n"
                    f"\n"
                    f"  温度    [{tc}]{gpu.temperature:.0f}°C[/{tc}]\n"
                    f"{power_line}{fan_line}"
                    f"{proc_lines}"
                )
            self.query_one("#gpu-detail-text", Label).update("\n".join(lines))

            # 详情曲线图
            self._update_multi_chart("#gpu-detail-chart", gpus)
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # 磁盘
    # ------------------------------------------------------------------

    def _update_disk(self, disks) -> None:
        try:
            lines = []
            for d in disks[:6]:
                c = pct_color(d.percent)
                lines.append(
                    f"  {d.mountpoint:15s}  [{c}]{d.percent:5.1f}%[/{c}]  "
                    f"{bar_text(d.percent, 15)}  "
                    f"{fmt_bytes(d.used)} / {fmt_bytes(d.total)}"
                )
            self.query_one("#disk-text", Label).update(
                "\n".join(lines) if lines else "  无磁盘信息"
            )
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # 网络（总览卡片）
    # ------------------------------------------------------------------

    def _update_network(self, net) -> None:
        try:
            self.query_one("#net-text", Label).update(
                f"  ↑ 上传  {fmt_rate(net.bytes_sent_per_sec):>12s}   累计 {fmt_bytes(net.total_sent)}\n"
                f"  ↓ 下载  {fmt_rate(net.bytes_recv_per_sec):>12s}   累计 {fmt_bytes(net.total_recv)}"
            )

            # 网络总流量曲线
            send = self.collector.get_net_send_history()
            recv = self.collector.get_net_recv_history()
            combined = [s + r for s, r in zip(send, recv)] if send else []
            self._update_chart(
                "#net-chart", combined,
                title="总流量 (B/s)", color="#0A84FF",
            )
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # 网络详情
    # ------------------------------------------------------------------

    def _update_net_detail(self, net) -> None:
        try:
            send_hist = self.collector.get_net_send_history()
            recv_hist = self.collector.get_net_recv_history()
            send_peak = max(send_hist) if send_hist else 0
            recv_peak = max(recv_hist) if recv_hist else 0

            self.query_one("#net-detail-text", Label).update(
                f"  ↑ 上传速率  {fmt_rate(net.bytes_sent_per_sec):>12s}   "
                f"累计发送 {fmt_bytes(net.total_sent)}   峰值 {fmt_rate(send_peak)}\n"
                f"  ↓ 下载速率  {fmt_rate(net.bytes_recv_per_sec):>12s}   "
                f"累计接收 {fmt_bytes(net.total_recv)}   峰值 {fmt_rate(recv_peak)}"
            )

            self._update_chart(
                "#net-send-chart", send_hist,
                title="上传速率 (B/s)", color="#30D158",
            )
            self._update_chart(
                "#net-recv-chart", recv_hist,
                title="下载速率 (B/s)", color="#0A84FF",
            )
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # 进程
    # ------------------------------------------------------------------

    def _update_processes(self, procs) -> None:
        try:
            table = self.query_one("#proc-table", DataTable)
            sort_key = {
                "cpu": lambda p: p.cpu_percent,
                "mem": lambda p: p.memory_percent,
                "gpu": lambda p: p.gpu_memory or 0,
                "name": lambda p: p.name.lower(),
            }.get(self._proc_sort, lambda p: p.cpu_percent)
            reverse = self._proc_sort != "name"
            procs.sort(key=sort_key, reverse=reverse)

            table.clear()
            for p in procs:
                gpu_str = f"{p.gpu_memory:.0f}" if p.gpu_memory else "-"
                table.add_row(
                    str(p.pid),
                    p.username[:12],
                    p.name[:25],
                    f"{p.cpu_percent:.1f}",
                    f"{p.memory_percent:.1f}",
                    gpu_str,
                    p.status,
                    key=str(p.pid),
                )

            sort_names = {"cpu": "CPU%", "mem": "内存%", "gpu": "显存", "name": "名称"}
            self.query_one("#sort-hint", Label).update(
                f"排序: [bold]{sort_names.get(self._proc_sort, 'CPU%')}[/bold]  ·  "
                f"[s] 切换排序  ·  [k] 终止选中  ·  "
                f"共 {len(procs)} 个进程"
            )
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # 告警
    # ------------------------------------------------------------------

    def _check_alerts(self, cpu, mem, gpus) -> None:
        alerts = []
        if cpu.percent_total >= 95:
            alerts.append(f"处理器 {cpu.percent_total:.0f}%")
        if mem.percent >= 95:
            alerts.append(f"内存 {mem.percent:.0f}%")
        for gpu in gpus:
            if gpu.temperature >= 85:
                alerts.append(f"GPU{gpu.index} 温度 {gpu.temperature:.0f}°C")
            if gpu.memory_percent >= 95:
                alerts.append(f"GPU{gpu.index} 显存 {gpu.memory_percent:.0f}%")
        try:
            banner = self.query_one("#alert-banner", AlertBanner)
            if alerts:
                banner.update(f"  ⚠ 告警: {' | '.join(alerts)}  ")
                banner.add_class("alert-visible")
            else:
                banner.remove_class("alert-visible")
        except NoMatches:
            pass

    # ------------------------------------------------------------------
    # 绘图工具
    # ------------------------------------------------------------------

    def _update_chart(
        self,
        widget_id: str,
        data: list[float],
        title: str = "",
        color: str = "#30D158",
        y_min: float | None = None,
        y_max: float | None = None,
    ) -> None:
        """更新 SmoothChart 曲线数据."""
        try:
            chart = self.query_one(widget_id, SmoothChart)
            chart.title = title
            chart.chart_color = color
            chart.y_min = y_min
            chart.y_max = y_max
            chart.data = list(data)
        except (NoMatches, Exception):
            pass

    def _update_multi_chart(
        self, widget_id: str, gpus,
    ) -> None:
        """更新 MultiLineChart 多 GPU 曲线."""
        GPU_COLORS = [
            "#30D158",  # 绿
            "#0A84FF",  # 蓝
            "#FF9F0A",  # 橙
            "#BF5AF2",  # 紫
            "#FF453A",  # 红
            "#64D2FF",  # 青
        ]
        try:
            chart = self.query_one(widget_id, MultiLineChart)
            chart.title = "显卡利用率 (%)"
            chart.y_min = 0
            chart.y_max = 100
            series = []
            for i, gpu in enumerate(gpus):
                hist = self.collector.get_gpu_history(gpu.index)
                if len(hist) >= 2:
                    color = GPU_COLORS[i % len(GPU_COLORS)]
                    series.append((list(hist), color, f"GPU {gpu.index}"))
            chart.series = series
        except (NoMatches, Exception):
            pass

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------

    def action_toggle_theme(self) -> None:
        self.theme_idx = (self.theme_idx + 1) % len(self.THEMES)
        self.theme = self.THEMES[self.theme_idx]
        self.notify(f"主题: {self.THEMES[self.theme_idx]}", timeout=2)

    def action_tab_overview(self) -> None:
        try:
            self.query_one("#tabs", TabbedContent).active = "overview-tab"
        except NoMatches:
            pass

    def action_tab_agent(self) -> None:
        try:
            self.query_one("#tabs", TabbedContent).active = "agent-tab"
        except NoMatches:
            pass

    def action_tab_gpu(self) -> None:
        try:
            self.query_one("#tabs", TabbedContent).active = "gpu-tab"
        except NoMatches:
            pass

    def action_tab_process(self) -> None:
        try:
            self.query_one("#tabs", TabbedContent).active = "proc-tab"
        except NoMatches:
            pass

    def action_tab_network(self) -> None:
        try:
            self.query_one("#tabs", TabbedContent).active = "net-tab"
        except NoMatches:
            pass

    def action_cycle_sort(self) -> None:
        modes = ["cpu", "mem", "gpu", "name"]
        idx = modes.index(self._proc_sort) if self._proc_sort in modes else 0
        self._proc_sort = modes[(idx + 1) % len(modes)]
        names = {"cpu": "CPU%", "mem": "内存%", "gpu": "显存", "name": "名称"}
        self.notify(f"排序: {names.get(self._proc_sort, 'CPU%')}", timeout=2)

    def action_kill_process(self) -> None:
        try:
            table = self.query_one("#proc-table", DataTable)
            row_key = table.cursor_row
            if row_key is not None:
                row = table.get_row_at(row_key)
                pid = int(row[0])
                name = row[2]
                try:
                    os.kill(pid, signal.SIGTERM)
                    self.notify(f"已发送终止信号: {name} (PID {pid})", timeout=3)
                except ProcessLookupError:
                    self.notify(f"进程 {pid} 未找到", timeout=3)
                except PermissionError:
                    self.notify(f"权限不足: PID {pid}", timeout=3, severity="error")
        except (NoMatches, Exception) as e:
            self.notify(f"无法终止: {e}", timeout=3, severity="error")

    def action_refresh_now(self) -> None:
        self._refresh_metrics()
        self.notify("已刷新", timeout=1)

    def action_show_help(self) -> None:
        self.notify(
            "[bold]快捷键[/bold]\n\n"
            "  [bold]1 2 3 4 5[/bold]  切换标签页（总览 / Agent / 显卡 / 进程 / 网络）\n"
            "  [bold]t[/bold]        切换主题配色\n"
            "  [bold]s[/bold]        切换进程排序（CPU / 内存 / 显存 / 名称）\n"
            "  [bold]k[/bold]        终止选中进程\n"
            "  [bold]r[/bold]        立即刷新\n"
            "  [bold]q[/bold]        退出\n",
            timeout=8,
            title="帮助",
        )


def main():
    app = ServerMonitorApp()
    app.run()


if __name__ == "__main__":
    main()
