"""
Comprehensive system metrics collector for macOS and Linux.

Collects CPU, memory, GPU, disk, network, and process information using
psutil and platform-specific tooling (ioreg/system_profiler on macOS,
nvidia-smi on Linux/macOS-Intel).
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import psutil

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

HISTORY_MAXLEN = 120


@dataclass
class CpuInfo:
    """Snapshot of CPU utilisation."""

    total_percent: float
    per_core_percent: list[float]
    core_count_logical: int
    core_count_physical: int
    frequency_mhz: Optional[float] = None
    load_avg_1: Optional[float] = None
    load_avg_5: Optional[float] = None
    load_avg_15: Optional[float] = None


@dataclass
class MemInfo:
    """RAM and swap statistics (bytes)."""

    ram_total: int
    ram_used: int
    ram_available: int
    ram_percent: float
    swap_total: int
    swap_used: int
    swap_percent: float


@dataclass
class GpuInfo:
    """Per-GPU metrics."""

    index: int
    name: str
    utilization_percent: Optional[float] = None
    memory_used_mb: Optional[float] = None
    memory_total_mb: Optional[float] = None
    temperature_c: Optional[float] = None
    fan_speed_percent: Optional[float] = None
    power_draw_w: Optional[float] = None
    power_limit_w: Optional[float] = None


@dataclass
class NetProcessInfo:
    """进程网络速率."""
    pid: int = 0
    name: str = ""
    send_rate: float = 0.0   # bytes/sec
    recv_rate: float = 0.0   # bytes/sec
    total_rate: float = 0.0  # send + recv bytes/sec


@dataclass
class GpuProcessInfo:
    """A single process using GPU compute resources."""

    gpu_index: int
    pid: int
    process_name: str
    gpu_memory_used_mb: Optional[float] = None


@dataclass
class DiskInfo:
    """Per-partition disk usage."""

    device: str
    mountpoint: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float


@dataclass
class NetInfo:
    """Network I/O counters and computed rates."""

    bytes_sent: int
    bytes_recv: int
    send_rate_bytes_sec: float
    recv_rate_bytes_sec: float
    packets_sent: int
    packets_recv: int


@dataclass
class ProcessInfo:
    """Per-process resource snapshot."""

    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    gpu_memory_mb: Optional[float] = None


@dataclass
class AgentInfo:
    """A detected AI agent process."""

    pid: int
    name: str            # Agent display name (e.g. "Claude Code", "LangChain Agent")
    process_name: str    # Actual process name
    cmdline: str         # Full command line (truncated to 200 chars)
    cpu_percent: float
    memory_mb: float     # RSS memory in MB
    gpu_memory_mb: float # GPU memory in MB (0 if not using GPU)
    gpu_index: int       # Which GPU (-1 if none)
    uptime_seconds: float # How long running
    status: str          # running/sleeping etc
    agent_type: str = ""          # "推理服务" / "CLI工具" / "SDK应用" / "框架" / "Agent"
    model_name: str = ""          # Detected model name from cmdline/env
    children_count: int = 0       # Number of child processes
    total_cpu_percent: float = 0.0  # CPU% including children
    total_memory_mb: float = 0.0   # Memory including children
    thread_count: int = 0
    io_read_mb: float = 0.0       # Cumulative IO read in MB
    io_write_mb: float = 0.0      # Cumulative IO write in MB
    connections_count: int = 0    # Number of network connections
    listen_ports: list = field(default_factory=list)  # Ports this agent listens on
    mem_trend_mb_per_min: float = 0.0


# ---------------------------------------------------------------------------
# History helper
# ---------------------------------------------------------------------------

@dataclass
class _HistoryBuffer:
    """Fixed-length deque with timestamps."""

    values: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    timestamps: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))

    def append(self, value: float) -> None:
        self.values.append(value)
        self.timestamps.append(time.time())

    def snapshot(self) -> list[tuple[float, float]]:
        """Return list of (timestamp, value) pairs."""
        return list(zip(self.timestamps, self.values))


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class SystemCollector:
    """Collects system metrics on macOS and Linux.

    All public ``collect_*`` methods are safe to call at any time — internal
    errors (e.g. missing ``nvidia-smi``) are caught and handled gracefully.
    """

    def __init__(self) -> None:
        self._platform = platform.system()  # "Darwin" or "Linux"
        self._is_mac = self._platform == "Darwin"
        self._is_linux = self._platform == "Linux"
        self._is_apple_silicon = self._is_mac and platform.machine() == "arm64"

        # Network rate tracking
        self._prev_net_counters: Optional[psutil._common.snetio] = None
        self._prev_net_time: Optional[float] = None

        # History buffers
        self._cpu_history = _HistoryBuffer()
        self._mem_history = _HistoryBuffer()
        self._gpu_util_history: dict[int, _HistoryBuffer] = {}
        self._net_send_history = _HistoryBuffer()
        self._net_recv_history = _HistoryBuffer()

        # Agent memory trend tracking: pid -> [(timestamp, mem_mb)]
        self._agent_mem_history: dict[int, list[tuple[float, float]]] = {}

        # Per-process network rate tracking: pid -> (timestamp, bytes_sent, bytes_recv)
        self._proc_net_prev: dict[int, tuple[float, int, int]] = {}

        # 预热 psutil：第一次 cpu_percent(interval=0) 返回 0，需要先调一次
        psutil.cpu_percent(interval=0)
        psutil.cpu_percent(interval=0, percpu=True)
        # 预热 network counters
        self._prev_net_counters = psutil.net_io_counters()
        self._prev_net_time = time.time()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_cmd(cmd: list[str], timeout: int = 10) -> Optional[str]:
        """Run a subprocess and return stdout, or *None* on any failure."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                return result.stdout
            return None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    @staticmethod
    def _safe_float(value: str) -> Optional[float]:
        """Convert a string to float, returning *None* on failure."""
        try:
            return float(value.strip())
        except (ValueError, TypeError, AttributeError):
            return None

    # ------------------------------------------------------------------
    # CPU
    # ------------------------------------------------------------------

    def collect_cpu(self) -> CpuInfo:
        total = psutil.cpu_percent(interval=0)
        per_core = psutil.cpu_percent(interval=0, percpu=True)

        freq = psutil.cpu_freq()
        freq_mhz = freq.current if freq else None

        try:
            load1, load5, load15 = psutil.getloadavg()
        except (AttributeError, OSError):
            load1 = load5 = load15 = None

        info = CpuInfo(
            total_percent=total,
            per_core_percent=per_core,
            core_count_logical=psutil.cpu_count(logical=True) or 0,
            core_count_physical=psutil.cpu_count(logical=False) or 0,
            frequency_mhz=freq_mhz,
            load_avg_1=load1,
            load_avg_5=load5,
            load_avg_15=load15,
        )
        self._cpu_history.append(total)
        return info

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    def collect_memory(self) -> MemInfo:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()

        info = MemInfo(
            ram_total=vm.total,
            ram_used=vm.used,
            ram_available=vm.available,
            ram_percent=vm.percent,
            swap_total=sw.total,
            swap_used=sw.used,
            swap_percent=sw.percent,
        )
        self._mem_history.append(vm.percent)
        return info

    # ------------------------------------------------------------------
    # GPU
    # ------------------------------------------------------------------

    def collect_gpu(self) -> list[GpuInfo]:
        """Return GPU information for every detected GPU."""
        if self._is_mac:
            return self._collect_gpu_mac()
        if self._is_linux:
            return self._collect_gpu_linux_nvidia()
        return []

    # -- macOS paths ----------------------------------------------------

    def _collect_gpu_mac(self) -> list[GpuInfo]:
        if self._is_apple_silicon:
            return self._collect_gpu_apple_silicon()
        # Intel Mac — try NVIDIA first, fall back to integrated info.
        gpus = self._collect_gpu_nvidia_smi()
        if gpus:
            return gpus
        return self._collect_gpu_mac_integrated()

    def _collect_gpu_apple_silicon(self) -> list[GpuInfo]:
        name = "Apple GPU"

        # Attempt to get the chip name via system_profiler.
        sp_out = self._run_cmd(
            ["system_profiler", "SPDisplaysDataType", "-json"]
        )
        if sp_out:
            try:
                data = json.loads(sp_out)
                displays = data.get("SPDisplaysDataType", [])
                if displays:
                    name = displays[0].get("sppci_model", name)
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        # Unified memory via sysctl.
        mem_total_mb: Optional[float] = None
        sysctl_out = self._run_cmd(["sysctl", "-n", "hw.memsize"])
        if sysctl_out:
            val = self._safe_float(sysctl_out)
            if val is not None:
                mem_total_mb = val / (1024 * 1024)

        # Utilisation from IOAccelerator (best-effort).
        utilization = self._parse_ioreg_gpu_utilization()

        gpu = GpuInfo(
            index=0,
            name=name,
            utilization_percent=utilization,
            memory_total_mb=mem_total_mb,
        )

        self._record_gpu_util(0, utilization)
        return [gpu]

    def _parse_ioreg_gpu_utilization(self) -> Optional[float]:
        """Try to extract GPU utilisation from ``ioreg -r -d 1 -c IOAccelerator``."""
        raw = self._run_cmd(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"])
        if not raw:
            return None

        # Look for "Device Utilization %" or "GPU Activity(%)" patterns.
        patterns = [
            r'"Device Utilization %"\s*=\s*(\d+)',
            r'"GPU Activity\(%\)"\s*=\s*(\d+)',
            r'"gpu-core-utilization"\s*=\s*(\d+)',
        ]
        for pat in patterns:
            match = re.search(pat, raw)
            if match:
                return self._safe_float(match.group(1))
        return None

    def _collect_gpu_mac_integrated(self) -> list[GpuInfo]:
        """Fallback for Intel Macs with only integrated graphics."""
        name = "Integrated GPU"
        sp_out = self._run_cmd(
            ["system_profiler", "SPDisplaysDataType", "-json"]
        )
        if sp_out:
            try:
                data = json.loads(sp_out)
                displays = data.get("SPDisplaysDataType", [])
                if displays:
                    name = displays[0].get("sppci_model", name)
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        gpu = GpuInfo(index=0, name=name)
        self._record_gpu_util(0, None)
        return [gpu]

    # -- Linux / NVIDIA path -------------------------------------------

    def _collect_gpu_linux_nvidia(self) -> list[GpuInfo]:
        return self._collect_gpu_nvidia_smi()

    def _collect_gpu_nvidia_smi(self) -> list[GpuInfo]:
        """Parse ``nvidia-smi`` CSV output into :class:`GpuInfo` objects."""
        raw = self._run_cmd([
            "nvidia-smi",
            "--query-gpu=index,name,utilization.gpu,memory.used,"
            "memory.total,temperature.gpu,fan.speed,power.draw,power.limit",
            "--format=csv,noheader,nounits",
        ])
        if not raw:
            return []

        gpus: list[GpuInfo] = []
        for line in raw.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 9:
                continue

            idx_val = self._safe_float(parts[0])
            idx = int(idx_val) if idx_val is not None else len(gpus)
            util = self._safe_float(parts[2])

            gpu = GpuInfo(
                index=idx,
                name=parts[1],
                utilization_percent=util,
                memory_used_mb=self._safe_float(parts[3]),
                memory_total_mb=self._safe_float(parts[4]),
                temperature_c=self._safe_float(parts[5]),
                fan_speed_percent=self._safe_float(parts[6]),
                power_draw_w=self._safe_float(parts[7]),
                power_limit_w=self._safe_float(parts[8]),
            )
            gpus.append(gpu)
            self._record_gpu_util(idx, util)

        return gpus

    # ------------------------------------------------------------------
    # GPU processes
    # ------------------------------------------------------------------

    def collect_gpu_processes(self) -> list[GpuProcessInfo]:
        """List processes using GPU compute via ``nvidia-smi``."""
        raw = self._run_cmd([
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ])
        if not raw:
            return []

        # We also need a uuid→index mapping.
        uuid_map = self._nvidia_uuid_to_index()

        procs: list[GpuProcessInfo] = []
        for line in raw.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue

            gpu_uuid = parts[0]
            gpu_idx = uuid_map.get(gpu_uuid, 0)

            pid_f = self._safe_float(parts[1])
            pid = int(pid_f) if pid_f is not None else 0

            procs.append(GpuProcessInfo(
                gpu_index=gpu_idx,
                pid=pid,
                process_name=parts[2],
                gpu_memory_used_mb=self._safe_float(parts[3]),
            ))
        return procs

    def _nvidia_uuid_to_index(self) -> dict[str, int]:
        raw = self._run_cmd([
            "nvidia-smi",
            "--query-gpu=uuid,index",
            "--format=csv,noheader,nounits",
        ])
        if not raw:
            return {}
        mapping: dict[str, int] = {}
        for line in raw.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                idx = self._safe_float(parts[1])
                if idx is not None:
                    mapping[parts[0]] = int(idx)
        return mapping

    # ------------------------------------------------------------------
    # Disk
    # ------------------------------------------------------------------

    def collect_disk(self) -> list[DiskInfo]:
        disks: list[DiskInfo] = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            disks.append(DiskInfo(
                device=part.device,
                mountpoint=part.mountpoint,
                fstype=part.fstype,
                total=usage.total,
                used=usage.used,
                free=usage.free,
                percent=usage.percent,
            ))
        return disks

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def collect_network(self) -> NetInfo:
        counters = psutil.net_io_counters()
        now = time.time()

        send_rate = 0.0
        recv_rate = 0.0
        if self._prev_net_counters is not None and self._prev_net_time is not None:
            dt = now - self._prev_net_time
            if dt > 0:
                send_rate = (counters.bytes_sent - self._prev_net_counters.bytes_sent) / dt
                recv_rate = (counters.bytes_recv - self._prev_net_counters.bytes_recv) / dt

        self._prev_net_counters = counters
        self._prev_net_time = now

        info = NetInfo(
            bytes_sent=counters.bytes_sent,
            bytes_recv=counters.bytes_recv,
            send_rate_bytes_sec=max(send_rate, 0.0),
            recv_rate_bytes_sec=max(recv_rate, 0.0),
            packets_sent=counters.packets_sent,
            packets_recv=counters.packets_recv,
        )

        self._net_send_history.append(info.send_rate_bytes_sec)
        self._net_recv_history.append(info.recv_rate_bytes_sec)
        return info

    # ------------------------------------------------------------------
    # Processes
    # ------------------------------------------------------------------

    def collect_net_processes(self, limit: int = 5) -> list[NetProcessInfo]:
        """采集网速 Top N 进程.

        Linux: 用 psutil io_counters 差值。
        macOS: 用 nettop 命令（psutil 在 macOS 上无 per-process IO）。
        """
        if self._is_mac:
            return self._collect_net_processes_mac(limit)
        return self._collect_net_processes_linux(limit)

    def _collect_net_processes_linux(self, limit: int = 5) -> list[NetProcessInfo]:
        """Linux: 基于 psutil io_counters 差值."""
        results: list[NetProcessInfo] = []
        active_pids: set[int] = set()
        try:
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    conns = proc.connections()
                    if not conns:
                        continue
                    io = proc.io_counters()
                    pid = proc.pid
                    active_pids.add(pid)
                    now = time.time()
                    if pid in self._proc_net_prev:
                        prev_time, prev_sent, prev_recv = self._proc_net_prev[pid]
                        dt = now - prev_time
                        if dt > 0:
                            send_rate = max(0, (io.write_bytes - prev_sent) / dt)
                            recv_rate = max(0, (io.read_bytes - prev_recv) / dt)
                            results.append(NetProcessInfo(
                                pid=pid, name=proc.name(),
                                send_rate=send_rate, recv_rate=recv_rate,
                                total_rate=send_rate + recv_rate,
                            ))
                    self._proc_net_prev[pid] = (now, io.write_bytes, io.read_bytes)
                except (psutil.NoSuchProcess, psutil.AccessDenied,
                        psutil.ZombieProcess, AttributeError):
                    continue
        except Exception:
            pass
        stale = [p for p in self._proc_net_prev if p not in active_pids]
        for p in stale:
            del self._proc_net_prev[p]
        results.sort(key=lambda x: x.total_rate, reverse=True)
        return results[:limit]

    def _collect_net_processes_mac(self, limit: int = 5) -> list[NetProcessInfo]:
        """macOS: 用 lsof + io delta 追踪有网络连接的进程流量.

        策略:
        1. lsof -i -n -P 列出有网络连接的进程（不需要 root）
        2. 对这些进程追踪 psutil.Process.memory_info 的 RSS 变化作为活跃度参考
        3. 按连接数排序（macOS 无法获取 per-process 网速）
        """
        # 用 lsof 获取有网络连接的进程
        pid_conns: dict[int, tuple[str, int]] = {}  # pid -> (name, connection_count)
        try:
            out = subprocess.run(
                ["lsof", "-i", "-n", "-P", "-F", "pcn"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout:
                current_pid = None
                current_name = ""
                for line in out.stdout.strip().split("\n"):
                    if line.startswith("p"):
                        try:
                            current_pid = int(line[1:])
                        except ValueError:
                            current_pid = None
                    elif line.startswith("c") and current_pid is not None:
                        current_name = line[1:]
                    elif line.startswith("n") and current_pid is not None:
                        if current_pid in pid_conns:
                            old_name, old_count = pid_conns[current_pid]
                            pid_conns[current_pid] = (old_name, old_count + 1)
                        else:
                            pid_conns[current_pid] = (current_name, 1)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        if not pid_conns:
            # lsof 也失败了，尝试遍历进程
            try:
                for proc in psutil.process_iter(['pid', 'name']):
                    try:
                        conns = proc.connections()
                        if conns:
                            pid_conns[proc.pid] = (proc.name(), len(conns))
                    except (psutil.AccessDenied, psutil.NoSuchProcess,
                            psutil.ZombieProcess):
                        continue
            except Exception:
                pass

        # 构建结果：追踪 IO delta（如果可用）
        results: list[NetProcessInfo] = []
        active_pids: set[int] = set()

        for pid, (name, conn_count) in pid_conns.items():
            active_pids.add(pid)
            send_rate = 0.0
            recv_rate = 0.0
            now = time.time()

            # macOS 上部分进程可能支持 io_counters
            try:
                proc = psutil.Process(pid)
                io = proc.io_counters()
                if pid in self._proc_net_prev:
                    prev_time, prev_sent, prev_recv = self._proc_net_prev[pid]
                    dt = now - prev_time
                    if dt > 0:
                        send_rate = max(0, (io.write_bytes - prev_sent) / dt)
                        recv_rate = max(0, (io.read_bytes - prev_recv) / dt)
                self._proc_net_prev[pid] = (now, io.write_bytes, io.read_bytes)
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess, AttributeError):
                # io_counters 不可用，用连接数作为排序权重
                send_rate = 0.0
                recv_rate = 0.0

            results.append(NetProcessInfo(
                pid=pid, name=name,
                send_rate=send_rate, recv_rate=recv_rate,
                total_rate=send_rate + recv_rate if (send_rate + recv_rate) > 0 else float(conn_count),
            ))

        # 清理已退出进程
        stale = [p for p in self._proc_net_prev if p not in active_pids]
        for p in stale:
            del self._proc_net_prev[p]

        results.sort(key=lambda x: x.total_rate, reverse=True)
        return results[:limit]

    def collect_processes(self, limit: int = 20) -> list[ProcessInfo]:
        """Top *limit* processes sorted by CPU%, with optional GPU memory."""
        # Build pid → gpu_memory mapping from GPU processes.
        gpu_mem_by_pid: dict[int, float] = {}
        for gp in self.collect_gpu_processes():
            if gp.gpu_memory_used_mb is not None:
                gpu_mem_by_pid[gp.pid] = (
                    gpu_mem_by_pid.get(gp.pid, 0.0) + gp.gpu_memory_used_mb
                )

        procs: list[ProcessInfo] = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                info = proc.info
                procs.append(ProcessInfo(
                    pid=info["pid"],
                    name=info["name"] or "",
                    cpu_percent=info["cpu_percent"] or 0.0,
                    memory_percent=info["memory_percent"] or 0.0,
                    gpu_memory_mb=gpu_mem_by_pid.get(info["pid"]),
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        procs.sort(key=lambda p: p.cpu_percent, reverse=True)
        return procs[:limit]

    # ------------------------------------------------------------------
    # AI Agent detection
    # ------------------------------------------------------------------

    # Pattern → friendly display name mapping for agent detection.
    _AGENT_PATTERNS: list[tuple[re.Pattern, str]] = [
        (re.compile(r"claude", re.IGNORECASE), "Claude Code"),
        (re.compile(r"anthropic", re.IGNORECASE), "Anthropic Agent"),
        (re.compile(r"langgraph", re.IGNORECASE), "LangGraph Agent"),
        (re.compile(r"langchain", re.IGNORECASE), "LangChain Agent"),
        (re.compile(r"autogen", re.IGNORECASE), "AutoGen Agent"),
        (re.compile(r"crewai", re.IGNORECASE), "CrewAI Agent"),
        (re.compile(r"crew", re.IGNORECASE), "Crew Agent"),
        (re.compile(r"openai", re.IGNORECASE), "OpenAI Agent"),
        (re.compile(r"llamaindex", re.IGNORECASE), "LlamaIndex Agent"),
        (re.compile(r"dspy", re.IGNORECASE), "DSPy Agent"),
        (re.compile(r"metagpt", re.IGNORECASE), "MetaGPT Agent"),
        (re.compile(r"chatglm", re.IGNORECASE), "ChatGLM"),
        (re.compile(r"vllm", re.IGNORECASE), "vLLM Server"),
        (re.compile(r"text-generation", re.IGNORECASE), "Text Generation Server"),
        (re.compile(r"ollama", re.IGNORECASE), "Ollama"),
        (re.compile(r"lmstudio", re.IGNORECASE), "LM Studio"),
        (re.compile(r"transformers.*(pipeline|generate)", re.IGNORECASE), "Transformers Pipeline"),
    ]
    # Catch-all: any python process with "agent" in the cmdline.
    _AGENT_PYTHON_PATTERN = re.compile(r"agent", re.IGNORECASE)

    @staticmethod
    def _classify_agent_type(cmdline_str: str) -> str:
        """Classify agent type based on command line."""
        # 推理服务
        for kw in ("vllm", "text-generation", "ollama", "lmstudio"):
            if kw in cmdline_str.lower():
                return "推理服务"
        if "--serve" in cmdline_str or "--server" in cmdline_str:
            return "推理服务"
        # CLI工具 (claude without serve)
        if "claude" in cmdline_str.lower() and "--serve" not in cmdline_str and "--server" not in cmdline_str:
            return "CLI工具"
        # SDK应用
        for kw in ("openai", "anthropic", "langchain", "llamaindex", "dspy"):
            if kw in cmdline_str.lower():
                return "SDK应用"
        # 框架
        for kw in ("autogen", "crewai", "metagpt"):
            if kw in cmdline_str.lower():
                return "框架"
        return "Agent"

    @staticmethod
    def _detect_model_name(cmdline_str: str) -> str:
        """Detect model name from command line arguments."""
        # Try to find --model, --model-name, --model_name, -m followed by a value
        match = re.search(r'(?:--model(?:[-_]name)?|-m)\s+(\S+)', cmdline_str)
        if match:
            return match.group(1)
        # Check common model name patterns
        for pattern in (
            r'(gpt-4[o0-9a-z\-]*)',
            r'(gpt-3\.5[a-z\-]*)',
            r'(claude[a-z0-9\-\.]*)',
            r'(llama[a-z0-9\-\.]*)',
            r'(qwen[a-z0-9\-\.]*)',
            r'(mistral[a-z0-9\-\.]*)',
            r'(gemma[a-z0-9\-\.]*)',
            r'(deepseek[a-z0-9\-\.]*)',
            r'(yi[a-z0-9\-\.]*)',
            r'(baichuan[a-z0-9\-\.]*)',
            r'(chatglm[a-z0-9\-\.]*)',
            r'(vicuna[a-z0-9\-\.]*)',
            r'(phi[a-z0-9\-\.]*)',
        ):
            m = re.search(pattern, cmdline_str, re.IGNORECASE)
            if m:
                return m.group(1)
        return ""

    def get_agent_memory_trend(self, pid: int) -> float:
        """Return memory change rate in MB/min. Positive = growing."""
        history = self._agent_mem_history.get(pid, [])
        if len(history) < 10:
            return 0.0
        oldest_time, oldest_mem = history[0]
        newest_time, newest_mem = history[-1]
        dt_min = (newest_time - oldest_time) / 60
        if dt_min <= 0:
            return 0.0
        return (newest_mem - oldest_mem) / dt_min

    def collect_agents(self) -> list[AgentInfo]:
        """Scan running processes and return detected AI agent processes."""
        now = time.time()

        # Build pid → (gpu_memory_mb, gpu_index) from GPU process list.
        gpu_info_by_pid: dict[int, tuple[float, int]] = {}
        try:
            for gp in self.collect_gpu_processes():
                mem = gp.gpu_memory_used_mb or 0.0
                existing = gpu_info_by_pid.get(gp.pid)
                if existing is None or mem > existing[0]:
                    gpu_info_by_pid[gp.pid] = (mem, gp.gpu_index)
        except Exception:
            pass

        agents: list[AgentInfo] = []

        for proc in psutil.process_iter(
            ["pid", "name", "cpu_percent", "memory_info", "status", "create_time"]
        ):
            try:
                pinfo = proc.info
                pid = pinfo["pid"]
                proc_name = pinfo["name"] or ""

                try:
                    cmdline_parts = proc.cmdline()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

                if not cmdline_parts:
                    continue

                cmdline = " ".join(cmdline_parts)
                cmdline_truncated = cmdline[:200]

                # Try to match against known patterns.
                matched_name: Optional[str] = None
                for pattern, display_name in self._AGENT_PATTERNS:
                    if pattern.search(cmdline):
                        matched_name = display_name
                        break

                # Catch-all: python process with "agent" in cmdline.
                if matched_name is None:
                    is_python = any(
                        p in proc_name.lower() for p in ("python", "python3")
                    ) or cmdline_parts[0].lower().endswith(("python", "python3"))
                    if is_python and self._AGENT_PYTHON_PATTERN.search(cmdline):
                        matched_name = "Python Agent"

                if matched_name is None:
                    continue

                mem_info = pinfo.get("memory_info")
                memory_mb = (mem_info.rss / (1024 * 1024)) if mem_info else 0.0

                cpu_pct = pinfo.get("cpu_percent") or 0.0

                status = pinfo.get("status") or "unknown"

                create_time = pinfo.get("create_time")
                uptime = (now - create_time) if create_time else 0.0

                gpu_mem, gpu_idx = gpu_info_by_pid.get(pid, (0.0, -1))

                # Agent type classification
                agent_type = self._classify_agent_type(cmdline)

                # Model name detection (use original cmdline, not lowered)
                model_name = self._detect_model_name(cmdline)

                # Child processes, total CPU/memory
                children_count = 0
                total_cpu = cpu_pct
                total_mem = memory_mb
                try:
                    p = psutil.Process(pid)
                    children = p.children(recursive=True)
                    children_count = len(children)
                    for child in children:
                        try:
                            total_cpu += child.cpu_percent(interval=0)
                            child_mem = child.memory_info()
                            if child_mem:
                                total_mem += child_mem.rss / (1024 * 1024)
                        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                            continue
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass

                # Thread count
                thread_count = 0
                try:
                    thread_count = psutil.Process(pid).num_threads()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass

                # IO counters (Linux only)
                io_read_mb = 0.0
                io_write_mb = 0.0
                try:
                    io = psutil.Process(pid).io_counters()
                    io_read_mb = io.read_bytes / (1024 * 1024)
                    io_write_mb = io.write_bytes / (1024 * 1024)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, AttributeError):
                    pass

                # Network connections
                connections_count = 0
                listen_ports: list[int] = []
                try:
                    conns = psutil.Process(pid).connections()
                    connections_count = len(conns)
                    for conn in conns:
                        if conn.status == "LISTEN" and conn.laddr:
                            listen_ports.append(conn.laddr.port)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass

                # Memory trend tracking
                if pid not in self._agent_mem_history:
                    self._agent_mem_history[pid] = []
                self._agent_mem_history[pid].append((now, memory_mb))
                # Keep last 60 entries
                self._agent_mem_history[pid] = self._agent_mem_history[pid][-60:]

                mem_trend = self.get_agent_memory_trend(pid)

                agents.append(AgentInfo(
                    pid=pid,
                    name=matched_name,
                    process_name=proc_name,
                    cmdline=cmdline_truncated,
                    cpu_percent=cpu_pct,
                    memory_mb=round(memory_mb, 2),
                    gpu_memory_mb=round(gpu_mem, 2),
                    gpu_index=gpu_idx,
                    uptime_seconds=round(uptime, 1),
                    status=status,
                    agent_type=agent_type,
                    model_name=model_name,
                    children_count=children_count,
                    total_cpu_percent=total_cpu,
                    total_memory_mb=round(total_mem, 2),
                    thread_count=thread_count,
                    io_read_mb=round(io_read_mb, 2),
                    io_write_mb=round(io_write_mb, 2),
                    connections_count=connections_count,
                    listen_ports=listen_ports,
                    mem_trend_mb_per_min=round(mem_trend, 4),
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue

        # Clean up stale PIDs from memory history
        active_pids = {a.pid for a in agents}
        stale = [pid for pid in self._agent_mem_history if pid not in active_pids]
        for pid in stale:
            del self._agent_mem_history[pid]

        # Sort by GPU memory desc, then CPU desc.
        agents.sort(key=lambda a: (-a.gpu_memory_mb, -a.cpu_percent))
        return agents

    def calculate_capacity(self) -> dict:
        """Estimate how many more AI agents the system can support."""
        GPU_PER_AGENT_MB = 4096.0
        RAM_PER_AGENT_MB = 2048.0
        CPU_CORES_PER_AGENT = 2.0

        # GPU free memory.
        gpu_free_mb = 0.0
        try:
            gpus = self.collect_gpu()
            for g in gpus:
                total = g.memory_total_mb or 0.0
                used = g.memory_used_mb or 0.0
                gpu_free_mb += max(total - used, 0.0)
        except Exception:
            pass

        # RAM free.
        ram_free_mb = 0.0
        try:
            vm = psutil.virtual_memory()
            ram_free_mb = vm.available / (1024 * 1024)
        except Exception:
            pass

        # CPU free cores.
        cpu_free_cores = 0.0
        try:
            logical = psutil.cpu_count(logical=True) or 0
            cpu_pct = psutil.cpu_percent(interval=0)
            cpu_free_cores = logical * (1.0 - cpu_pct / 100.0)
        except Exception:
            pass

        # Running agents count.
        try:
            running_agents = self.collect_agents()
            running_agent_count = len(running_agents)
        except Exception:
            running_agent_count = 0

        max_by_gpu = int(gpu_free_mb / GPU_PER_AGENT_MB) if gpu_free_mb > 0 else 0
        max_by_ram = int(ram_free_mb / RAM_PER_AGENT_MB) if ram_free_mb > 0 else 0
        max_by_cpu = int(cpu_free_cores / CPU_CORES_PER_AGENT) if cpu_free_cores > 0 else 0

        # If no GPU is available, don't let GPU be the bottleneck.
        if gpu_free_mb == 0.0:
            recommended = min(max_by_ram, max_by_cpu)
        else:
            recommended = min(max_by_gpu, max_by_ram, max_by_cpu)

        return {
            "max_agents_by_gpu": max_by_gpu,
            "max_agents_by_ram": max_by_ram,
            "max_agents_by_cpu": max_by_cpu,
            "recommended_parallel": max(recommended, 0),
            "gpu_free_mb": round(gpu_free_mb, 2),
            "ram_free_mb": round(ram_free_mb, 2),
            "cpu_free_cores": round(cpu_free_cores, 2),
            "running_agent_count": running_agent_count,
        }

    # ------------------------------------------------------------------
    # History accessors
    # ------------------------------------------------------------------

    def collect(self) -> dict:
        """一次性采集所有指标，返回 _refresh() 期望的 dict 格式.

        字段名严格对应 main_window.py _refresh() 中的 data.get() 调用。
        """
        cpu = self.collect_cpu()
        mem = self.collect_memory()
        gpus = self.collect_gpu()
        gpu_procs = self.collect_gpu_processes()
        disks = self.collect_disk()
        net = self.collect_network()
        procs = self.collect_processes()
        agents = self.collect_agents()
        capacity = self.calculate_capacity()

        gpu0 = gpus[0] if gpus else None

        # 磁盘取根分区或第一个
        disk_pct = 0.0
        for d in disks:
            if d.mountpoint == "/":
                disk_pct = d.percent
                break
        if disk_pct == 0.0 and disks:
            disk_pct = disks[0].percent

        return {
            # CPU — CpuInfo.total_percent
            "cpu_percent": cpu.total_percent,
            # Memory — MemInfo.ram_percent
            "memory_percent": mem.ram_percent,
            # GPU — GpuInfo 字段带 _percent/_mb/_c/_w 后缀
            "gpu_util": gpu0.utilization_percent or 0.0 if gpu0 else 0.0,
            "gpu_temp": gpu0.temperature_c or 0.0 if gpu0 else 0.0,
            "gpu_name": gpu0.name if gpu0 else "—",
            "gpu_memory_used": int(gpu0.memory_used_mb or 0) if gpu0 else 0,
            "gpu_memory_total": int(gpu0.memory_total_mb or 0) if gpu0 else 0,
            "gpu_power": gpu0.power_draw_w or 0.0 if gpu0 else 0.0,
            # Disk
            "disk_percent": disk_pct,
            # Network — NetInfo 字段: recv_rate_bytes_sec / send_rate_bytes_sec
            "net_download_speed": net.recv_rate_bytes_sec,
            "net_upload_speed": net.send_rate_bytes_sec,
            "net_bytes_recv": net.bytes_recv,
            "net_bytes_sent": net.bytes_sent,
            # Net processes — NetProcessInfo (top 5 by network speed)
            "net_processes": [
                {"pid": p.pid, "name": p.name, "send_rate": p.send_rate,
                 "recv_rate": p.recv_rate, "total_rate": p.total_rate}
                for p in self.collect_net_processes(limit=5)
            ],
            # GPU processes — GpuProcessInfo
            "gpu_processes": [
                {
                    "pid": p.pid,
                    "name": p.process_name,
                    "gpu_memory": p.gpu_memory_used_mb or 0,
                    "gpu_util": 0.0,
                }
                for p in gpu_procs
            ],
            # Processes — ProcessInfo (no username/status fields)
            "processes": [
                {
                    "pid": p.pid,
                    "user": "",
                    "name": p.name,
                    "cpu_percent": p.cpu_percent,
                    "memory_percent": p.memory_percent,
                    "gpu_memory": p.gpu_memory_mb or 0,
                    "status": "",
                }
                for p in procs
            ],
            # AI Agents — AgentInfo
            "agents": [
                {
                    "pid": a.pid,
                    "name": a.name,
                    "process_name": a.process_name,
                    "cmdline": a.cmdline,
                    "cpu_percent": a.cpu_percent,
                    "memory_mb": a.memory_mb,
                    "gpu_memory_mb": a.gpu_memory_mb,
                    "gpu_index": a.gpu_index,
                    "uptime_seconds": a.uptime_seconds,
                    "status": a.status,
                    "agent_type": a.agent_type,
                    "model_name": a.model_name,
                    "children_count": a.children_count,
                    "total_cpu_percent": a.total_cpu_percent,
                    "total_memory_mb": a.total_memory_mb,
                    "thread_count": a.thread_count,
                    "io_read_mb": a.io_read_mb,
                    "io_write_mb": a.io_write_mb,
                    "connections_count": a.connections_count,
                    "listen_ports": a.listen_ports,
                    "mem_trend_mb_per_min": a.mem_trend_mb_per_min,
                }
                for a in agents
            ],
            # Capacity estimation
            "capacity": capacity,
        }

    def _record_gpu_util(self, index: int, value: Optional[float]) -> None:
        if value is None:
            return
        if index not in self._gpu_util_history:
            self._gpu_util_history[index] = _HistoryBuffer()
        self._gpu_util_history[index].append(value)

    def get_cpu_history(self) -> list[tuple[float, float]]:
        """Return up to 120 ``(timestamp, cpu_percent)`` pairs."""
        return self._cpu_history.snapshot()

    def get_memory_history(self) -> list[tuple[float, float]]:
        """Return up to 120 ``(timestamp, mem_percent)`` pairs."""
        return self._mem_history.snapshot()

    def get_gpu_utilization_history(
        self, gpu_index: int = 0
    ) -> list[tuple[float, float]]:
        """Return up to 120 ``(timestamp, gpu_util_percent)`` pairs."""
        buf = self._gpu_util_history.get(gpu_index)
        if buf is None:
            return []
        return buf.snapshot()

    def get_network_send_history(self) -> list[tuple[float, float]]:
        """Return up to 120 ``(timestamp, bytes_per_sec)`` pairs."""
        return self._net_send_history.snapshot()

    def get_network_recv_history(self) -> list[tuple[float, float]]:
        """Return up to 120 ``(timestamp, bytes_per_sec)`` pairs."""
        return self._net_recv_history.snapshot()
