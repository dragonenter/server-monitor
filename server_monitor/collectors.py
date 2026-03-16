"""System metrics collectors for NVIDIA GPU, Apple M-series, CPU, Memory, Disk, Network."""

import platform
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CpuInfo:
    percent_total: float = 0.0
    percent_per_core: list[float] = field(default_factory=list)
    load_1: float = 0.0
    load_5: float = 0.0
    load_15: float = 0.0
    temp: Optional[float] = None
    freq_current: Optional[float] = None
    core_count: int = 0


@dataclass
class MemInfo:
    total: int = 0
    used: int = 0
    percent: float = 0.0
    swap_total: int = 0
    swap_used: int = 0
    swap_percent: float = 0.0


@dataclass
class GpuInfo:
    index: int = 0
    name: str = ""
    utilization: float = 0.0
    memory_used: float = 0.0
    memory_total: float = 0.0
    memory_percent: float = 0.0
    temperature: float = 0.0
    fan_speed: Optional[float] = None
    power_draw: float = 0.0
    power_limit: float = 0.0
    backend: str = "nvidia"  # "nvidia" or "apple"


@dataclass
class DiskInfo:
    mountpoint: str = ""
    total: int = 0
    used: int = 0
    percent: float = 0.0
    device: str = ""


@dataclass
class NetInfo:
    bytes_sent_per_sec: float = 0.0
    bytes_recv_per_sec: float = 0.0
    total_sent: int = 0
    total_recv: int = 0


@dataclass
class GpuProcessInfo:
    pid: int = 0
    name: str = ""
    gpu_index: int = 0
    gpu_memory: float = 0.0  # MB
    type: str = ""  # C=Compute, G=Graphics


@dataclass
class ProcessInfo:
    pid: int = 0
    name: str = ""
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    gpu_memory: Optional[float] = None  # MB
    username: str = ""
    status: str = ""


@dataclass
class AgentInfo:
    """检测到的 AI Agent 进程."""
    pid: int = 0
    name: str = ""           # 显示名 (e.g. "Claude Code")
    process_name: str = ""   # 实际进程名
    cmdline: str = ""        # 命令行（截断200字符）
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    gpu_memory_mb: float = 0.0
    gpu_index: int = -1
    uptime_seconds: float = 0.0
    status: str = ""


# Agent 识别模式: (命令行关键词, 显示名)
_AGENT_PATTERNS: list[tuple[str, str]] = [
    ("claude", "Claude Code"),
    ("anthropic", "Anthropic SDK"),
    ("langchain", "LangChain"),
    ("langgraph", "LangGraph"),
    ("autogen", "AutoGen"),
    ("crewai", "CrewAI"),
    ("crew", "CrewAI"),
    ("openai", "OpenAI Agent"),
    ("llamaindex", "LlamaIndex"),
    ("dspy", "DSPy"),
    ("metagpt", "MetaGPT"),
    ("chatglm", "ChatGLM"),
    ("vllm", "vLLM"),
    ("text-generation", "TGI"),
    ("ollama", "Ollama"),
    ("lmstudio", "LM Studio"),
    ("transformers", "Transformers"),
]


