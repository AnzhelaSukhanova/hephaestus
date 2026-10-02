import json
import multiprocessing as mp
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.driver.fixtures import replay
from tests.driver.support import assert_success, launch


@pytest.mark.parametrize('method', [method for method in ('spawn', 'fork')
                                  if method in mp.get_all_start_methods()])
@pytest.mark.parametrize('mode', ['success', 'reject', 'crash'])
@pytest.mark.parametrize('dry_run', [False, True])
def test_worker_equivalence(method: str, mode: str, dry_run: bool,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('DRIVER_COMPILER_MODE', mode)
    with TemporaryDirectory(prefix='driver_workers_') as directory:
        root = Path(directory)
        source = root / 'input.bin'
        replay(source)
        arguments = ['--replay', str(source), '--iterations', '3', '--keep-everything',
                     '--disable-metrics']
        if dry_run:
            arguments.append('--dry-run')
        sequential = root / 'sequential'
        parallel = root / 'parallel'
        assert_success(launch('tests.driver.scenarios_workers.sequential', sequential, arguments))
        assert_success(launch('tests.driver.scenarios_workers.' + method, parallel,
                              [*arguments, '--workers', '2']))
        first = json.loads((sequential / 'snapshot.json').read_text())
        second = json.loads((parallel / 'snapshot.json').read_text())
        assert first == second
        failed = 0 if dry_run or mode == 'success' else 3
        assert first['totals'] == {'passed': 3 - failed, 'failed': failed}