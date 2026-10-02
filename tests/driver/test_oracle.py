"""Oracle subprocess scenarios; unexpected child failures must never be xfailed."""
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tests.driver.support import assert_success, launch


class ConfirmedOracleDefect(Exception):
    pass


def _assert_defect_result(result: CompletedProcess[str], message: str) -> None:
    if result.returncode == 73:
        assert result.stdout.strip() == f'CONFIRMED_ORACLE_DEFECT: {message}'
        assert result.stderr == ''
        raise ConfirmedOracleDefect(message)
    assert_success(result)


@pytest.mark.parametrize('scenario', [
    'correct_accepted', 'correct_rejected', 'incorrect_accepted', 'incorrect_rejected',
    'mixed_batch', 'filtered_diagnostics', 'unknown_output',
    'report_latest_correct', 'report_latest_incorrect',
    'report_no_match_correct', 'report_no_match_incorrect', 'report_zero',
])
def test_oracle(scenario: str, tmp_path: Path) -> None:
    assert_success(launch(f'tests.driver.scenarios_oracle.{scenario}', tmp_path,
                          ['--disable-metrics']))


@pytest.mark.parametrize('debug', [False, True])
def test_compiler_crash(debug: bool, tmp_path: Path) -> None:
    arguments = ['--disable-metrics'] + (['--debug'] if debug else [])
    assert_success(launch('tests.driver.scenarios_oracle.compiler_crash',
                          tmp_path, arguments))


@pytest.mark.parametrize('scenario', ['debug_exit', 'debug_incorrect_acceptance'])
def test_debug(scenario: str, tmp_path: Path) -> None:
    assert_success(launch(f'tests.driver.scenarios_oracle.{scenario}', tmp_path,
                          ['--disable-metrics', '--debug']))


@pytest.mark.parametrize('scenario', ['rerun_correct', 'rerun_incorrect'])
def test_rerun(scenario: str, tmp_path: Path) -> None:
    assert_success(launch(f'tests.driver.scenarios_oracle.{scenario}', tmp_path,
                          ['--disable-metrics', '--rerun', '-t', '3']))


@pytest.mark.parametrize('phase', ['frontend', 'backend'])
@pytest.mark.parametrize('disabled', [False, True])
@pytest.mark.parametrize('timing', [False, True])
def test_enrichment(phase: str, disabled: bool, timing: bool, tmp_path: Path) -> None:
    arguments = (['--disable-metrics'] if disabled else [])
    arguments += ['--time-metrics'] if timing else []
    assert_success(launch(f'tests.driver.scenarios_oracle.enrichment_{phase}',
                          tmp_path, arguments))


@pytest.mark.xfail(strict=True, raises=ConfirmedOracleDefect,
                   reason='Confirmed baseline oracle record/error handling defects')
@pytest.mark.parametrize(('scenario', 'message'), [
    ('generation_failed', 'generation-failed record omitted'),
    ('generation_failed_crash', 'generation-failed record omitted during crash'),
    ('missing_injected_error', 'None injected error concatenated'),
])
def test_oracle_defects(scenario: str, message: str, tmp_path: Path) -> None:
    result = launch(f'tests.driver.scenarios_oracle.{scenario}', tmp_path,
                    ['--disable-metrics'])
    _assert_defect_result(result, message)


@pytest.mark.xfail(strict=True, raises=ConfirmedOracleDefect,
                   reason='check_oracle_mul swallows unexpected oracle exceptions')
@pytest.mark.parametrize('stacktrace', [False, True])
def test_wrapper_exception(stacktrace: bool, tmp_path: Path) -> None:
    arguments = ['--disable-metrics'] + (['--print-stacktrace'] if stacktrace else [])
    result = launch('tests.driver.scenarios_oracle.wrapper_exception',
                    tmp_path, arguments)
    _assert_defect_result(result, 'oracle exception swallowed by wrapper')


@pytest.mark.xfail(strict=True, raises=ConfirmedOracleDefect,
                   reason='Unrecognized nonzero compiler output is treated as acceptance')
def test_unknown_nonzero_output(tmp_path: Path) -> None:
    result = launch('tests.driver.scenarios_oracle.unknown_nonzero_output', tmp_path,
                    ['--disable-metrics'])
    _assert_defect_result(result, 'nonzero compiler failure treated as acceptance')


@pytest.mark.parametrize(('returncode', 'stdout', 'stderr'), [
    (1, 'CONFIRMED_ORACLE_DEFECT: expected', ''),
    (2, '', 'argument parsing failed'),
    (73, 'CONFIRMED_ORACLE_DEFECT: unrelated', ''),
    (73, 'CONFIRMED_ORACLE_DEFECT: expected', 'unexpected traceback'),
])
def test_defect_guard(returncode: int, stdout: str, stderr: str) -> None:
    result = CompletedProcess(['scenario'], returncode, stdout, stderr)
    with pytest.raises(AssertionError):
        _assert_defect_result(result, 'expected')