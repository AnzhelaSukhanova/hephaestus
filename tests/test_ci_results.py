"""The CI results collector ``python -m ci.results``."""
import io
import json
import os
from pathlib import Path
import signal
import zipfile

import pytest

from ci import results
from src.modules.worker_reporting import Reporting
from src.tools.outcomes import read_report
from tests.test_ci_fuzz import headless, launch  # noqa: F401  (pytest fixture)


RUN = 'kotlin-2.5.20-dev-1-backend-comparison-20261117T120000Z'
PREFIX = f'{results.PREFIX}/{RUN}'
CRASH = '\n'.join([
    'exception: java.lang.RuntimeException: Exception while generating code for public open fun '
    'backups (izvestia: kotlin.Any): kotlin.Unit declared in src.d3.Raphael:',
    '\tat org.jetbrains.kotlin.backend.jvm.codegen.FunctionCodegen.generate(FunctionCodegen.kt:57)',
    '\tat org.jetbrains.kotlin.backend.jvm.codegen.ClassCodegen.generate(ClassCodegen.kt:170)',
    'Caused by: java.util.NoSuchElementException: Array is empty.',
    '\tat kotlin.collections.ArraysKt___ArraysKt.last(_Arrays.kt:1234)',
    '\tat org.jetbrains.kotlin.codegen.inline.MethodInliner.doInline(MethodInliner.kt:99)',
    '\tat org.jetbrains.kotlin.codegen.inline.MethodInliner.inline(MethodInliner.kt:77)',
    '\tat org.jetbrains.kotlin.codegen.inline.InlineCodegen.inline(InlineCodegen.kt:55)',
])
CRASH_SIGNATURE = ('java.util.NoSuchElementException | '
                   'kotlin.collections.ArraysKt___ArraysKt.last | '
                   'org.jetbrains.kotlin.codegen.inline.MethodInliner.doInline | '
                   'org.jetbrains.kotlin.codegen.inline.MethodInliner.inline')


def dependencies(closure):
    return {'direct': closure[0] if closure else None, 'closure': closure,
            'kept': closure, 'dropped': [], 'variants': {}}


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2))
    return path


@pytest.fixture
def bugs(tmp_path) -> Path:
    """A run laid out like a real one: programs live in <run>/<id>/, dependencies are
    sibling ids, and the JSON names deleted temp paths."""
    bugs = tmp_path / 'bugs'
    run = bugs / RUN
    faults = {
        '3': {'error': CRASH, 'oracle': 'backend-comparison',
              'programs': {'/tmp/tmpabc/src/d3/program.kt': True},
              'comparison_dependencies': dependencies([1]),
              'comparison': {'findings': [{'kind': 'crash', 'diagnostics': [CRASH]}]}},
        '5': {'error': "Backend acceptance differs: {'accepted': ('js',)}",
              'oracle': 'backend-comparison',
              'programs': {'/tmp/tmpdef/src/d5/program.kt': True},
              'comparison_dependencies': dependencies([]),
              'comparison': {'findings': [{'kind': 'status_mismatch', 'diagnostics': []}]}},
        '7': {'error': 'org.jetbrains.kotlin.CompilerException\ncontrolled crash',
              'oracle': 'backend-comparison', 'programs': {'/tmp/x/src/d7/program.kt': True},
              'comparison_dependencies': dependencies([6]),
              'comparison': {'findings': [{'kind': 'crash', 'diagnostics': []}]}},
    }
    inconclusive = {
        '9': {'error': 'empty range', 'program': None, 'oracle': 'backend-comparison',
              'inconclusive_reason': 'generation_failed',
              'comparison': {'findings': [], 'incomplete_reasons': ['Generation failed']}},
        '11': {'error': '', 'program': '/tmp/y/src/d11/program.kt',
               'oracle': 'backend-comparison',
               'inconclusive_reason': 'backend_comparison_incomplete', 'comparison': {}},
    }
    write(run / 'faults.json', faults)
    write(run / 'inconclusive.json', inconclusive)
    write(run / 'escalations.json', {'4': [{'call_stack_semantic': []}]})
    write(run / 'stats.json', {'Info': {'name': RUN, 'bugs': '/opt/hephaestus/bugs',
                                        'compiler': 'jvm: info: kotlinc-jvm 2.5.20-dev-1'},
                               'totals': {'passed': 6, 'failed': 3, 'inconclusive': 2}})
    write(run / 'time_metrics.json', b'{"1": {"generation_cpu": 0.5}, "2": {')  # cut off
    write(run / 'crossmodule.json', {'1': {'direct': None, 'closure': []}})
    write(run / 'resources.json', {'plan': {'workers': 4, 'java_opts': '-Xmx6144m'},
                                   'measured': {'container_peak_bytes': 123}})
    write(run / 'divergences.md', '# Divergences\n')
    write(run / 'console.log', 'Test Programs Passed 6 / 11\n')
    write(run / 'klibs' / 'd1-js.klib', b'PK klib')
    for pid in ('1', '2', '3', '4', '5', '11'):
        write(run / pid / 'program.kt', f'package src.d{pid}\n')
        write(run / pid / 'program.kt.bin', b'pickle')
        write(run / pid / 'variants' / 'js' / f'd{pid}.klib', b'PK klib')
        write(run / pid / 'variants' / 'js' / 'result.json',
              {'reproduce_argv': ['kotlinc-js', 'program.kt', '-library',
                                  '../klibs/d1-js.klib']})
    write(run / '3' / 'comparison.json', {'source': 'program.kt'})
    write(run / '3' / 'variants' / 'jvm' / 'output.txt', CRASH)
    write(run / '3' / 'variants' / 'jvm' / 'Raphael.class', b'\xca\xfe')
    return bugs


