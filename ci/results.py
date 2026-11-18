"""Pack a run's results and provenance: ``python -m ci.results --bugs <dir> --out <dir>``.

Writes ``hephaestus-results.zip``, ``summary.json`` and ``provenance.json`` to
``--out``. The archive holds every run under ``--bugs`` (a folder with
``stats.json``):

- the run's result files, byte for byte;
- the folders of every fault and inconclusive entry and of every program in
  their ``comparison_dependencies.closure``, without compiled outputs;
- ``index/findings.jsonl`` (one line per entry, with a signature for grouping),
  ``manifest.json``, ``summary.json``, ``provenance.json`` and a README.

It reads a live run safely: Hephaestus renames its result files into place, a
finding's folder exists before the finding is recorded, and a provider's folder
exists before the ledger names it. TeamCity runs it in a second container
during fuzzing (``--checkpoint``) and once after the fuzz container stopped.
"""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from typing import Any, TextIO
import zipfile

from ci.fuzz import EXIT_CODES, STOP_REASONS, service_message
from src.tools.kotlin_compilers.layout import Layout
from src.tools.kotlin_compilers.sync import read_sync_state


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = 'hephaestus-results.zip'
PREFIX = 'hephaestus-results'
SCHEMA = 1

REPORTS = {'faults.json': 'fault', 'inconclusive.json': 'inconclusive'}
RUN_FILES = ('faults.json', 'inconclusive.json', 'escalations.json', 'stats.json',
             'time_metrics.json', 'crossmodule.json', 'resources.json')
TEXT_FILES = ('divergences.md', 'console.log')
# Compiled outputs and pickles: rebuilt from the sources with the recorded compilers.
EXCLUDED_SUFFIXES = ('.klib', '.jar', '.class', '.js', '.mjs', '.wasm', '.kexe', '.so',
                     '.dylib', '.exe', '.bin')
LOG_LIMIT = 100 * 1024 * 1024
WARN_BYTES = 280 * 1024 * 1024
ERROR_HEAD_LINES = 6
TOP_SIGNATURES = 50

FRAME = re.compile(r'^\s*at ([\w.$<>]+)\(', re.M)
EXCEPTION = re.compile(r'\s*(?:exception:\s*)?([\w$]+(?:\.[\w$]+)+)')
CAUSED_BY = re.compile(r'\n\s*Caused by: ')
REASONS_BY_CODE = {code: STOP_REASONS[reason] for reason, code in EXIT_CODES.items()}


# ----------------------------------------------------------------------------- reading


@dataclass(frozen=True)
class Snapshot:
    data: bytes
    value: Any
    error: str | None = None


def read_snapshot(path: Path, attempts: int = 20, delay: float = 0.05) -> Snapshot | None:
    """The bytes of a JSON file and their parsed value, or None if it doesn't exist.

    Like ``src.tools.outcomes.read_report`` it retries on a decode error, but it
    keeps the exact bytes it parsed, so the archive holds what was validated.
    A file that stays invalid (e.g. cut off when an older run was killed) keeps
    its bytes, with ``value`` None and the decode error.
    """
    data = b''
    error = ''
    for attempt in range(attempts):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return Snapshot(data, json.loads(data))
        except ValueError as exc:
            error = str(exc)
            if attempt + 1 < attempts:
                time.sleep(delay)
    return Snapshot(data, None, error)


def read_text_head(path: Path, limit: int = LOG_LIMIT) -> bytes | None:
    """At most the last ``limit`` bytes of a file that may still be growing."""
    try:
        with open(path, 'rb') as source:
            size = os.fstat(source.fileno()).st_size
            if size > limit:
                source.seek(size - limit)
            return source.read(min(size, limit))
    except FileNotFoundError:
        return None


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        key, sep, value = line.partition('=')
        if sep and key.strip() and not key.lstrip().startswith('#'):
            values[key.strip()] = value.strip()
    return values


