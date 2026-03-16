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
