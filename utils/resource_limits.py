import math
import multiprocessing
import os

GIB = 1024**3
MAX_PREINSTALL_WORKERS = 8
MAX_PNPM_CHILD_CONCURRENCY = 8
MAX_PNPM_NETWORK_CONCURRENCY = 16


def _read_positive_int(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = int(handle.read().strip())
        return value if value > 0 else None
    except (OSError, ValueError):
        return None


def _cgroup_cpu_limit():
    try:
        with open("/sys/fs/cgroup/cpu.max", encoding="utf-8") as handle:
            quota_text, period_text = handle.read().split()[:2]
        if quota_text != "max":
            quota = int(quota_text)
            period = int(period_text)
            if quota > 0 and period > 0:
                return max(1, math.ceil(quota / period))
    except (OSError, ValueError):
        pass

    quota = _read_positive_int("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_positive_int("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota and period:
        return max(1, math.ceil(quota / period))
    return None


def available_cpu_count():
    """Return CPUs usable by this process, including affinity/cgroup limits."""
    candidates = []
    try:
        candidates.append(max(1, multiprocessing.cpu_count()))
    except NotImplementedError:
        pass
    if hasattr(os, "sched_getaffinity"):
        try:
            candidates.append(max(1, len(os.sched_getaffinity(0))))
        except OSError:
            pass
    cgroup_limit = _cgroup_cpu_limit()
    if cgroup_limit:
        candidates.append(cgroup_limit)
    return max(1, min(candidates)) if candidates else 1


def _host_available_memory_bytes():
    try:
        with open("/proc/meminfo", encoding="utf-8") as meminfo:
            for line in meminfo:
                key, _, value = line.partition(":")
                if key == "MemAvailable":
                    return int(value.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _cgroup_available_memory_bytes():
    maximum = _read_positive_int("/sys/fs/cgroup/memory.max")
    current = _read_positive_int("/sys/fs/cgroup/memory.current")
    if maximum and current is not None:
        return max(0, maximum - current)

    maximum = _read_positive_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    current = _read_positive_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    # Linux commonly reports an effectively unlimited v1 value near 2^63.
    if maximum and maximum < 1 << 60 and current is not None:
        return max(0, maximum - current)
    return None


def available_memory_bytes():
    """Return conservative available memory for this process, when discoverable."""
    candidates = [
        value
        for value in (
            _host_available_memory_bytes(),
            _cgroup_available_memory_bytes(),
        )
        if value is not None
    ]
    return min(candidates) if candidates else None


def preinstall_worker_count(
    task_count, *, cpu_count=None, memory_bytes=None, maximum=MAX_PREINSTALL_WORKERS
):
    """Size parallel heavyweight service installs without oversubscribing hosts."""
    if task_count <= 0:
        return 0
    cpus = max(1, int(cpu_count or available_cpu_count()))
    memory = available_memory_bytes() if memory_bytes is None else memory_bytes
    cpu_slots = max(1, cpus // 2)
    memory_slots = max(1, int(memory) // (2 * GIB)) if memory is not None else maximum
    return max(1, min(int(task_count), int(maximum), cpu_slots, memory_slots))


def pnpm_concurrency(*, cpu_count=None, memory_bytes=None):
    """Return conservative pnpm lifecycle-child and download concurrency."""
    cpus = max(1, int(cpu_count or available_cpu_count()))
    memory = available_memory_bytes() if memory_bytes is None else memory_bytes

    child_workers = max(1, math.ceil(cpus / 4))
    if memory is not None:
        child_workers = min(child_workers, max(1, int(memory) // GIB))
    child_workers = min(child_workers, MAX_PNPM_CHILD_CONCURRENCY)

    network_workers = min(
        MAX_PNPM_NETWORK_CONCURRENCY,
        max(2, math.ceil(cpus / 2)),
    )
    return child_workers, network_workers