def read_inspect(path: Path) -> dict[str, Any] | None:
    """The first object of a ``docker inspect`` output file."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


# ----------------------------------------------------------------------------- signatures


def _closure(entry: Mapping[str, Any]) -> list[str]:
    dependencies = entry.get('comparison_dependencies') or {}
    return [str(pid) for pid in dependencies.get('closure') or ()]


def _finding_kinds(entry: Mapping[str, Any]) -> list[str]:
    findings = (entry.get('comparison') or {}).get('findings') or ()
    return sorted({str(finding.get('kind')) for finding in findings})


def _crash_text(entry: Mapping[str, Any]) -> str:
    error = entry.get('error')
    if isinstance(error, str) and error.strip():
        return error
    findings = (entry.get('comparison') or {}).get('findings') or ()
    for finding in findings:
        diagnostics = finding.get('diagnostics') or ()
        if finding.get('kind') == 'crash' and diagnostics:
            return '\n'.join(str(line) for line in diagnostics)
    return ''


def _normalised_line(line: str) -> str:
    line = re.sub(r"'[^']*'|\"[^\"]*\"", '<q>', line)
    line = re.sub(r'(/[^\s:]+)+', '<path>', line)
    line = re.sub(r'\d+', '<n>', line)
    return line.strip()[:160]


def crash_signature(text: str) -> str:
    """Root-cause exception and its top three frames, like ClusterFuzz's crash state.

    Without frames it is the exception class alone, or failing that the
    normalised first line.
    """
    root = CAUSED_BY.split(text)[-1]
    match = EXCEPTION.match(root)
    exception = match.group(1) if match else None
    frames = FRAME.findall(root)[:3]
    if exception and frames:
        return ' | '.join((exception, *frames))
    if exception:
        return exception
    first = next((line for line in root.splitlines() if line.strip()), '')
    return _normalised_line(first) or '<no error text>'


def signature(kind: str, entry: Mapping[str, Any]) -> str:
    oracle = entry.get('oracle') or '?'
    if kind == 'inconclusive':
        return f'inconclusive: {entry.get("inconclusive_reason") or "unknown"}'
    kinds = _finding_kinds(entry)
    if kinds and 'crash' not in kinds:
        return f'{oracle}: {"+".join(kinds)}'
    return crash_signature(_crash_text(entry))


def error_head(entry: Mapping[str, Any]) -> list[str]:
    lines = [line for line in _crash_text(entry).splitlines() if line.strip()]
    return lines[:ERROR_HEAD_LINES]


# ----------------------------------------------------------------------------- collection


@dataclass
class Archive:
    """Files to pack: archive path -> bytes or a source file on disk."""
    blobs: dict[str, bytes] = field(default_factory=dict)
    files: dict[str, Path] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)
    invalid_json: list[dict[str, str]] = field(default_factory=list)

    def add_bytes(self, name: str, data: bytes) -> None:
        self.blobs[f'{PREFIX}/{name}'] = data

    def add_file(self, name: str, path: Path) -> None:
        self.files[f'{PREFIX}/{name}'] = path


@dataclass
class Run:
    name: str
    directory: Path
    reports: dict[str, dict[str, Any]]
    info: dict[str, Any]
    totals: dict[str, Any]
    resources: dict[str, Any] | None = None


def find_runs(bugs: Path) -> list[Path]:
    if not bugs.is_dir():
        return []
    return sorted(path for path in bugs.iterdir()
                  if path.is_dir() and (path / 'stats.json').is_file())


def collect_run(directory: Path, archive: Archive) -> Run:
    name = directory.name
    reports: dict[str, dict[str, Any]] = {}
    stats: dict[str, Any] = {}
    sizing: dict[str, Any] | None = None
    for file_name in RUN_FILES:
        snapshot = read_snapshot(directory / file_name)
        if snapshot is None:
            continue
        archive.add_bytes(f'{name}/{file_name}', snapshot.data)
        if snapshot.error is not None:
            archive.invalid_json.append({'path': f'{name}/{file_name}', 'error': snapshot.error})
        value = snapshot.value
        if file_name in REPORTS and isinstance(value, dict):
            reports[file_name] = value
        elif file_name == 'stats.json' and isinstance(value, dict):
            stats = value
        elif file_name == 'resources.json' and isinstance(value, dict):
            sizing = value
    for file_name in TEXT_FILES:
        data = read_text_head(directory / file_name)
        if data is not None:
            archive.add_bytes(f'{name}/{file_name}', data)
    return Run(name, directory, reports, stats.get('Info') or {}, stats.get('totals') or {},
               sizing)


def program_files(directory: Path) -> Iterator[Path]:
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for file_name in sorted(files):
            if not file_name.endswith(EXCLUDED_SUFFIXES):
                yield Path(root) / file_name


def select_programs(run: Run, archive: Archive) -> tuple[list[dict[str, Any]],
                                                         list[dict[str, Any]]]:
    """Copy finding and dependency folders; return index lines and missing ids."""
    index: list[dict[str, Any]] = []
    wanted: dict[str, list[str]] = {}
    expected: set[str] = set()
    for file_name, kind in REPORTS.items():
        for pid, entry in run.reports.get(file_name, {}).items():
            closure = _closure(entry)
            wanted.setdefault(pid, []).append(f'{kind}:{pid}')
            if kind == 'fault' or entry.get('program') or entry.get('programs'):
                expected.add(pid)
            for dependency in closure:
                wanted.setdefault(dependency, []).append(f'closure of {pid}')
                expected.add(dependency)
            index.append({
                'kind': kind, 'run': run.name, 'id': pid,
                'signature': signature(kind, entry),
                'oracle': entry.get('oracle'),
                'finding_kinds': _finding_kinds(entry),
                'inconclusive_reason': entry.get('inconclusive_reason'),
                'error_head': error_head(entry),
                'closure': closure,
                'program_dirs': [f'{run.name}/{pid}' for pid in (pid, *closure)],
            })
    missing: list[dict[str, Any]] = []
    for pid in sorted(wanted, key=_pid_order):
        directory = run.directory / pid
        if not directory.is_dir():
            if pid in expected:
                missing.append({'run': run.name, 'id': pid, 'referenced_by': wanted[pid]})
            continue
        for path in program_files(directory):
            archive.add_file(f'{run.name}/{path.relative_to(run.directory).as_posix()}', path)
    return index, missing


def _pid_order(pid: str) -> tuple[int, int | str]:
    return (0, int(pid)) if pid.isdigit() else (1, pid)


# ----------------------------------------------------------------------------- provenance


def master_version(layout: Layout) -> str | None:
    master = layout.compiler_dir('master')
    return master.resolve().name if master.exists() else None


def java_version() -> str | None:
    try:
        done = subprocess.run(['java', '-version'], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stderr or done.stdout).strip() or None


def stop_reason(container: Mapping[str, Any] | None, checkpoint: bool) -> str:
    if container is None:
        return 'still running (checkpoint)' if checkpoint else 'unknown: no container state'
    state = container.get('State') or {}
    if state.get('Running'):
        return 'still running'
    code = state.get('ExitCode')
    if state.get('OOMKilled'):
        return f'killed: out of memory (exit {code})'
    if code in REASONS_BY_CODE:
        return REASONS_BY_CODE[code]
    if code == 2:
        return 'rejected its arguments (exit 2)'
    return f'failed (exit {code})'


def provenance(runs: Sequence[Run], agent: Path | None, checkpoint: bool,
               created: str, sizing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    layout = Layout()
    version = master_version(layout)
    image = read_inspect(agent / 'image.json') if agent else None
    container = read_inspect(agent / 'container.json') if agent else None
    labels = ((image or {}).get('Config') or {}).get('Labels') or {}
    state = (container or {}).get('State') or {}
    host_config = (container or {}).get('HostConfig') or {}
    try:
        versions_json = json.loads((ROOT / 'versions.json').read_text())
    except (FileNotFoundError, ValueError):
        versions_json = None
    return {
        'schema': SCHEMA,
        'created_at': created,
        'checkpoint': checkpoint,
        'kotlin': {
            'master_version': version,
            'requested_master_version': labels.get('org.jetbrains.kotlin.master-version'),
            'commit': labels.get('org.jetbrains.kotlin.commit'),
            'compiler_descriptor': layout.descriptor(version) if version else None,
            'sync_state': read_sync_state(layout),
            'versions_json': versions_json,
            'compiler_versions_by_run': {run.name: run.info.get('compiler') for run in runs},
        },
        'hephaestus': {
            'commit': labels.get('org.opencontainers.image.revision'),
            'branch': labels.get('org.jetbrains.hephaestus.branch'),
            'command': (container or {}).get('Args'),
            'runs': {run.name: run.info for run in runs},
        },
        'run': {
            'stop_reason': stop_reason(container, checkpoint),
            'exit_code': state.get('ExitCode'),
            'started_at': state.get('StartedAt'),
            'finished_at': state.get('FinishedAt'),
            'oom_killed': state.get('OOMKilled'),
        },
        'runtime': {
            'image_id': (image or {}).get('Id'),
            'image_labels': labels,
            'cpu_limit': (host_config.get('NanoCpus') or 0) / 1e9 or None,
            'memory_limit_bytes': host_config.get('Memory') or None,
            # ci.fuzz: detected CPUs/memory/disk, chosen workers and heap, measured peaks
            'sizing': dict(sizing or {}),
            'python': platform.python_version(),
            'java': java_version(),
            'platform': platform.platform(),
        },
        'teamcity': read_env_file(agent / 'teamcity.env') if agent else {},
        'agent': read_env_file(agent / 'host.env') if agent else {},
    }


# ----------------------------------------------------------------------------- output

README = """# Hephaestus results

