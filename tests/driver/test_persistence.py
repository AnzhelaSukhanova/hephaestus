from pathlib import Path

import pytest

from tests.driver.support import assert_success, launch


@pytest.mark.parametrize('disable_metrics', [False, True])
@pytest.mark.parametrize('time_metrics', [False, True])
@pytest.mark.parametrize('debug', [False, True])
def test_accumulated_stats(tmp_path: Path, disable_metrics: bool,
                           time_metrics: bool, debug: bool) -> None:
    arguments: list[str] = []
    if disable_metrics:
        arguments.append('--disable-metrics')
    if time_metrics:
        arguments.append('--time-metrics')
    if debug:
        arguments.append('--debug')
    assert_success(launch('tests.driver.scenarios_persistence.accumulated_stats',
                          tmp_path, arguments))


def test_preservation(tmp_path: Path) -> None:
    assert_success(launch('tests.driver.scenarios_persistence.preservation', tmp_path))


class WriteStateLoss(Exception):
    pass


@pytest.mark.xfail(strict=True, raises=WriteStateLoss,
                   reason='save_stats removes in-memory records before a failed write')
def test_failed_save_retains_in_memory_records(tmp_path: Path) -> None:
    result = launch('tests.driver.scenarios_persistence.failed_write', tmp_path)
    if result.returncode == 73:
        assert result.stdout.strip() == 'CONFIRMED_PERSISTENCE_DEFECT: records lost after failed write'
        assert result.stderr == ''
        raise WriteStateLoss('faults/escalations/time_metrics disappeared')
    assert_success(result)