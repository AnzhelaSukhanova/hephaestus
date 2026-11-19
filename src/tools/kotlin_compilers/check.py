"""Smoke check: call every installed compiler x backend the way Hephaestus does.

Driven only by ``compiler.json``, so it checks the contract a consumer sees.
Three probes per backend, each named in the report when it fails:

``version``
    ``<launcher> -version`` (js/wasm with ``-ir-output-name/-ir-output-dir``,
    like the Hephaestus preflight) exits 0 and prints the descriptor's version.
``compile``
    ``Fixture.kt`` with ``-J-Dorg.jetbrains.kotlin.cliMessageRenderer=FullPath``
    and the inliner flag in the descriptor's style. Klib backends must produce a
    klib whose ``abi_version`` matches; jvm must produce the jar.
``fullpath``
    ``Broken.kt`` (one type error) with the same ``-J`` option must fail with an
    ``error:`` line that starts with the file's absolute path, the format
    Hephaestus parses.

Native runs with a fresh ``-Xkonan-data-dir`` and fails if anything lands in its
``dependencies/`` (an LLVM or sysroot download) or if ``airplaneMode`` had to
stop a fetch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from .descriptor import DescriptorError, klib_manifest, read_descriptor
from .download import log
from .layout import Layout
from .sync import read_sync_state, select
from .versions import BACKENDS, VersionList, selector_version


FIXTURES = Path(__file__).resolve().parent / 'fixtures'
FIXTURE = FIXTURES / 'Fixture.kt'
BROKEN = FIXTURES / 'Broken.kt'
FULL_PATH_RENDERER = '-J-Dorg.jetbrains.kotlin.cliMessageRenderer=FullPath'
PROBES = ('version', 'compile', 'fullpath')
VERSION_LINE = re.compile(r'^(?:info:\s*)?kotlinc(?:-[a-z]+)?\s+(\S+)', re.MULTILINE)
INLINER_MODE = 'full'  # Hephaestus's default inlining mode
TIMEOUT_SECONDS = 900
AIRPLANE_ERRORS = ('Cannot find a dependency locally', 'airplaneMode', 'airplane mode')
STDLIB_WARNING = re.compile(r'^warning:.*(?:standard library|stdlib).*$', re.IGNORECASE | re.MULTILINE)


@dataclass
class Target:
    directory: Path
    backends: set[str]
    ids: list[str] = field(default_factory=list)


@dataclass
class Result:
    ids: str
    version: str
    backend: str
    failed: list[str]
    seconds: float
    note: str

    @property
    def ok(self) -> bool:
        return not self.failed


def targets(layout: Layout, version_list: VersionList, ids: Sequence[str]) -> tuple[
        list[Target], list[str]]:
    """Real directories to check with their backends; problems for explicit ids."""
    resolved = read_sync_state(layout).get('resolved') or {}
    found: dict[Path, Target] = {}
    problems = []
    for selector in select(version_list, ids):
        version = selector_version(selector, resolved)
        if version is None:
            if ids:
                problems.append(f'{selector.text}: not resolved (run sync)')
            continue
        directory = layout.compiler_dir(version)
        try:
            descriptor = read_descriptor(directory)
        except DescriptorError as exc:
            problems.append(f'{selector.text}: {exc}')
            continue
        if descriptor is None:
            if ids:
                problems.append(f'{selector.text}: {version} is not installed')
            continue
        wanted = selector.backends & set(descriptor['backends'])
        missing = selector.backends - set(descriptor['backends'])
        if ids and missing and len(selector.backends) == 1:
            problems.append(f'{selector.text}: backend not installed in {version}')
        target = found.setdefault(directory.resolve(), Target(directory.resolve(), set()))
        target.backends |= wanted
        target.ids.append(selector.text)
    return [t for t in found.values() if t.backends], problems


def inliner_args(descriptor: dict, backend: str) -> list[str]:
    if backend == 'jvm':
        return []
    if descriptor['inliner_flag'] == 'boolean':
        return ['-Xklib-ir-inliner']  # present = on; 2.2.x rejects a value
    return [f'-Xklib-ir-inliner={INLINER_MODE}']


def version_command(directory: Path, backend: str, entry: dict, scratch: Path) -> list[str]:
    command = [str(directory / entry['launcher']), *entry['args'], '-version']
    if backend in ('js', 'wasm'):
        command += ['-ir-output-name', 'probe', '-ir-output-dir', str(scratch / 'probe')]
    return command


def command(directory: Path, descriptor: dict, backend: str, source: Path, out: Path,
            konan_data: Path) -> tuple[list[str], Path]:
    """A compile of ``source`` shaped like Hephaestus's, and the artifact it must produce."""
    entry = descriptor['backends'][backend]
    base = [str(directory / entry['launcher']), *entry['args'], str(source), FULL_PATH_RENDERER]
    inliner = inliner_args(descriptor, backend)
    if backend == 'jvm':
        return [*base, '-d', str(out / 'fixture.jar')], out / 'fixture.jar'
    if backend in ('js', 'wasm'):
        return ([*base, '-Xir-produce-klib-file', '-ir-output-dir', str(out),
                 '-ir-output-name', 'fixture', '-libraries', str(directory / entry['stdlib']),
                 *inliner], out / 'fixture.klib')
    return ([*base, '-produce', 'library', '-o', str(out / 'fixture'), *inliner,
             f'-Xkonan-data-dir={konan_data}'], out / 'fixture.klib')


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                                   cwd=cwd, stdin=subprocess.DEVNULL)
        return completed.returncode, completed.stdout + completed.stderr
    except subprocess.TimeoutExpired:
        return -1, f'timed out after {TIMEOUT_SECONDS}s'
    except OSError as exc:
        return -1, str(exc)


