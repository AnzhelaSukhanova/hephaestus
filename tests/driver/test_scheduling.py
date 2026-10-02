"""Scheduling contracts run in fresh processes, without real pool workers."""
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tests.driver.support import assert_success, launch


class ConfirmedSchedulingDefect(Exception):
    pass


def _assert_defect_result(result: CompletedProcess[str], message: str) -> None:
    if result.returncode == 74:
        assert result.stdout.strip() == f'CONFIRMED_SCHEDULING_DEFECT: {message}'
        assert result.stderr == ''
        raise ConfirmedSchedulingDefect(message)
    assert_success(result)


@pytest.mark.parametrize('scenario', [
    'boundaries', 'loop_iterations', 'loop_timeout', 'loop_overshoot',
    'loop_stop', 'loop_unbounded', 'loop_preset_stop',
    'loop_generation_interrupt', 'loop_result_interrupt', 'loop_exception',
    'wrappers', 'serial_interrupt', 'serial_stopped', 'oracle_submit_interrupt',
    'shutdown_close', 'shutdown_join', 'shutdown_terminate_error',
    'shutdown_join_error', 'shutdown_stopped',
    'parallel_stopped', 'serial_generation_interrupt', 'serial_oracle_interrupt',
    'parallel_get_interrupt',
])
def test_scheduling(scenario: str, tmp_path: Path) -> None:
    assert_success(launch(f'tests.driver.scenarios_scheduling.{scenario}', tmp_path))


@pytest.mark.parametrize('parallel', [False, True])
@pytest.mark.parametrize('disabled', [False, True])
@pytest.mark.parametrize('dry', [False, True])
def test_lifecycle(parallel: bool, disabled: bool, dry: bool, tmp_path: Path) -> None:
    arguments = ['-i', '5', '--batch', '2']
    if parallel:
        arguments += ['--workers', '2']
    if disabled:
        arguments += ['--disable-metrics']
    if dry:
        arguments += ['--dry-run']
    scenario = 'parallel' if parallel else 'serial'
    assert_success(launch(f'tests.driver.scenarios_scheduling.{scenario}', tmp_path, arguments))


def test_debug_selects_serial(tmp_path: Path) -> None:
    assert_success(launch('tests.driver.scenarios_scheduling.serial', tmp_path,
                          ['-i', '5', '--batch', '2', '--workers', '2', '--debug']))


@pytest.mark.xfail(strict=True, raises=ConfirmedSchedulingDefect,
                   reason='Interrupted generation results/handles are consumed without a None check')
@pytest.mark.parametrize(('scenario', 'message'), [
    ('none_worker_result', 'None generation result consumed'),
    ('none_submission_handle', 'missing generation handle consumed'),
])
def test_interrupted_generation_defect(scenario: str, message: str, tmp_path: Path) -> None:
    result = launch(f'tests.driver.scenarios_scheduling.{scenario}', tmp_path)
    _assert_defect_result(result, message)


@pytest.mark.xfail(strict=True, raises=ConfirmedSchedulingDefect,
                   reason='Dry-run skips oracle cleanup of batch temporary directories')
@pytest.mark.parametrize('parallel', [False, True])
def test_dry_run_cleanup_defect(parallel: bool, tmp_path: Path) -> None:
    arguments = ['-i', '5', '--batch', '2', '--dry-run']
    if parallel:
        arguments += ['--workers', '2']
    result = launch('tests.driver.scenarios_scheduling.dry_run_cleanup', tmp_path, arguments)
    _assert_defect_result(result, 'dry-run batch directories leaked')


@pytest.mark.parametrize(('returncode', 'stdout', 'stderr'), [
    (1, 'CONFIRMED_SCHEDULING_DEFECT: expected', ''),
    (2, '', 'argument parsing failed'),
    (74, 'CONFIRMED_SCHEDULING_DEFECT: unrelated', ''),
    (74, 'CONFIRMED_SCHEDULING_DEFECT: expected', 'unexpected traceback'),
])
def test_defect_guard(returncode: int, stdout: str, stderr: str) -> None:
    result = CompletedProcess(['scenario'], returncode, stdout, stderr)
    with pytest.raises(AssertionError):
        _assert_defect_result(result, 'expected')