def archive_names(out: Path) -> list[str]:
    with zipfile.ZipFile(out / results.ARCHIVE) as archive:
        return archive.namelist()


def read_member(out: Path, name: str) -> bytes:
    with zipfile.ZipFile(out / results.ARCHIVE) as archive:
        return archive.read(name)


def program_ids(names) -> set[str]:
    ids = set()
    for name in names:
        parts = name.split('/')
        if len(parts) > 3 and parts[1] == RUN and parts[2].isdigit():
            ids.add(parts[2])
    return ids


def test_packs_findings_and_their_dependencies_without_binaries(bugs, tmp_path):
    out = tmp_path / 'out'
    results.collect(bugs, out)
    names = archive_names(out)
    # Faults 3 and 5, inconclusive 11 and fault 3's dependency 1; not the passing program 2
    # nor 4, which only escalations.json mentions.
    assert program_ids(names) == {'1', '3', '5', '11'}
    assert f'{PREFIX}/3/program.kt' in names
    assert f'{PREFIX}/3/comparison.json' in names
    assert f'{PREFIX}/3/variants/jvm/output.txt' in names
    assert f'{PREFIX}/1/variants/js/result.json' in names
    assert not [name for name in names if name.endswith(results.EXCLUDED_SUFFIXES)]
    assert not [name for name in names if '/klibs/' in name]


def test_result_files_are_packed_byte_for_byte(bugs, tmp_path):
    out = tmp_path / 'out'
    summary = results.collect(bugs, out)
    for name in results.RUN_FILES + results.TEXT_FILES:
        assert read_member(out, f'{PREFIX}/{name}') == (bugs / RUN / name).read_bytes(), name
    # The cut-off time_metrics.json is packed as it is and reported, not fatal
    assert summary['invalid_json'] == [f'{RUN}/time_metrics.json']
    manifest = json.loads(read_member(out, f'{results.PREFIX}/manifest.json'))
    assert [entry['path'] for entry in manifest['invalid_json']] == [f'{RUN}/time_metrics.json']


def test_reports_missing_folders_only_where_a_program_is_expected(bugs, tmp_path):
    out = tmp_path / 'out'
    summary = results.collect(bugs, out)
    manifest = json.loads(read_member(out, f'{results.PREFIX}/manifest.json'))
    # Fault 7 and its dependency 6 have no folder; inconclusive 9 never had a program.
    assert manifest['missing'] == [
        {'run': RUN, 'id': '6', 'referenced_by': ['closure of 7']},
        {'run': RUN, 'id': '7', 'referenced_by': ['fault:7']},
    ]
    assert summary['missing'] == 2
    assert manifest['bugs_root'] == str(bugs.resolve())
    assert manifest['run_bugs_roots'] == {RUN: '/opt/hephaestus/bugs'}
    files = {entry['path']: entry for entry in manifest['files']}
    assert files[f'{RUN}/3/program.kt']['size'] == len('package src.d3\n')


