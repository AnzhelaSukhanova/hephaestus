from copy import deepcopy
import json
from pathlib import Path
from typing import assert_type

from src.generators.generator import EscalationLog
from tests.driver.contracts import json_value, record
from tests.driver.support import load_driver


def accumulated_stats() -> None:
    from hephaestus import OracleResult, ProgramStats
    driver = load_driver()
    root = Path(driver.cli_args.test_directory)
    escalations: list[EscalationLog] = [{'reason': 'scope', 'chain': ('a', 'b')}]
    fault: ProgramStats = {'error': 'controlled failure', 'time': 0,
                                  'transformations': [], 'programs': {}}
    result: OracleResult = ({2: fault}, 1.25, {2: {'compilation': 1.25}}, {})
    assert_type(result, OracleResult)
    driver.update_stats(result, 2, 0.75, {'2': escalations})
    driver.update_stats(({}, 0.5, {3: {'compilation': 0.5}}, {}), 1, 0.25)
    before = deepcopy(driver.STATS)
    driver.save_stats()
    assert driver.STATS == before
    stats = json.loads((root / 'stats.json').read_text())
    json_value(stats)
    assert record(stats)['totals'] == {'passed': 2, 'failed': 1}
    assert record(stats)['time'] == 1.0
    assert record(stats)['compilation_time'] == 1.75
    assert not {'faults', 'escalations', 'time_metrics'} & record(stats).keys()
    assert json.loads((root / 'faults.json').read_text()) == {'2': fault}
    assert driver.STATS['faults'] == {2: fault}
    assert driver.STATS['time_metrics'] == {2: {'compilation': 1.25}, 3: {'compilation': 0.5}}
    if driver.cli_args.disable_metrics:
        assert not (root / 'escalations.json').exists()
        assert driver.STATS['escalations'] == {}
    else:
        assert json.loads((root / 'escalations.json').read_text()) == {
            '2': [{'reason': 'scope', 'chain': ['a', 'b']}]}
        assert driver.STATS['escalations'] == {'2': escalations}
    if driver.cli_args.time_metrics:
        assert json.loads((root / 'time_metrics.json').read_text()) == {
            '2': {'compilation': 1.25}, '3': {'compilation': 0.5}}
    else:
        assert not (root / 'time_metrics.json').exists()


def preservation() -> None:
    driver = load_driver()
    root = Path(driver.cli_args.test_directory)
    driver.preserve_final_program_dir(1)
    assert not (root / '1').exists()
    source = root / 'tmp/1'
    source.mkdir(parents=True)
    (source / 'program.kt').write_text('first')
    driver.preserve_final_program_dir(1)
    (source / 'program.kt').write_text('second')
    (source / 'trace.txt').write_text('diagnostic')
    driver.preserve_final_program_dir(1)
    assert (root / '1/program.kt').read_text() == 'second'
    assert (root / '1/trace.txt').read_text() == 'diagnostic'
    driver._cleanup_program_tmp(1)
    driver._cleanup_program_tmp(1)
    assert not source.exists()
    assert (root / '1/trace.txt').read_text() == 'diagnostic'


def failed_write() -> None:
    driver = load_driver()
    root = Path(driver.cli_args.test_directory)
    # This is a genuine failed filesystem write, not a replaced save_stats.
    (root / 'faults.json').mkdir(parents=True)
    before = deepcopy(driver.STATS)
    try:
        driver.save_stats()
    except IsADirectoryError as error:
        assert error.filename == str(root / 'faults.json')
    else:
        raise AssertionError('Expected the filesystem write to fail')
    if driver.STATS == before:
        return
    assert driver.STATS == {key: value for key, value in before.items()
                            if key not in {'faults', 'escalations', 'time_metrics'}}
    print('CONFIRMED_PERSISTENCE_DEFECT: records lost after failed write')
    raise SystemExit(73)