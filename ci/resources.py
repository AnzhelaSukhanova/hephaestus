"""What the fuzzer may use (CPUs, memory, disk) and how ``ci.fuzz`` spends it.

Inside a container ``os.cpu_count()`` and ``/proc/meminfo`` describe the host,
so the limits are read from the cgroup (``cpu.max`` and ``memory.max`` in v2,
the ``cpu``/``memory`` controllers in v1) and combined with the CPU affinity.

Each worker generates a program and then compiles it with one JVM at a time.
There is one worker per performance core; where all cores are alike (servers,
VMs), one per CPU. The constants come from local runs on an M2 Max with one
worker per performance core (8 workers, all four backends): 0.4 GB per worker
steady, peaking at 1.5-2 GB (generator 0.8 GB, compiler about 1 GB); 3-6 GB for
all 8 workers. The 1.2-1.3 cores each worker measured on top of its own were
the efficiency cores absorbing GC, JIT and the main process. Recalibrate from
the measured peaks that ``ci.fuzz`` records.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
from typing import Any


GIB = 1024 ** 3
MIB = 1024 ** 2
CGROUP = Path('/sys/fs/cgroup')
MEMINFO = Path('/proc/meminfo')
DEVICES = Path('/sys/devices')

# The main process (scheduler, reporting) and page cache, outside every worker's share.
RESERVED_MEMORY = 2 * GIB
# A worker's measured peak (generator 0.8 GB + compiler 1 GB); peaks rarely coincide.
WORKER_MEMORY = 2 * GIB
# The worker's Python process, subtracted from its share before sizing the heap.
WORKER_OVERHEAD = 1 * GIB
# -Xmx is a ceiling the JVM only grows into: about twice the measured compiler peak at
# least. Too small a heap makes the compiler throw OutOfMemoryError, which Hephaestus
# records as a crash.
MIN_HEAP = 2 * GIB
MAX_HEAP = 8 * GIB
# The run stops at this much free disk; it doesn't start with less than twice as much.
MIN_FREE_DISK = 2 * GIB

SIZE = re.compile(r'(\d+)([KMG])', re.I)
UNIT_BYTES = {'K': 1024, 'M': MIB, 'G': GIB}


def parse_size(value: str) -> int:
    """``\\d+[KMG]`` (binary units) to bytes."""
    match = SIZE.fullmatch(value)
    if match is None:
        raise ValueError(f'invalid size {value!r}: use a number with K, M or G, e.g. 2G')
    return int(match.group(1)) * UNIT_BYTES[match.group(2).upper()]


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _cgroup_cpus(root: Path) -> float | None:
    v2 = _read(root / 'cpu.max')
    if v2 is not None:
        quota, _, period = v2.partition(' ')
        return None if quota == 'max' else int(quota) / int(period or 100000)
    quota, period = _read(root / 'cpu/cpu.cfs_quota_us'), _read(root / 'cpu/cpu.cfs_period_us')
    if quota is None or period is None or int(quota) <= 0:
        return None
    return int(quota) / int(period)


def _cgroup_memory(root: Path) -> int | None:
    v2 = _read(root / 'memory.max')
    if v2 is not None:
        return None if v2 == 'max' else int(v2)
    v1 = _read(root / 'memory/memory.limit_in_bytes')
    # v1 reports "unlimited" as a huge page-aligned number.
    return int(v1) if v1 is not None and int(v1) < 1 << 60 else None


def _total_memory(meminfo: Path) -> int | None:
    for line in (_read(meminfo) or '').splitlines():
        if line.startswith('MemTotal:'):
            return int(line.split()[1]) * 1024
    return None


def _physical_memory() -> int | None:
    """Total RAM where there is no /proc/meminfo (macOS); on Linux the same as MemTotal."""
    try:
        return os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    except (AttributeError, ValueError, OSError):
        return None


def _affinity() -> set[int] | None:
    try:
        return os.sched_getaffinity(0)
    except AttributeError:  # macOS
        return None


def _cpu_list(text: str) -> set[int]:
    """A sysfs CPU list such as ``0-7,16-23``."""
    cpus: set[int] = set()
    for part in filter(None, text.split(',')):
        first, _, last = part.partition('-')
        cpus.update(range(int(first), int(last or first) + 1))
    return cpus


def _sysctl(name: str) -> int | None:
    try:
        done = subprocess.run(['sysctl', '-n', name], capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    value = done.stdout.strip()
    return int(value) if done.returncode == 0 and value.isdigit() else None


def _performance_cpus(devices: Path = DEVICES, affinity: set[int] | None = None) -> int | None:
    """How many of the usable CPUs are performance cores; None if all cores are alike."""
    # Linux on Intel hybrid CPUs lists P-cores (with their hyperthreads) under cpu_core
    # and E-cores under cpu_atom.
    listed = _read(devices / 'cpu_core/cpus')
    if listed is not None:
        cores = _cpu_list(listed)
        return len(cores & affinity if affinity is not None else cores) or None
    if sys.platform == 'darwin' and (_sysctl('hw.nperflevels') or 1) > 1:
        return _sysctl('hw.perflevel0.logicalcpu')
    return None


@dataclass(frozen=True)
class Resources:
    cpus: float
    performance_cpus: int | None  # None: all cores alike
    memory_bytes: int
    free_disk_bytes: int
    cpu_source: str
    memory_source: str


def detect(disk_path: Path, cgroup: Path = CGROUP, meminfo: Path = MEMINFO,
           affinity: int | None = None, physical: int | None = None) -> Resources:
    """The CPUs and memory this process may use, and the free disk at ``disk_path``."""
    allowed = _affinity() if affinity is None else None
    usable, cpu_source = (affinity if affinity is not None
                          else len(allowed) if allowed is not None
                          else os.cpu_count() or 1), 'affinity'
    performance = _performance_cpus(affinity=allowed)
    if performance is not None and performance >= usable:
        performance = None
    quota = _cgroup_cpus(cgroup)
    cpus = float(usable)
    if quota is not None and quota < usable:
        cpus, cpu_source = quota, 'cgroup'

    limit = _cgroup_memory(cgroup)
    total, total_source = _total_memory(meminfo), 'meminfo'
    if total is None:
        total, total_source = (physical if physical is not None else _physical_memory(),
                               'sysconf')
    if limit is not None and (total is None or limit < total):
        memory, memory_source = limit, 'cgroup'
    elif total is not None:
        memory, memory_source = total, total_source
    else:  # nothing to go by: size for the configuration known to work
        memory, memory_source = 30 * GIB, 'default'
    return Resources(cpus, performance, memory, shutil.disk_usage(disk_path).free,
                     cpu_source, memory_source)


@dataclass(frozen=True)
class Plan:
    workers: int
    heap_bytes: int | None
    java_opts: str
    java_opts_source: str
    workers_source: str
    warnings: tuple[str, ...]


def plan(found: Resources, workers: int | None = None,
         java_opts: str | None = None) -> Plan:
    """Workers from CPUs and memory, then the heap from each worker's share.

    ``workers`` and ``java_opts`` (``HEPHAESTUS_JAVA_OPTS``) override the choice.
    Memory is never split below ``WORKER_MEMORY``: fewer workers, not smaller heaps.
    """
    warnings: list[str] = []
    usable = max(found.memory_bytes - RESERVED_MEMORY, 0)
    if workers is None:
        by_cpu = int(found.cpus)
        if found.performance_cpus is not None:
            by_cpu = min(by_cpu, found.performance_cpus)
        by_memory = usable // WORKER_MEMORY
        chosen, workers_source = max(1, min(by_cpu, by_memory)), 'auto'
    else:
        chosen, workers_source = workers, 'option'
    share = usable // chosen
    if java_opts is not None:
        return Plan(chosen, None, java_opts, 'HEPHAESTUS_JAVA_OPTS', workers_source, ())
    heap = max(MIN_HEAP, min(MAX_HEAP, share - WORKER_OVERHEAD)) // MIB * MIB
    if share < WORKER_MEMORY:
        warnings.append(
            f'{chosen} worker(s) in {found.memory_bytes / GIB:.1f} GiB leave less than '
            f'{WORKER_MEMORY / GIB:.0f} GiB each: the compiler may run out of memory, and '
            'Hephaestus records that as a crash')
    return Plan(chosen, heap, f'-Xmx{heap // MIB}m', 'auto', workers_source, tuple(warnings))


def describe(found: Resources, chosen: Plan) -> str:
    performance = (f', {found.performance_cpus} performance cores'
                   if found.performance_cpus is not None else '')
    return (f'{found.cpus:g} CPUs ({found.cpu_source}{performance}), '
            f'{found.memory_bytes / GIB:.1f} GiB memory ({found.memory_source}), '
            f'{found.free_disk_bytes / GIB:.1f} GiB free disk: '
            f'{chosen.workers} worker(s) ({chosen.workers_source}), '
            f'compiler JAVA_OPTS {chosen.java_opts} ({chosen.java_opts_source})')


def measured_peaks(cgroup: Path = CGROUP) -> dict[str, int | None]:
    """Peak memory of the whole container and of its largest finished process.

    Only meant for recalibrating the constants above. ``ru_maxrss`` covers every
    descendant that has been waited for, so call this after the workers have exited.
    """
    peak = _read(cgroup / 'memory.peak') or _read(cgroup / 'memory/memory.max_usage_in_bytes')
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # Linux reports kilobytes, macOS bytes.
    return {'container_peak_bytes': int(peak) if peak else None,
            'largest_process_rss_bytes': rss if sys.platform == 'darwin' else rss * 1024}


def record(found: Resources, chosen: Plan, min_free_disk: int,
           measured: dict[str, int | None] | None = None) -> dict[str, Any]:
    return {'detected': asdict(found), 'plan': asdict(chosen),
            'min_free_disk_bytes': min_free_disk, 'measured': measured,
            'constants': {'reserved_memory_bytes': RESERVED_MEMORY,
                          'worker_memory_bytes': WORKER_MEMORY,
                          'worker_overhead_bytes': WORKER_OVERHEAD,
                          'min_heap_bytes': MIN_HEAP, 'max_heap_bytes': MAX_HEAP}}