def test_index_groups_findings_by_signature(bugs, tmp_path):
    out = tmp_path / 'out'
    summary = results.collect(bugs, out)
    lines = [json.loads(line) for line in
             read_member(out, f'{results.PREFIX}/index/findings.jsonl').splitlines()]
    by_id = {line['id']: line for line in lines}
    assert set(by_id) == {'3', '5', '7', '9', '11'}
    assert by_id['3']['signature'] == CRASH_SIGNATURE
    assert by_id['3']['closure'] == ['1']
    assert by_id['3']['program_dirs'] == [f'{RUN}/3', f'{RUN}/1']
    assert by_id['3']['error_head'][0].startswith('exception: java.lang.RuntimeException')
    assert by_id['5']['signature'] == 'backend-comparison: status_mismatch'
    assert by_id['7']['signature'] == 'org.jetbrains.kotlin.CompilerException'
    assert by_id['9']['signature'] == 'inconclusive: generation_failed'
    assert by_id['9']['kind'] == 'inconclusive'
    assert summary['distinct_signatures'] == 5
    assert summary['runs'][RUN] == {'totals': {'passed': 6, 'failed': 3, 'inconclusive': 2},
                                    'faults': 3, 'inconclusive': 2}


@pytest.mark.parametrize('text, expected', [
    (CRASH, CRASH_SIGNATURE),
    ('org.jetbrains.kotlin.CompilerException\ncontrolled crash',
     'org.jetbrains.kotlin.CompilerException'),
    ("error: 'oaten' at /tmp/a/b.kt:12 is wrong", 'error: <q> at <path>:<n> is wrong'),
    ('', '<no error text>'),
])
def test_crash_signature(text, expected):
    assert results.crash_signature(text) == expected


def test_outputs_are_renamed_into_place(bugs, tmp_path):
    out = tmp_path / 'out'
    results.collect(bugs, out)
    assert sorted(path.name for path in out.iterdir()) == [
        'hephaestus-results.zip', 'provenance.json', 'summary.json']
    assert (out / 'summary.json').read_bytes() == read_member(
        out, f'{results.PREFIX}/summary.json')
    assert (out / 'provenance.json').read_bytes() == read_member(
        out, f'{results.PREFIX}/provenance.json')
    assert read_member(out, f'{results.PREFIX}/README.md').startswith(b'# Hephaestus results')


def agent_files(directory: Path, container_state: dict | None) -> Path:
    write(directory / 'teamcity.env', 'build_id=42\nbranch=hephaestus/ci-main\n# comment\n')
    write(directory / 'host.env', 'cpus=16\n')
    write(directory / 'image.json', [{'Id': 'sha256:abc', 'Config': {'Labels': {
        'org.opencontainers.image.revision': 'f00d',
        'org.jetbrains.hephaestus.branch': 'hephaestus/ci-main',
        'org.jetbrains.kotlin.master-version': '2.5.20-dev-1',
        'org.jetbrains.kotlin.commit': 'beef'}}}])
    if container_state is not None:
        write(directory / 'container.json', [{
            'Args': ['--', 'python', '-m', 'ci.fuzz', 'backend-comparison'],
            'State': container_state,
            'HostConfig': {'NanoCpus': 16_000_000_000, 'Memory': 30 * 2**30}}])
    return directory


