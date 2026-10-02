import json
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from src import utils
from src.translators.kotlin import KotlinTranslator
from tests.driver.contracts import duration, json_value, program, record
from tests.driver.fixtures import replay
from tests.driver.support import assert_success, launch


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    # Supported-path cases must be independent of pytest's hyphenated base path.
    with TemporaryDirectory(prefix='driver_script_') as directory:
        yield Path(directory)


@pytest.mark.parametrize('scenario', ['script', 'reject', 'crash'])
@pytest.mark.parametrize('retention', ['', '--keep-all', '--keep-everything'])
@pytest.mark.parametrize('dry_run', [False, True])
def test_script_artifacts(tmp_path: Path, scenario: str,
                          retention: str, dry_run: bool) -> None:
    source = tmp_path / 'replay.bin'
    replay(source)
    arguments = ['--replay', str(source), '--disable-metrics', '--time-metrics']
    if retention:
        arguments.append(retention)
    if dry_run:
        arguments.append('--dry-run')
    result = launch('tests.driver.scenarios_script.' + scenario, tmp_path, arguments)
    assert_success(result)
    root = tmp_path / 'bugs/session'
    failed = scenario != 'script' and not dry_run
    stats = record(json.loads((root / 'stats.json').read_text()))
    json_value(stats)
    assert stats['totals'] == {'passed': int(not failed), 'failed': int(failed)}
    assert record(stats['Info'])['compiler'] == 'hermetic compiler 1.0'
    duration(stats['time'])
    duration(stats['compilation_time'])
    faults = record(json.loads((root / 'faults.json').read_text()))
    assert set(faults) == ({'1'} if failed else set())
    if failed:
        error = record(faults['1'])['error']
        assert isinstance(error, str)
        assert ('controlled rejection' if scenario == 'reject' else 'controlled crash') in error
    assert not (root / 'tmp').exists()
    assert not (root / 'escalations.json').exists()
    assert (root / 'generator/iter_1/program.kt').exists() == bool(retention)
    preserved = failed or (retention == '--keep-everything' and not dry_run)
    assert (root / '1/program.kt').exists() == preserved
    if preserved:
        loaded = program(utils.load_program(str(root / '1/program.kt.bin')))
        translator = KotlinTranslator('src.d1')
        translator.visit(loaded)
        assert (root / '1/program.kt').read_text() == translator.result()
    tools = [record(json.loads(line)) for line in (tmp_path / 'tool.jsonl').read_text().splitlines()]
    assert tools[0]['arguments'] == ['-version']
    assert len(tools) == (1 if dry_run else 2)
    for tool in tools:
        assert tool['java_opts'] == '-Xmx8g'
    if not dry_run:
        argv = tools[1]['arguments']
        assert isinstance(argv, list)
        assert '-produce' in argv and 'library' in argv
        assert '-Xklib-ir-inliner=full' in argv
    assert f'Total faults: {int(failed)}' in result.stdout


def test_script_expected_rejection(tmp_path: Path) -> None:
    # JavaCompiler only supports simple diagnostic path characters. Keep the
    # hyphenated-path counterexample separate, not accepted as a success contract.
    with TemporaryDirectory(prefix='driver_expected_') as directory:
        workspace = Path(directory)
        source = workspace / 'replay.bin'
        replay(source, 'java')
        result = launch('tests.driver.scenarios_script.expected_rejection', workspace,
                        ['--language', 'java', '--replay', str(source), '--keep-everything'])
        assert_success(result)
        root = workspace / 'bugs/session'
        stats = record(json.loads((root / 'stats.json').read_text()))
        assert stats['totals'] == {'passed': 1, 'failed': 0}
        assert json.loads((root / 'faults.json').read_text()) == {}
        assert 'Integer answer = 1' in (root / '1/Main.java').read_text()
        assert 'String answer = 1' in (root / '1/Incorrect.java').read_text()
        assert not (root / 'tmp').exists()


class DiagnosticPathLoss(Exception):
    pass


@pytest.mark.xfail(strict=True, raises=DiagnosticPathLoss,
                   reason='JavaCompiler drops the prefix of hyphenated diagnostic paths')
def test_expected_rejection_with_hyphenated_path(tmp_path: Path) -> None:
    workspace = tmp_path / 'hyphen-path'
    workspace.mkdir()
    source = workspace / 'replay.bin'
    replay(source, 'java')
    assert_success(launch('tests.driver.scenarios_script.expected_rejection', workspace,
                          ['--language', 'java', '--replay', str(source), '--keep-everything']))
    root = workspace / 'bugs/session'
    faults = record(json.loads((root / 'faults.json').read_text()))
    if faults:
        assert set(faults) == {'1'}
        assert record(faults['1'])['error'] == (
            'SHOULD NOT BE COMPILED: String expected but Integer found')
        raise DiagnosticPathLoss('An actual rejection was attributed as acceptance')