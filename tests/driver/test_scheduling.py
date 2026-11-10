"""Scheduling contracts run in fresh processes, without real pool workers."""
from pathlib import Path

import pytest

from tests.driver.support import assert_success, launch


@pytest.mark.parametrize('scenario', [
    'boundaries', 'loop_iterations', 'loop_timeout', 'loop_overshoot',
    'loop_stop', 'loop_unbounded', 'loop_preset_stop',
    'loop_generation_interrupt', 'loop_result_interrupt', 'loop_exception',
    'wrappers', 'serial_interrupt', 'serial_stopped', 'oracle_submit_interrupt',
    'shutdown_close', 'shutdown_join', 'shutdown_terminate_error',
    'shutdown_join_error', 'shutdown_stopped',
    'parallel_stopped', 'serial_generation_interrupt', 'serial_oracle_interrupt',
    'parallel_wait_interrupt', 'generation_none_result', 'generation_submit_interrupt',
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


@pytest.mark.parametrize('parallel', [False, True])
def test_dry_run_cleanup(parallel: bool, tmp_path: Path) -> None:
    arguments = ['-i', '5', '--batch', '2', '--dry-run']
    if parallel:
        arguments += ['--workers', '2']
    assert_success(launch('tests.driver.scenarios_scheduling.dry_run_cleanup',
                          tmp_path, arguments))