def test_provenance_merges_image_container_and_teamcity_facts(bugs, tmp_path):
    out = tmp_path / 'out'
    agent = agent_files(tmp_path / 'agent', {
        'ExitCode': 143, 'Running': False, 'OOMKilled': False,
        'StartedAt': '2026-11-17T01:00:00Z', 'FinishedAt': '2026-11-17T23:59:00Z'})
    results.collect(bugs, out, agent)
    provenance = json.loads((out / 'provenance.json').read_text())
    assert provenance['checkpoint'] is False
    assert provenance['run'] == {'stop_reason': 'stopped by SIGTERM', 'exit_code': 143,
                                 'started_at': '2026-11-17T01:00:00Z',
                                 'finished_at': '2026-11-17T23:59:00Z', 'oom_killed': False}
    assert provenance['hephaestus']['commit'] == 'f00d'
    assert provenance['hephaestus']['command'][-1] == 'backend-comparison'
    assert provenance['hephaestus']['runs'][RUN]['bugs'] == '/opt/hephaestus/bugs'
    assert provenance['kotlin']['requested_master_version'] == '2.5.20-dev-1'
    assert provenance['kotlin']['commit'] == 'beef'
    assert provenance['kotlin']['compiler_versions_by_run'] == {
        RUN: 'jvm: info: kotlinc-jvm 2.5.20-dev-1'}
    assert provenance['kotlin']['versions_json'] == json.loads(
        (results.ROOT / 'versions.json').read_text())
    assert provenance['runtime']['image_id'] == 'sha256:abc'
    assert provenance['runtime']['cpu_limit'] == 16
    assert provenance['runtime']['sizing'][RUN]['plan']['java_opts'] == '-Xmx6144m'
    assert provenance['teamcity'] == {'build_id': '42', 'branch': 'hephaestus/ci-main'}
    assert provenance['agent'] == {'cpus': '16'}
    summary = json.loads((out / 'summary.json').read_text())
    assert summary['stop_reason'] == 'stopped by SIGTERM'


@pytest.mark.parametrize('state, checkpoint, expected', [
    ({'ExitCode': 0}, False, 'time budget used up'),
    ({'ExitCode': 143}, False, 'stopped by SIGTERM'),
    ({'ExitCode': 124}, False, 'stopped by the watchdog'),
    ({'ExitCode': 74}, False, 'stopped: disk almost full'),
    ({'ExitCode': 2}, False, 'rejected its arguments (exit 2)'),
    ({'ExitCode': 1}, False, 'failed (exit 1)'),
    ({'ExitCode': 137, 'OOMKilled': True}, False, 'killed: out of memory (exit 137)'),
    ({'ExitCode': 0, 'Running': True}, False, 'still running'),
    (None, True, 'still running (checkpoint)'),
    (None, False, 'unknown: no container state'),
])
def test_stop_reason(state, checkpoint, expected):
    container = None if state is None else {'State': state}
    assert results.stop_reason(container, checkpoint) == expected


def test_no_run_yet_still_packs_provenance_and_the_console_log(tmp_path):
    bugs = tmp_path / 'bugs'
    write(bugs / f'{RUN}.console.log', 'preflight\n')
    out = tmp_path / 'out'
    summary = results.collect(bugs, out, checkpoint=True)
    assert summary['runs'] == {} and summary['checkpoint'] is True
    assert summary['stop_reason'] == 'still running (checkpoint)'
    names = archive_names(out)
    assert f'{results.PREFIX}/logs/{RUN}.console.log' in names
    assert f'{results.PREFIX}/provenance.json' in names
    assert results.collect(tmp_path / 'absent', tmp_path / 'out2')['runs'] == {}


def test_report_prints_statistics_and_warnings_for_teamcity(bugs, tmp_path):
    out = tmp_path / 'out'
    summary = results.collect(bugs, out)
    text = io.StringIO()
    results.report(summary, out / results.ARCHIVE, warn_bytes=10, teamcity=True, out=text)
    lines = text.getvalue().splitlines()
    assert lines[0].startswith('hephaestus-results.zip: ')
    assert '5 findings in 5 signatures, 2 missing' in lines[0]
    assert "##teamcity[buildStatisticValue key='hephaestus.signatures' value='5']" in lines
    assert "##teamcity[buildStatisticValue key='hephaestus.missing' value='2']" in lines
    warnings = [line for line in lines if line.startswith("##teamcity[message text=")]
    assert len(warnings) == 3  # invalid JSON, missing folders, size
    plain = io.StringIO()
    results.report(summary, out / results.ARCHIVE, warn_bytes=10**9, teamcity=False, out=plain)
    assert '##teamcity' not in plain.getvalue()


def test_main_writes_to_out(bugs, tmp_path, capsys):
    assert results.main(['--bugs', str(bugs), '--out', str(tmp_path / 'out'),
                         '--checkpoint']) == 0
    assert 'hephaestus-results.zip: ' in capsys.readouterr().out
    assert json.loads((tmp_path / 'out' / 'summary.json').read_text())['checkpoint'] is True