class MetricsCollector:
    def __init__(self):
        import psutil
        self.psutil = psutil
        self._net_prev = psutil.net_io_counters()
        self._net_prev_time = time.time()
        self._gpu_backend = self._detect_gpu_backend()
        self._cpu_history: list[float] = []
        self._gpu_histories: dict[int, list[float]] = {}
        self._mem_history: list[float] = []
        self._net_send_history: list[float] = []
        self._net_recv_history: list[float] = []
        self._history_max = 60  # 60 seconds

    def _detect_gpu_backend(self) -> str:
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            return "apple"
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return "nvidia"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "none"

    @property
    def gpu_backend(self) -> str:
        return self._gpu_backend

    def collect_cpu(self) -> CpuInfo:
        ps = self.psutil
        per_core = ps.cpu_percent(interval=0, percpu=True)
        total = sum(per_core) / max(len(per_core), 1)
        load = ps.getloadavg()
        temp = None
        try:
            temps = ps.sensors_temperatures()
            if temps:
                for key in ("coretemp", "cpu_thermal", "k10temp", "zenpower"):
                    if key in temps and temps[key]:
                        temp = max(t.current for t in temps[key])
                        break
                if temp is None:
                    first = list(temps.values())[0]
                    if first:
                        temp = first[0].current
        except (AttributeError, Exception):
            pass
        freq = None
        try:
            f = ps.cpu_freq()
            if f:
                freq = f.current
        except Exception:
            pass
        info = CpuInfo(
            percent_total=total,
            percent_per_core=per_core,
            load_1=load[0], load_5=load[1], load_15=load[2],
            temp=temp,
            freq_current=freq,
            core_count=len(per_core),
        )
        self._cpu_history.append(total)
        if len(self._cpu_history) > self._history_max:
            self._cpu_history.pop(0)
        return info

    def collect_memory(self) -> MemInfo:
        ps = self.psutil
        vm = ps.virtual_memory()
        sw = ps.swap_memory()
        info = MemInfo(
            total=vm.total, used=vm.used, percent=vm.percent,
            swap_total=sw.total, swap_used=sw.used, swap_percent=sw.percent,
        )
        self._mem_history.append(vm.percent)
        if len(self._mem_history) > self._history_max:
            self._mem_history.pop(0)
        return info

    def collect_gpu(self) -> list[GpuInfo]:
        if self._gpu_backend == "nvidia":
            return self._collect_nvidia()
        elif self._gpu_backend == "apple":
            return self._collect_apple()
        return []

    def _collect_nvidia(self) -> list[GpuInfo]:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,fan.speed,power.draw,power.limit",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return []
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        gpus = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 9:
                continue
            idx = int(parts[0])
            mem_used = float(parts[3])
            mem_total = float(parts[4])
            mem_pct = (mem_used / mem_total * 100) if mem_total > 0 else 0
            fan = None
            try:
                fan = float(parts[6])
            except (ValueError, IndexError):
                pass
            util = 0.0
            try:
                util = float(parts[2])
            except ValueError:
                pass
            gpu = GpuInfo(
                index=idx,
                name=parts[1],
                utilization=util,
                memory_used=mem_used,
                memory_total=mem_total,
                memory_percent=mem_pct,
                temperature=float(parts[5]),
                fan_speed=fan,
                power_draw=float(parts[7]) if parts[7] not in ("[N/A]", "") else 0,
                power_limit=float(parts[8]) if parts[8] not in ("[N/A]", "") else 0,
                backend="nvidia",
            )
            gpus.append(gpu)
            if idx not in self._gpu_histories:
                self._gpu_histories[idx] = []
            self._gpu_histories[idx].append(util)
            if len(self._gpu_histories[idx]) > self._history_max:
                self._gpu_histories[idx].pop(0)
        return gpus

    def _collect_apple(self) -> list[GpuInfo]:
        """Collect Apple M-series GPU metrics via powermetrics or ioreg."""
        try:
            # Try using system_profiler for basic info
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType", "-json"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return []
            import json
            data = json.loads(result.stdout)
            displays = data.get("SPDisplaysDataType", [])
            if not displays:
                return []

            gpu = GpuInfo(
                index=0,
                name=displays[0].get("sppci_model", "Apple GPU"),
                backend="apple",
            )

            # Try to get utilization from ioreg
            try:
                ioreg = subprocess.run(
                    ["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in ioreg.stdout.split("\n"):
                    if "PerformanceStatistics" in line or "Device Utilization" in line:
                        # Parse utilization percentage
                        import re
                        match = re.search(r"(\d+)", line)
                        if match:
                            gpu.utilization = float(match.group(1))
                            break
            except Exception:
                pass

            # Try to get memory info from sysctl
            try:
                result2 = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, timeout=5,
                )
                if result2.returncode == 0:
                    # Apple unified memory - GPU shares system memory
                    total_mem = int(result2.stdout.strip()) / (1024 ** 2)  # MB
                    gpu.memory_total = total_mem
                    # Rough estimation from system memory pressure
                    gpu.memory_used = 0  # Precise value requires sudo powermetrics
            except Exception:
                pass

            if 0 not in self._gpu_histories:
                self._gpu_histories[0] = []
            self._gpu_histories[0].append(gpu.utilization)
            if len(self._gpu_histories[0]) > self._history_max:
                self._gpu_histories[0].pop(0)

            return [gpu]
        except Exception:
            return []

    def collect_disk(self) -> list[DiskInfo]:
        disks = []
        seen = set()
        for part in self.psutil.disk_partitions(all=False):
            if part.mountpoint in seen:
                continue
            if part.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay"):
                continue
            seen.add(part.mountpoint)
            try:
                usage = self.psutil.disk_usage(part.mountpoint)
                disks.append(DiskInfo(
                    mountpoint=part.mountpoint,
                    total=usage.total,
                    used=usage.used,
                    percent=usage.percent,
                    device=part.device,
                ))
            except PermissionError:
                continue
        return disks

    def collect_network(self) -> NetInfo:
        now = time.time()
        current = self.psutil.net_io_counters()
        dt = now - self._net_prev_time
        if dt <= 0:
            dt = 1.0
        send_rate = (current.bytes_sent - self._net_prev.bytes_sent) / dt
        recv_rate = (current.bytes_recv - self._net_prev.bytes_recv) / dt
        self._net_prev = current
        self._net_prev_time = now

        self._net_send_history.append(send_rate)
        self._net_recv_history.append(recv_rate)
        if len(self._net_send_history) > self._history_max:
            self._net_send_history.pop(0)
        if len(self._net_recv_history) > self._history_max:
            self._net_recv_history.pop(0)

        return NetInfo(
            bytes_sent_per_sec=send_rate,
            bytes_recv_per_sec=recv_rate,
            total_sent=current.bytes_sent,
            total_recv=current.bytes_recv,
        )

    def collect_processes(self, limit: int = 15) -> list[ProcessInfo]:
        procs = []
        for p in self.psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "username", "status"]):
            try:
                info = p.info
                procs.append(ProcessInfo(
                    pid=info["pid"],
                    name=info["name"] or "",
                    cpu_percent=info["cpu_percent"] or 0.0,
                    memory_percent=info["memory_percent"] or 0.0,
                    username=info["username"] or "",
                    status=info["status"] or "",
                ))
            except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
                continue
        # Collect GPU process memory if nvidia
        gpu_proc_mem = {}
        if self._gpu_backend == "nvidia":
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        if not line.strip():
                            continue
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) >= 2:
                            try:
                                gpu_proc_mem[int(parts[0])] = float(parts[1])
                            except ValueError:
                                pass
            except Exception:
                pass

        for proc in procs:
            if proc.pid in gpu_proc_mem:
                proc.gpu_memory = gpu_proc_mem[proc.pid]

        procs.sort(key=lambda x: x.cpu_percent, reverse=True)
        return procs[:limit]

    def collect_gpu_processes(self) -> list[GpuProcessInfo]:
        """采集占用显存的进程列表."""
        if self._gpu_backend != "nvidia":
            return []
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,gpu_uuid,used_memory,process_name",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return []
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        # 获取 gpu_uuid -> index 的映射
        uuid_map: dict[str, int] = {}
        try:
            r2 = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if r2.returncode == 0:
                for line in r2.stdout.strip().split("\n"):
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 2:
                        uuid_map[parts[1]] = int(parts[0])
        except Exception:
            pass

        procs: list[GpuProcessInfo] = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            try:
                pid = int(parts[0])
                uuid = parts[1]
                mem = float(parts[2])
                name = parts[3].split("/")[-1]  # 只取进程名
                gpu_idx = uuid_map.get(uuid, 0)
                procs.append(GpuProcessInfo(
                    pid=pid, name=name, gpu_index=gpu_idx, gpu_memory=mem,
                ))
            except (ValueError, IndexError):
                continue
        procs.sort(key=lambda p: p.gpu_memory, reverse=True)
        return procs

    def collect_agents(self) -> list[AgentInfo]:
        """识别系统中运行的 AI Agent 进程."""
        ps = self.psutil
        gpu_procs = self.collect_gpu_processes()
        gpu_mem_by_pid: dict[int, tuple[float, int]] = {}  # pid -> (mem_mb, gpu_idx)
        for gp in gpu_procs:
            existing = gpu_mem_by_pid.get(gp.pid, (0.0, gp.gpu_index))
            gpu_mem_by_pid[gp.pid] = (existing[0] + gp.gpu_memory, gp.gpu_index)

        agents: list[AgentInfo] = []
        seen_pids: set[int] = set()

        for proc in ps.process_iter(["pid", "name", "cpu_percent", "memory_info", "status", "create_time", "cmdline"]):
            try:
                info = proc.info
                pid = info["pid"]
                if pid in seen_pids:
                    continue
                cmdline_list = info.get("cmdline") or []
                cmdline_str = " ".join(cmdline_list).lower()
                if not cmdline_str:
                    continue

                matched_name = None
                for keyword, display_name in _AGENT_PATTERNS:
                    if keyword in cmdline_str:
                        matched_name = display_name
                        break
                # 通用匹配：python 进程命令行含 "agent"
                if not matched_name and "python" in (info["name"] or "").lower():
                    if "agent" in cmdline_str:
                        matched_name = "AI Agent"

                if not matched_name:
                    continue

                seen_pids.add(pid)
                mem_info = info.get("memory_info")
                mem_mb = (mem_info.rss / (1024 * 1024)) if mem_info else 0.0
                create_time = info.get("create_time") or time.time()
                uptime = time.time() - create_time
                gpu_mem, gpu_idx = gpu_mem_by_pid.get(pid, (0.0, -1))

                agents.append(AgentInfo(
                    pid=pid,
                    name=matched_name,
                    process_name=info["name"] or "",
                    cmdline=" ".join(cmdline_list)[:200],
                    cpu_percent=info["cpu_percent"] or 0.0,
                    memory_mb=mem_mb,
                    gpu_memory_mb=gpu_mem,
                    gpu_index=gpu_idx,
                    uptime_seconds=uptime,
                    status=info["status"] or "",
                ))
            except (ps.NoSuchProcess, ps.AccessDenied, ps.ZombieProcess):
                continue

        agents.sort(key=lambda a: (a.gpu_memory_mb, a.cpu_percent), reverse=True)
        return agents

    def calculate_capacity(self) -> dict:
        """计算还能并行运行多少个 Agent."""
        ps = self.psutil
        agents = self.collect_agents()

        # RAM
        vm = ps.virtual_memory()
        ram_free_mb = vm.available / (1024 * 1024)
        max_by_ram = int(ram_free_mb / 2048)  # 2GB per agent

        # CPU
        cpu_count = ps.cpu_count(logical=True) or 1
        cpu_usage = ps.cpu_percent(interval=0)
        cpu_free = cpu_count * (1 - cpu_usage / 100)
        max_by_cpu = int(cpu_free / 2)  # 2 cores per agent

        # GPU
        gpu_free_mb = 0.0
        max_by_gpu = 999
        gpus = self.collect_gpu()
        if gpus:
            for gpu in gpus:
                free = gpu.memory_total - gpu.memory_used
                gpu_free_mb += free
            max_by_gpu = int(gpu_free_mb / 4096)  # 4GB per agent

        recommended = min(max_by_ram, max_by_cpu, max_by_gpu)

        return {
            "running_agent_count": len(agents),
            "recommended_parallel": max(0, recommended),
            "max_agents_by_gpu": max_by_gpu if gpus else -1,
            "max_agents_by_ram": max_by_ram,
            "max_agents_by_cpu": max_by_cpu,
            "gpu_free_mb": gpu_free_mb,
            "ram_free_mb": ram_free_mb,
            "cpu_free_cores": cpu_free,
        }

    def get_cpu_history(self) -> list[float]:
        return list(self._cpu_history)

    def get_gpu_history(self, index: int) -> list[float]:
        return list(self._gpu_histories.get(index, []))

    def get_mem_history(self) -> list[float]:
        return list(self._mem_history)

    def get_net_send_history(self) -> list[float]:
        return list(self._net_send_history)

    def get_net_recv_history(self) -> list[float]:
        return list(self._net_recv_history)