One archive per TeamCity build: what was found, the programs needed to look at
it, and exactly what ran.

- `provenance.json`: Kotlin master version and its compiler descriptor,
  Hephaestus commit and branch, the `ci.fuzz` command, start/end/exit code and
  why the run stopped, image, TeamCity build and agent.
- `summary.json`: totals per run and findings per signature.
- `index/findings.jsonl`: one line per fault or inconclusive entry, with a
  rough `signature` (root-cause exception plus top three frames, or the oracle
  and finding kinds), the first error lines and the folders of its programs.
  First pass: `jq -r .signature index/findings.jsonl | sort | uniq -c | sort -rn`.
- `manifest.json`: size and sha256 of every file, the bugs root the run used,
  and `missing` folders (recorded by Hephaestus but not found).
- `<run>/`: the run's own files, unchanged, in the run's own layout:
  `faults.json`, `inconclusive.json`, `escalations.json`, `stats.json`, ...
- `<run>/<id>/`: every fault and inconclusive entry with a program, and every
  dependency module in its `comparison_dependencies.closure`.

Compiled outputs (`*.klib`, `*.jar`, ...) and pickles (`*.bin`) are left out.
To reproduce a cross-module finding, first rebuild its dependency modules, in
closure order, with the `reproduce_argv` in each dependency's
`variants/<variant>/result.json` (run from `<run>/<cid>/`, writing
`../klibs/d<cid>-<variant>.klib`), then run the finding's own `reproduce_argv`.
Absolute paths inside the JSON files start with the bugs root in
`manifest.json`; replace it with the folder you unpacked `<run>/` into.
"""


def write_atomically(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def dumps(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=False) + '\n').encode()


def write_zip(path: Path, archive: Archive, generated: Mapping[str, bytes]) -> None:
    temporary = path.with_name(path.name + '.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for name, data in {**archive.blobs, **generated}.items():
            output.writestr(name, data)
        for name, source in archive.files.items():
            output.write(source, name)
    os.replace(temporary, path)


def manifest_files(archive: Archive) -> list[dict[str, Any]]:
    """Size and sha256 of every collected file; unreadable ones are dropped."""
    entries: list[dict[str, Any]] = []
    for name, data in archive.blobs.items():
        entries.append({'path': name.removeprefix(f'{PREFIX}/'), 'size': len(data),
                        'sha256': hashlib.sha256(data).hexdigest()})
    for name, source in list(archive.files.items()):
        try:
            data = source.read_bytes()
        except OSError:
            del archive.files[name]
            archive.unreadable.append(name.removeprefix(f'{PREFIX}/'))
            continue
        entries.append({'path': name.removeprefix(f'{PREFIX}/'), 'size': len(data),
                        'sha256': hashlib.sha256(data).hexdigest()})
    return sorted(entries, key=lambda entry: entry['path'])


def summarise(runs: Sequence[Run], index: Sequence[Mapping[str, Any]],
              missing: Sequence[Mapping[str, Any]], prov: Mapping[str, Any],
              invalid_json: Sequence[Mapping[str, str]] = ()) -> dict[str, Any]:
    counts = Counter((line['kind'], line['signature']) for line in index)
    examples: dict[tuple[str, str], list[str]] = {}
    for line in index:
        examples.setdefault((line['kind'], line['signature']), []).append(
            f'{line["run"]}/{line["id"]}')
    signatures = [{'kind': kind, 'signature': sig, 'count': count,
                   'examples': examples[(kind, sig)][:5]}
                  for (kind, sig), count in counts.most_common()]
    return {
        'schema': SCHEMA,
        'created_at': prov['created_at'],
        'checkpoint': prov['checkpoint'],
        'kotlin_master_version': prov['kotlin']['master_version'],
        'stop_reason': prov['run']['stop_reason'],
        'runs': {run.name: {
            'totals': run.totals,
            'faults': len(run.reports.get('faults.json', {})),
            'inconclusive': len(run.reports.get('inconclusive.json', {})),
        } for run in runs},
        'distinct_signatures': len(counts),
        'signatures': signatures[:TOP_SIGNATURES],
        'missing': len(missing),
        'invalid_json': [entry['path'] for entry in invalid_json],
    }


def collect(bugs: Path, out: Path, agent: Path | None = None, checkpoint: bool = False
            ) -> dict[str, Any]:
    """Write the archive, summary and provenance to ``out``; return the summary."""
    out.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    archive = Archive()
    runs: list[Run] = []
    index: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for directory in find_runs(bugs):
        run = collect_run(directory, archive)
        run_index, run_missing = select_programs(run, archive)
        runs.append(run)
        index.extend(run_index)
        missing.extend(run_missing)
    # A run that hasn't finished keeps its console log and resources next to its folder.
    for log in sorted(bugs.glob('*.console.log')) if bugs.is_dir() else ():
        data = read_text_head(log)
        if data is not None:
            archive.add_bytes(f'logs/{log.name}', data)
    sizing = {run.name: run.resources for run in runs if run.resources is not None}
    for pending in sorted(bugs.glob('*.resources.json')) if bugs.is_dir() else ():
        snapshot = read_snapshot(pending)
        if snapshot is None:
            continue
        archive.add_bytes(f'logs/{pending.name}', snapshot.data)
        if isinstance(snapshot.value, dict):
            sizing.setdefault(pending.name.removesuffix('.resources.json'), snapshot.value)

    prov = provenance(runs, agent, checkpoint, created, sizing)
    summary = summarise(runs, index, missing, prov, archive.invalid_json)
    files = manifest_files(archive)
    manifest = {
        'schema': SCHEMA,
        'bugs_root': str(bugs.resolve()),
        'runs': [run.name for run in runs],
        'run_bugs_roots': {run.name: run.info.get('bugs') for run in runs},
        'excluded_suffixes': list(EXCLUDED_SUFFIXES),
        'missing': missing,
        'unreadable': archive.unreadable,
        'invalid_json': archive.invalid_json,
        'files': files,
    }
    findings = ''.join(json.dumps(line) + '\n' for line in index).encode()
    generated = {
        f'{PREFIX}/README.md': README.encode(),
        f'{PREFIX}/provenance.json': dumps(prov),
        f'{PREFIX}/summary.json': dumps(summary),
        f'{PREFIX}/manifest.json': dumps(manifest),
        f'{PREFIX}/index/findings.jsonl': findings,
    }
    write_zip(out / ARCHIVE, archive, generated)
    write_atomically(out / 'summary.json', dumps(summary))
    write_atomically(out / 'provenance.json', dumps(prov))
    return summary


def report(summary: Mapping[str, Any], archive: Path, warn_bytes: int, teamcity: bool,
           out: TextIO | None = None) -> None:
    out = out or sys.stdout
    size = archive.stat().st_size
    findings = sum(run['faults'] + run['inconclusive'] for run in summary['runs'].values())
    line = (f'{archive.name}: {size / 1e6:.1f} MB, {findings} findings in '
            f'{summary["distinct_signatures"]} signatures, {summary["missing"]} missing; '
            f'{summary["stop_reason"]}')
    print(line, file=out, flush=True)
    warnings: list[str] = []
    for path in summary['invalid_json']:
        warnings.append(f'{path} is not valid JSON; packed as is (see manifest.json)')
    if summary['missing']:
        warnings.append(f'{summary["missing"]} recorded program folders were not found '
                        f'(see manifest.json)')
    if size > warn_bytes:
        warnings.append(f'{archive.name} is {size / 1e6:.0f} MB, over {warn_bytes / 1e6:.0f} MB; '
                        f'check the maximum artifact size')
    for warning in warnings:
        print(f'warning: {warning}', file=out, flush=True)
    if teamcity:
        for key, value in (('hephaestus.signatures', summary['distinct_signatures']),
                           ('hephaestus.missing', summary['missing']),
                           ('hephaestus.results.bytes', size)):
            print(service_message('buildStatisticValue', key=key, value=value),
                  file=out, flush=True)
        for warning in warnings:
            print(service_message('message', text=warning, status='WARNING'),
                  file=out, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m ci.results',
        description='Pack Hephaestus results and provenance into hephaestus-results.zip.')
    parser.add_argument('--bugs', type=Path, default=ROOT / 'bugs',
                        help='Hephaestus bugs directory (read only)')
    parser.add_argument('--out', type=Path, required=True,
                        help='Folder for hephaestus-results.zip, summary.json, provenance.json')
    parser.add_argument('--agent', type=Path,
                        help='Folder with teamcity.env, host.env, image.json, container.json')
    parser.add_argument('--checkpoint', action='store_true',
                        help='The run is still going; mark the results as a checkpoint')
    parser.add_argument('--teamcity', action='store_true',
                        help='Print TeamCity build statistics and warnings')
    parser.add_argument('--warn-bytes', type=int, default=WARN_BYTES,
                        help='Warn when the archive is larger (default: 280 MB)')
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(None if argv is None else list(argv))
    summary = collect(args.bugs, args.out, args.agent, args.checkpoint)
    report(summary, args.out / ARCHIVE, args.warn_bytes, args.teamcity)
    return 0


if __name__ == '__main__':
    sys.exit(main())
