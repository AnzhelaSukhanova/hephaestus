"""The headless CI entry ``python -m ci.fuzz``."""
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time

import pytest

from ci import fuzz
from hephaestus import Fuzzer
from src.args import parse_args, validate_args
from src.tools.kotlin_compilers.sync import sync
from src.tools.kotlin_compilers.versions import load_versions
from tests.driver.support import ROOT
from tests.kotlin_compilers_fakes import publish_release
from tests.test_kotlin_compilers_releases import HOST, layout, web  # pytest fixtures


EXPECTED = {
    'backend-comparison': ('backend-comparison', None),
    'inlining-native': ('inlining-comparison', 'native'),
    'inlining-js': ('inlining-comparison', 'js'),
    'inlining-wasm': ('inlining-comparison', 'wasm'),
}


@pytest.mark.parametrize('oracle', list(fuzz.ORACLES))
@pytest.mark.parametrize('duration, seconds', [('23h', 82800), ('90m', 5400), ('45s', 45)])
def test_oracle_options_are_valid(oracle, duration, seconds, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    options = parse_args(fuzz.hephaestus_arguments(
        oracle, fuzz.parse_duration(duration), 4, 'run'))
    validate_args(options)
    expected_oracle, expected_backend = EXPECTED[oracle]
    assert options.oracle == expected_oracle
    if expected_backend is not None:
        assert options.backend == expected_backend
    assert options.iterations == 0
    assert options.seconds == seconds
    assert options.stop_cond == 'timeout'
    assert options.workers == 4
    assert options.batch == 1
    assert options.test_directory == str(tmp_path / 'bugs' / 'run')
    assert Path(options.generator_config).is_file()


def test_oracle_table_matches_the_documented_oracles():
    assert set(fuzz.ORACLES) == set(EXPECTED)


@pytest.mark.parametrize('value, seconds', [
    ('23h', 23 * 3600), ('90m', 5400), ('45s', 45), ('2d', 172800), ('007s', 7)])
def test_parse_duration(value, seconds):
    assert fuzz.parse_duration(value) == seconds


@pytest.mark.parametrize('value', ['23', 'h', '1.5h', '23H', '-1h', '0h', '23h ', '1w', ''])
def test_bad_duration_exits_2(value, capsys):
    with pytest.raises(SystemExit) as exc:
        fuzz.main(['backend-comparison', f'--duration={value}'])
    assert exc.value.code == 2
    assert 'invalid duration' in capsys.readouterr().err


def test_unknown_oracle_exits_2_and_lists_valid_ones(capsys):
    with pytest.raises(SystemExit) as exc:
        fuzz.main(['inlining-comparison'])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert all(oracle in err for oracle in fuzz.ORACLES)


def test_run_name_uses_the_master_version(master):
    assert fuzz.master_version() == master.name
    moment = datetime(2026, 11, 10, 3, 4, 5, tzinfo=timezone.utc)
    assert (fuzz.run_name('inlining-js', fuzz.master_version(), moment)
            == f'kotlin-{master.name}-inlining-js-20261110T030405Z')


def _java_opts(tmp_path) -> str:
    fuzzer = Fuzzer(parse_args(fuzz.hephaestus_arguments(
        'backend-comparison', 60, 1, 'heap') + ['-b', str(tmp_path)]))
    script = "import os; print(os.environ['JAVA_OPTS'])"
    result = fuzzer.compilation.run_process(
        [shlex.quote(sys.executable), '-c', shlex.quote(script)])
    assert result.status, result.output
    return result.output.strip()


def test_compiler_heap_defaults_to_8g(tmp_path, monkeypatch):
    monkeypatch.delenv('HEPHAESTUS_JAVA_OPTS', raising=False)
    monkeypatch.setenv('JAVA_OPTS', '-Xmx64m')
    assert _java_opts(tmp_path) == '-Xmx8g'


def test_compiler_heap_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv('HEPHAESTUS_JAVA_OPTS', '-Xmx3g -XX:+UseSerialGC')
    monkeypatch.setenv('JAVA_OPTS', '-Xmx64m')
    assert _java_opts(tmp_path) == '-Xmx3g -XX:+UseSerialGC'
    assert os.environ['JAVA_OPTS'] == '-Xmx64m'


# Whole runs: ``python -m ci.fuzz`` in a subprocess against a fake master.

RUN_TIMEOUT = 120


@dataclass
class Run:
    workspace: Path
    process: subprocess.Popen[str]

    @property
    def out(self) -> str:
        return (self.workspace / 'out.txt').read_text()

    @property
    def bugs(self) -> Path:
        return self.workspace / 'bugs'

    def run_directory(self) -> Path:
        [directory] = [path for path in self.bugs.iterdir() if path.is_dir()]
        return directory

    def wait(self, timeout: float = RUN_TIMEOUT) -> int:
        return self.process.wait(timeout=timeout)

    def wait_for(self, condition, timeout: float = RUN_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            assert self.process.poll() is None, self.out
            assert time.monotonic() < deadline, self.out
            time.sleep(0.1)

    def started(self) -> bool:
        return any(path.stat().st_size for path in self.bugs.glob('*.console.log'))


@pytest.fixture
def launch(tmp_path):
    runs: list[Run] = []

    def start(*arguments: str, **env: str) -> Run:
        workspace = tmp_path / f'run{len(runs)}'
        workspace.mkdir()
        environment = {key: value for key, value in os.environ.items()
                       if key not in ('TEAMCITY_VERSION', 'HEPHAESTUS_JAVA_OPTS')}
        environment.update(env)
        environment['PYTHONPATH'] = os.pathsep.join((str(ROOT), environment.get('PYTHONPATH', '')))
        with open(workspace / 'out.txt', 'w') as out:
            process = subprocess.Popen(
                [sys.executable, '-m', 'tests.ci_fuzz_child', str(workspace), *arguments],
                cwd=workspace, env=environment, text=True, stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        run = Run(workspace, process)
        runs.append(run)
        return run

    yield start
    for run in runs:
        # Also ends fake compilers a stopped worker left behind.
        try:
            os.killpg(run.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        run.process.wait()


def assert_complete_results(run_directory: Path) -> dict:
    for name in ('stats.json', 'faults.json', 'inconclusive.json', 'divergences.md',
                 'console.log'):
        assert (run_directory / name).is_file(), name
    assert not (run_directory / 'tmp').exists()
    assert not list(run_directory.parent.glob('*.console.log'))
    return json.loads((run_directory / 'stats.json').read_text())


def test_budget_end_exits_0_with_heartbeat_summary_and_service_messages(launch):
    run = launch('backend-comparison', '--duration', '2s', '--heartbeat', '1s',
                 '--workers', '2', '--strip-pickles', TEAMCITY_VERSION='2025.11')
    assert run.wait() == 0, run.out
    run_directory = run.run_directory()
    assert run_directory.name.startswith('kotlin-2.5.20-dev-1-backend-comparison-')
    stats = assert_complete_results(run_directory)
    totals = stats['totals']
    console = (run_directory / 'console.log').read_text()
    assert 'Test Programs Passed' in console and 'Total faults' in console
    out = run.out
    assert '\r' not in out and 'Test Programs' not in out
    assert re.search(r'^\[0:00:01\] passed \d+, failed \d+, inconclusive \d+$', out, re.M)
    assert re.search(r"^##teamcity\[progressMessage '\|\[0:00:01\|\] passed \d+", out, re.M)
    summary = (f'{run_directory.name}: time budget used up after ')
    assert summary in out
    for key, value in totals.items():
        assert f"##teamcity[buildStatisticValue key='hephaestus.{key}' value='{value}']" in out
    assert re.search(r"^##teamcity\[buildStatus text='" + re.escape(summary), out, re.M)
    assert not list(run_directory.rglob('*.bin'))
    assert 'Removed ' in out


@pytest.mark.parametrize('start_method', ['fork', 'spawn'])
def test_sigterm_stops_like_ctrl_c(launch, start_method):
    run = launch('inlining-native', '--duration', '1h', '--heartbeat', '1s', '--workers', '3',
                 CI_FUZZ_SLOW='running', CI_FUZZ_COMPILER_DELAY='60',
                 CI_FUZZ_START_METHOD=start_method)
    run.wait_for(run.started)
    time.sleep(1)
    run.process.send_signal(signal.SIGTERM)
    assert run.wait(timeout=15) == 143, run.out
    assert_complete_results(run.run_directory())
    assert ': stopped by SIGTERM after ' in run.out
    assert '##teamcity' not in run.out


def test_watchdog_stops_a_run_whose_compiler_never_returns(launch):
    run = launch('inlining-wasm', '--duration', '2s', '--grace', '2s', '--heartbeat', '1s',
                 '--workers', '2', CI_FUZZ_SLOW='running', CI_FUZZ_COMPILER_DELAY='600',
                 CI_FUZZ_START_METHOD='fork')
    assert run.wait(timeout=60) == 124, run.out
    assert_complete_results(run.run_directory())
    assert ': stopped by the watchdog after 0:00:04' in run.out


def test_sigterm_during_the_preflight_leaves_no_results(launch):
    run = launch('backend-comparison', '--duration', '1h', '--heartbeat', '1s',
                 CI_FUZZ_SLOW='always', CI_FUZZ_COMPILER_DELAY='60')
    run.wait_for(lambda: '[0:00:01]' in run.out)
    run.process.send_signal(signal.SIGTERM)
    assert run.wait(timeout=15) == 143, run.out
    [console_log] = run.bugs.iterdir()
    assert console_log.name.endswith('.console.log')
    assert console_log.read_text() == ''
    assert ': stopped by SIGTERM after ' in run.out


# HeadlessFuzzer in-process.

def headless(tmp_path, out: io.StringIO, *extra: str) -> fuzz.HeadlessFuzzer:
    options = parse_args(fuzz.hephaestus_arguments('backend-comparison', 60, 1, 'run')
                         + ['-b', str(tmp_path / 'bugs'), *extra])
    return fuzz.HeadlessFuzzer(options, heartbeat=1, grace=1, out=out)


def test_service_messages_escape_values():
    assert (fuzz.service_message('progressMessage', "it's [1|2]\nnext\r")
            == "##teamcity[progressMessage 'it|'s |[1||2|]|nnext|r']")
    assert (fuzz.service_message('buildStatisticValue', key='hephaestus.failed', value=3)
            == "##teamcity[buildStatisticValue key='hephaestus.failed' value='3']")


def test_report_prints_service_messages_only_under_teamcity(tmp_path, monkeypatch):
    monkeypatch.delenv('TEAMCITY_VERSION', raising=False)
    out = io.StringIO()
    fuzzer = headless(tmp_path, out)
    fuzzer.reporting.stats['totals'].update(passed=7, failed=2, inconclusive=1)
    fuzzer.stop_reason, fuzzer.elapsed = 'watchdog', 3725
    fuzzer.report()
    assert out.getvalue() == ('run: stopped by the watchdog after 1:02:05; '
                              'passed 7, failed 2, inconclusive 1\n')
    monkeypatch.setenv('TEAMCITY_VERSION', '2025.11')
    out.truncate(0)
    out.seek(0)
    fuzzer.report()
    assert out.getvalue().splitlines()[1:] == [
        "##teamcity[buildStatisticValue key='hephaestus.passed' value='7']",
        "##teamcity[buildStatisticValue key='hephaestus.failed' value='2']",
        "##teamcity[buildStatisticValue key='hephaestus.inconclusive' value='1']",
        "##teamcity[buildStatus text='run: stopped by the watchdog after 1:02:05; "
        "passed 7, failed 2, inconclusive 1']",
    ]


def test_only_the_first_stop_request_raises(tmp_path):
    fuzzer = headless(tmp_path, io.StringIO())
    fuzzer.request_stop('sigterm')
    assert fuzzer.stop_reason == 'budget'
    fuzzer._accepting_stops = True
    with pytest.raises(KeyboardInterrupt):
        fuzzer.request_stop('sigterm')
    fuzzer.request_stop('watchdog')
    assert fuzzer.stop_reason == 'sigterm'


def test_stats_are_saved_with_stop_signals_blocked(tmp_path):
    fuzzer = headless(tmp_path, io.StringIO())
    masks = []
    guarded = fuzzer._signals_blocked(
        lambda: masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, [])))
    guarded()
    assert {signal.SIGTERM, signal.SIGALRM} <= masks[0]
    assert not {signal.SIGTERM, signal.SIGALRM} & signal.pthread_sigmask(signal.SIG_BLOCK, [])


def test_failed_start_keeps_its_error_and_restores_the_process(tmp_path):
    out = io.StringIO()
    fuzzer = headless(tmp_path, out)
    (tmp_path / 'bugs' / 'run').mkdir(parents=True)
    stdout = sys.stdout
    handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGALRM)}
    with pytest.raises(SystemExit, match='--name run already exists'):
        fuzzer.start()
    assert sys.stdout is stdout
    assert {signum: signal.getsignal(signum) for signum in handlers} == handlers
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert not fuzzer.started
    assert fuzzer.console_log == tmp_path / 'bugs' / 'run.console.log'
    assert fuzzer.console_log.is_file()
    assert list((tmp_path / 'bugs' / 'run').iterdir()) == []


# The CI image's compiler store: ci/versions.json.

def test_ci_versions_install_only_master_falling_back_to_prerelease(web, layout, monkeypatch):
    monkeypatch.delenv('KOTLIN_MASTER_VERSION', raising=False)
    version_list = load_versions(ROOT / 'ci' / 'versions.json')
    assert [selector.text for selector in version_list.selectors] == ['master']
    publish_release(web, '2.5.0-Beta2', abi='2.5.0', wasm_launcher=True)
    web.feed('v2.5.0-Beta2', 'v2.4.21')
    assert sync(layout, version_list, HOST) == 0
    assert layout.installed_versions() == ['2.5.0-Beta2']
    assert layout.symlink_target('master') == '2.5.0-Beta2'
    assert list(layout.descriptor('2.5.0-Beta2')['backends']) == ['jvm', 'js', 'wasm', 'native']
    assert json.loads(layout.sync_json.read_text())['fallbacks_used'] == {'master': 'prerelease'}