# Atomic result files: the collector reads them while Hephaestus rewrites them.

def test_save_stats_renames_result_files_into_place(tmp_path):
    fuzzer = headless(tmp_path, io.StringIO())
    reporting: Reporting = fuzzer.reporting
    reporting.stats['faults'] = {'1': {'error': 'boom'}}
    reporting.save_stats()
    run = Path(fuzzer.options.test_directory)
    inodes = {path.name: path.stat().st_ino for path in run.iterdir()}
    reporting.stats['faults']['2'] = {'error': 'again'}
    reporting.save_stats()
    assert not list(run.glob('*.tmp'))
    for name in ('faults.json', 'inconclusive.json', 'stats.json'):
        assert (run / name).stat().st_ino != inodes[name], name
    assert json.loads((run / 'faults.json').read_text()) == {
        '1': {'error': 'boom'}, '2': {'error': 'again'}}
    assert 'faults' not in json.loads((run / 'stats.json').read_text())


# A live run: checkpoints while Hephaestus writes, then the final archive.

def fault_with_closure(run) -> tuple[Path, str, list[str]] | None:
    directories = ([path for path in run.bugs.iterdir() if path.is_dir()]
                   if run.bugs.is_dir() else [])
    if not directories:
        return None
    try:
        faults = read_report(str(directories[0] / 'faults.json'))
    except ValueError:
        return None
    for pid, entry in faults.items():
        closure = [str(dependency) for dependency in
                   (entry.get('comparison_dependencies') or {}).get('closure') or ()]
        if closure:
            return directories[0], pid, closure
    return None


def assert_archive_is_consistent(out: Path) -> dict:
    with zipfile.ZipFile(out / results.ARCHIVE) as archive:
        names = archive.namelist()
        [faults_name] = [name for name in names if name.endswith('/faults.json')]
        faults = json.loads(archive.read(faults_name))
    run = faults_name.split('/')[1]
    for pid in faults:
        assert f'{results.PREFIX}/{run}/{pid}/program.kt' in names, pid
    return faults


def test_checkpoints_of_a_live_cross_module_run(launch, tmp_path):
    # Every second program crashes; the others pass and become dependencies.
    run = launch('backend-comparison', '--duration', '10m', '--heartbeat', '1s',
                 '--workers', '2', DRIVER_COMPILER_MODE='crash', DRIVER_CRASH_MODULO='2')
    run.wait_for(lambda: fault_with_closure(run) is not None)
    for n in range(3):
        out = tmp_path / f'checkpoint-{n}'
        summary = results.collect(run.bugs, out, checkpoint=True)
        assert summary['checkpoint'] is True and summary['invalid_json'] == []
        assert_archive_is_consistent(out)
        [sizing] = json.loads((out / 'provenance.json').read_text())['runtime']['sizing'].values()
        assert sizing['plan']['workers'] == 2 and sizing['measured'] is None
    run.process.send_signal(signal.SIGTERM)
    assert run.wait(timeout=60) == 143, run.out

    found = fault_with_closure(run)
    assert found is not None
    directory, pid, closure = found
    out = tmp_path / 'final'
    summary = results.collect(run.bugs, out)
    faults = assert_archive_is_consistent(out)
    assert pid in faults
    names = archive_names(out)
    for dependency in closure:
        assert f'{results.PREFIX}/{directory.name}/{dependency}/program.kt' in names, dependency
    assert f'{results.PREFIX}/{directory.name}/console.log' in names
    assert f'{results.PREFIX}/{directory.name}/resources.json' in names
    sizing = json.loads((out / 'provenance.json').read_text())['runtime']['sizing']
    assert sizing[directory.name]['measured']['largest_process_rss_bytes'] > 0
    assert summary['missing'] == 0
    assert [entry['signature'] for entry in summary['signatures']
            if entry['kind'] == 'fault'] == ['org.jetbrains.kotlin.CompilerException']
    assert not [name for name in names if name.endswith(('.klib', '.bin'))]
    assert not [path for path in out.iterdir() if path.suffix == '.tmp']
    assert not os.path.exists(directory / 'faults.json.tmp')
