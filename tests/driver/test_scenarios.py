from pathlib import Path

import pytest

from tests.driver.support import assert_success, launch


@pytest.mark.parametrize('language', ['kotlin', 'groovy', 'java', 'scala'])
@pytest.mark.parametrize('keep_all', [False, True])
@pytest.mark.parametrize('disable_metrics', [False, True])
def test_replay_roundtrip(tmp_path: Path, language: str,
                          keep_all: bool, disable_metrics: bool) -> None:
    arguments = ['--language', language]
    if keep_all:
        arguments.append('--keep-all')
    if disable_metrics:
        arguments.append('--disable-metrics')
    assert_success(launch('tests.driver.scenarios_generation.replay_roundtrip',
                          tmp_path, arguments))


@pytest.mark.parametrize('language', ['kotlin', 'groovy', 'java', 'scala'])
def test_generated_roundtrip(tmp_path: Path, language: str) -> None:
    assert_success(launch('tests.driver.scenarios_generation.generated_roundtrip',
                          tmp_path, ['--language', language]))


@pytest.mark.parametrize('scenario', ['cp_skipped', 'ncp_skipped', 'ncp_changed'])
@pytest.mark.parametrize('language', ['kotlin', 'groovy', 'java', 'scala'])
@pytest.mark.parametrize('keep_all', [False, True])
def test_transformation_files(tmp_path: Path, scenario: str,
                              language: str, keep_all: bool) -> None:
    arguments = ['--language', language, '-t', '2' if scenario == 'cp_skipped' else '0']
    if keep_all:
        arguments.append('--keep-all')
    assert_success(launch(f'tests.driver.scenarios_generation.{scenario}',
                          tmp_path, arguments))


@pytest.mark.parametrize(('scenario', 'arguments'), [
    ('cp_type_erasure', ['-t', '2']),
    ('gen_cp_changed', ['-t', '2']),
    ('cp_replacements', ['-t', '4']),
    ('gen_ncp_skipped', []),
    ('gen_ncp_changed', []),
])
@pytest.mark.parametrize('keep_all', [False, True])
def test_generation_transformations(tmp_path: Path, scenario: str,
                                    arguments: list[str], keep_all: bool) -> None:
    assert_success(launch(f'tests.driver.scenarios_generation.{scenario}',
                          tmp_path, [*arguments, *(['--keep-all'] if keep_all else [])]))


@pytest.mark.parametrize('disable_metrics', [False, True])
def test_escalations(tmp_path: Path, disable_metrics: bool) -> None:
    assert_success(launch('tests.driver.scenarios_generation.escalations', tmp_path,
                          ['--disable-metrics'] if disable_metrics else []))


@pytest.mark.parametrize('scenario', ['generation_error', 'cp_error', 'ncp_error'])
@pytest.mark.parametrize('print_stacktrace', [False, True])
@pytest.mark.parametrize('debug', [False, True])
def test_failure_records(tmp_path: Path, scenario: str,
                         print_stacktrace: bool, debug: bool) -> None:
    arguments = ['-t', '2']
    if print_stacktrace:
        arguments.append('--print-stacktrace')
    if debug:
        arguments.append('--debug')
    assert_success(launch(f'tests.driver.scenarios_generation.{scenario}',
                          tmp_path, arguments))


def test_examine(tmp_path: Path) -> None:
    assert_success(launch('tests.driver.scenarios_generation.examine',
                          tmp_path, ['--examine', '--replay', 'input.bin']))
