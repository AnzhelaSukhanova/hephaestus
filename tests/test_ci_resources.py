"""Sizing the CI fuzzer from the CPUs, memory and disk it may use (``ci.resources``)."""
from pathlib import Path
import sys

import pytest

from ci import resources
from ci.resources import GIB, MIB, Resources


# The real detection, before the fixture below replaces it.
performance_cpus = resources._performance_cpus


@pytest.fixture(autouse=True)
def uniform_cores(monkeypatch):
    """No efficiency cores unless a test says so (this may run on Apple silicon)."""
    monkeypatch.setattr(resources, '_performance_cpus', lambda **_: None)


def cgroup(tmp_path: Path, **files: str) -> Path:
    root = tmp_path / 'cgroup'
    for name, text in files.items():
        path = root / name.replace('__', '/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + '\n')
    root.mkdir(exist_ok=True)
    return root


def meminfo(tmp_path: Path, kib: int) -> Path:
    path = tmp_path / 'meminfo'
    path.write_text(f'MemTotal:       {kib} kB\nMemFree:        1 kB\n')
    return path


@pytest.mark.parametrize('files, affinity, cpus, source', [
    ({'cpu.max': '1600000 100000'}, 32, 16.0, 'cgroup'),        # docker --cpus 16
    ({'cpu.max': '250000 100000'}, 32, 2.5, 'cgroup'),
    ({'cpu.max': 'max 100000'}, 12, 12.0, 'affinity'),          # no --cpus
    ({'cpu.max': '1600000 100000'}, 4, 4.0, 'affinity'),        # --cpuset narrower than quota
    ({'cpu__cpu.cfs_quota_us': '800000', 'cpu__cpu.cfs_period_us': '100000'}, 32, 8.0, 'cgroup'),
    ({'cpu__cpu.cfs_quota_us': '-1', 'cpu__cpu.cfs_period_us': '100000'}, 6, 6.0, 'affinity'),
    ({}, 3, 3.0, 'affinity'),                                   # no cgroup at all
])
def test_cpus_follow_the_cgroup_quota_and_affinity(tmp_path, files, affinity, cpus, source):
    found = resources.detect(tmp_path, cgroup(tmp_path, **files), meminfo(tmp_path, 1 << 20),
                             affinity=affinity)
    assert (found.cpus, found.cpu_source) == (cpus, source)


@pytest.mark.parametrize('performance, recorded', [
    (8, 8),         # M2 Max: 8 P + 4 E cores
    (12, None),     # all usable cores are performance cores
    (None, None),
])
def test_performance_cores_are_recorded_next_to_all_cpus(tmp_path, monkeypatch, performance,
                                                         recorded):
    monkeypatch.setattr(resources, '_performance_cpus', lambda **_: performance)
    found = resources.detect(tmp_path, cgroup(tmp_path), meminfo(tmp_path, 1 << 20),
                             affinity=12)
    assert (found.cpus, found.performance_cpus) == (12.0, recorded)


@pytest.mark.parametrize('listed, affinity, expected', [
    ('0-15', None, 16),                 # Intel hybrid: P-cores with hyperthreads
    ('0-7,16-23', set(range(12)), 8),   # only the usable ones
    ('0-3', {8, 9}, None),
])
def test_linux_hybrid_cpus_list_their_performance_cores(tmp_path, listed, affinity, expected):
    (tmp_path / 'cpu_core').mkdir()
    (tmp_path / 'cpu_core' / 'cpus').write_text(listed + '\n')
    assert performance_cpus(tmp_path, affinity) == expected


def test_uniform_linux_cpus_have_no_performance_split(tmp_path):
    if sys.platform == 'darwin':
        pytest.skip('macOS answers from sysctl')
    assert performance_cpus(tmp_path, None) is None


@pytest.mark.parametrize('files, total_kib, memory, source', [
    ({'memory.max': str(30 * GIB)}, 64 << 20, 30 * GIB, 'cgroup'),   # docker --memory 30g
    ({'memory.max': 'max'}, 64 << 20, 64 * GIB, 'meminfo'),
    ({'memory.max': str(128 * GIB)}, 64 << 20, 64 * GIB, 'meminfo'),  # limit above the host
    ({'memory__memory.limit_in_bytes': str(8 * GIB)}, 64 << 20, 8 * GIB, 'cgroup'),
    ({'memory__memory.limit_in_bytes': str(0x7FFFFFFFFFFFF000)}, 16 << 20, 16 * GIB, 'meminfo'),
])
def test_memory_follows_the_cgroup_limit_and_meminfo(tmp_path, files, total_kib, memory, source):
    found = resources.detect(tmp_path, cgroup(tmp_path, **files), meminfo(tmp_path, total_kib),
                             affinity=4)
    assert (found.memory_bytes, found.memory_source) == (memory, source)


def test_without_linux_files_memory_comes_from_sysconf(tmp_path):
    found = resources.detect(tmp_path, tmp_path / 'none', tmp_path / 'none', affinity=4,
                             physical=12 * GIB)
    assert (found.memory_bytes, found.memory_source) == (12 * GIB, 'sysconf')
    assert found.free_disk_bytes > 0
    real = resources.detect(tmp_path, tmp_path / 'none', tmp_path / 'none', affinity=4)
    assert real.memory_source == 'sysconf' and real.memory_bytes > GIB


def test_without_any_memory_figure_sizes_for_the_known_good_configuration(tmp_path,
                                                                         monkeypatch):
    monkeypatch.setattr(resources, '_physical_memory', lambda: None)
    found = resources.detect(tmp_path, tmp_path / 'none', tmp_path / 'none', affinity=4)
    assert (found.memory_bytes, found.memory_source) == (30 * GIB, 'default')


def host(cpus: float, memory_gib: float, performance: int | None = None) -> Resources:
    return Resources(cpus, performance, int(memory_gib * GIB), 100 * GIB, 'cgroup', 'cgroup')


@pytest.mark.parametrize('cpus, performance, memory_gib, workers, heap_mib', [
    (12, 8, 94, 8, 8 * 1024),     # M2 Max, 96 GB: one worker per performance core
    (12, 8, 30, 8, 2560),         # M2 Max, 32 GB: 28 GiB / 8 = 3.5 each, minus 1
    (12, 8, 16, 7, 2048),         # memory-bound: 14 GiB / 2 GiB each
    (16, None, 30, 14, 2048),     # memory-bound: 28 GiB / 2 GiB each
    (32, None, 62, 30, 2048),
    (12, None, 93, 12, 6741),     # one per CPU: 91 GiB / 12 each, minus 1
    (2, None, 8, 2, 2048),
    (1, None, 3, 1, 2048),        # below one worker's peak: floor heap, warning
])
def test_plan_spends_cpus_and_memory(cpus, performance, memory_gib, workers, heap_mib):
    chosen = resources.plan(host(cpus, memory_gib, performance))
    assert (chosen.workers, chosen.heap_bytes) == (workers, heap_mib * MIB)
    assert chosen.java_opts == f'-Xmx{heap_mib}m'
    assert (chosen.workers_source, chosen.java_opts_source) == ('auto', 'auto')
    assert bool(chosen.warnings) == (memory_gib == 3)


def test_explicit_workers_keep_the_heap_within_their_share():
    chosen = resources.plan(host(16, 30), workers=2)
    assert (chosen.workers, chosen.java_opts, chosen.workers_source) == (2, '-Xmx8192m', 'option')
    crowded = resources.plan(host(16, 30), workers=20)
    assert crowded.java_opts == '-Xmx2048m'
    assert 'may run out of memory' in crowded.warnings[0]


def test_java_opts_from_the_agent_are_kept():
    chosen = resources.plan(host(16, 30), java_opts='-Xmx3g -XX:+UseSerialGC')
    assert chosen.workers == 14
    assert (chosen.java_opts, chosen.heap_bytes) == ('-Xmx3g -XX:+UseSerialGC', None)
    assert chosen.java_opts_source == 'HEPHAESTUS_JAVA_OPTS'


def test_describe_and_record():
    found = host(16, 30)
    chosen = resources.plan(found)
    assert resources.describe(found, chosen) == (
        '16 CPUs (cgroup), 30.0 GiB memory (cgroup), 100.0 GiB free disk: '
        '14 worker(s) (auto), compiler JAVA_OPTS -Xmx2048m (auto)')
    mac = host(12, 94, performance=8)
    assert resources.describe(mac, resources.plan(mac)).startswith(
        '12 CPUs (cgroup, 8 performance cores), 94.0 GiB memory')
    record = resources.record(found, chosen, 2 * GIB)
    assert record['plan']['workers'] == 14
    assert record['detected']['cpus'] == 16
    assert record['constants']['worker_memory_bytes'] == resources.WORKER_MEMORY
    assert record['measured'] is None


@pytest.mark.parametrize('value, size', [('2G', 2 * GIB), ('512m', 512 * MIB), ('64K', 65536)])
def test_parse_size(value, size):
    assert resources.parse_size(value) == size


@pytest.mark.parametrize('value', ['2', '2T', 'G', '-1G', '1.5G'])
def test_bad_size(value):
    with pytest.raises(ValueError, match='invalid size'):
        resources.parse_size(value)


def test_measured_peaks_read_the_cgroup(tmp_path):
    peaks = resources.measured_peaks(cgroup(tmp_path, **{'memory.peak': str(5 * GIB)}))
    assert peaks['container_peak_bytes'] == 5 * GIB
    assert peaks['largest_process_rss_bytes'] >= 0
    assert resources.measured_peaks(tmp_path / 'none')['container_peak_bytes'] is None