def _tail(output: str) -> str:
    return ' | '.join([line for line in output.strip().splitlines() if line.strip()][-3:])


def full_path_errors(output: str, source: Path) -> list[str]:
    """``error:`` lines that start with the absolute path of ``source``."""
    paths = {str(source), os.path.realpath(source)}
    pattern = re.compile(r'^(?:e:\s*)?(' + '|'.join(re.escape(p) for p in paths)
                         + r'):\d+:\d+:\s*error:', re.MULTILINE)
    return pattern.findall(output)


def _dependencies_content(konan_data: Path) -> list[str]:
    dependencies = konan_data / 'dependencies'
    if not dependencies.is_dir():
        return []
    return sorted(p.name for p in dependencies.iterdir())


def _native_guard(konan_data: Path, output: str) -> list[str]:
    problems = []
    fetched = _dependencies_content(konan_data)
    if fetched:
        problems.append(f'Native downloaded into dependencies/: {", ".join(fetched)}')
    if any(marker in output for marker in AIRPLANE_ERRORS):
        problems.append('Native tried to fetch LLVM (blocked by airplaneMode)')
    return problems


def probe_version(directory: Path, descriptor: dict, backend: str,
                  scratch: Path) -> tuple[bool, str]:
    entry = descriptor['backends'][backend]
    code, output = _run(version_command(directory, backend, entry, scratch), scratch)
    found = VERSION_LINE.search(output)
    printed = found.group(1) if found else None
    if code != 0:
        return False, f'exit {code}: {_tail(output)}'
    if printed != descriptor['version']:
        return False, f'printed {printed or "no version"} != compiler.json {descriptor["version"]}'
    return True, f'-version {printed}'


def probe_compile(directory: Path, descriptor: dict, backend: str,
                  scratch: Path) -> tuple[bool, str, str]:
    out, konan_data = scratch / 'compile', scratch / 'compile-konan'
    out.mkdir()
    konan_data.mkdir()
    cmd, artifact = command(directory, descriptor, backend, FIXTURE, out, konan_data)
    code, output = _run(cmd, scratch)
    notes = _native_guard(konan_data, output) if backend == 'native' else []
    ok = code == 0 and artifact.exists() and not notes
    if code != 0:
        notes.append(f'exit {code}: {_tail(output)}')
    elif not artifact.exists():
        notes.append(f'no {artifact.name} produced')
    elif artifact.suffix == '.klib':
        abi = (klib_manifest(artifact) or {}).get('abi_version')
        if abi != descriptor['abi_version']:
            ok = False
            notes.append(f'abi_version {abi} != compiler.json {descriptor["abi_version"]}')
        else:
            notes.append(f'abi {abi}')
    else:
        notes.append(artifact.name)
    return ok, '; '.join(notes), output


def probe_full_path(directory: Path, descriptor: dict, backend: str,
                    scratch: Path) -> tuple[bool, str]:
    out, konan_data = scratch / 'broken', scratch / 'broken-konan'
    out.mkdir()
    konan_data.mkdir()
    cmd, _ = command(directory, descriptor, backend, BROKEN, out, konan_data)
    code, output = _run(cmd, scratch)
    guard = _native_guard(konan_data, output) if backend == 'native' else []
    if guard:
        return False, '; '.join(guard)
    if code == 0:
        return False, f'{BROKEN.name} compiled without an error'
    if not full_path_errors(output, BROKEN):
        return False, f'no error line starting with {BROKEN}: {_tail(output)}'
    return True, 'full-path error'


def check_backend(directory: Path, descriptor: dict, backend: str, scratch: Path) -> Result:
    started = time.monotonic()
    failed, notes = [], []
    version_ok, version_note = probe_version(directory, descriptor, backend, scratch)
    compile_ok, compile_note, output = probe_compile(directory, descriptor, backend, scratch)
    full_path_ok, full_path_note = probe_full_path(directory, descriptor, backend, scratch)
    for probe, ok, note in (('version', version_ok, version_note),
                            ('compile', compile_ok, compile_note),
                            ('fullpath', full_path_ok, full_path_note)):
        if not ok:
            failed.append(probe)
        notes.append(note if ok else f'{probe}: {note}')
    warnings = sorted(set(STDLIB_WARNING.findall(output)))
    notes += [f'stdlib warning: {warning[:160]}' for warning in warnings]
    return Result('', descriptor['version'], backend, failed, time.monotonic() - started,
                  '; '.join(notes))


def check(layout: Layout, version_list: VersionList, ids: Sequence[str] = ()) -> tuple[
        int, list[str]]:
    found, problems = targets(layout, version_list, ids)
    results = []
    for target in found:
        descriptor = read_descriptor(target.directory)
        for backend in (b for b in BACKENDS if b in target.backends):
            log(f'checking {descriptor["version"]} {backend} ...')
            with tempfile.TemporaryDirectory(prefix='kotlin-compilers-check.') as scratch:
                result = check_backend(target.directory, descriptor, backend, Path(scratch))
            result.ids = ','.join(dict.fromkeys(target.ids))
            results.append(result)
    rows = [('ids', 'version', 'backend', 'result', 'seconds', 'details')]
    rows += [(r.ids, r.version, r.backend, 'ok' if r.ok else 'FAIL ' + ','.join(r.failed),
              f'{r.seconds:.1f}', r.note) for r in results]
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    lines = ['  '.join([*(cell.ljust(width) for cell, width in zip(row[:5], widths)), row[5]])
             .rstrip() for row in rows]
    if not results:
        lines.append('(nothing installed to check)')
    lines += [f'problem: {problem}' for problem in problems]
    failed = problems or any(not r.ok for r in results) or (ids and not results)
    return (1 if failed else 0), lines
