from copy import deepcopy
import json
from pathlib import Path
from typing import assert_type

from src.generators.generator import EscalationLog
from src.modules.models import OracleResult, ProgramStats, ProgramTimeMetrics
from tests.driver.contracts import json_value, record
from tests.driver.support import load_driver


def _timing(compilation: float) -> ProgramTimeMetrics:
    return {'generation_cpu': 0.125, 'compilation_with_ir_dumps': compilation,
            'phase_profiling': 0.0}


def accumulated_stats() -> None:
    driver = load_driver()
    reporting = driver.reporting
    root = Path(driver.options.test_directory)
    escalations: list[EscalationLog] = [{'reason': 'scope', 'chain': ('a', 'b')}]
    fault: ProgramStats = {'error': 'controlled failure', 'time': 0,
                           'transformations': [], 'programs': {}}
    unresolved: ProgramStats = {'error': None, 'time': 0, 'transformations': [],
                                'programs': {}, 'oracle': 'compilation',
                                'inconclusive_reason': 'evaluation_error',
                                'inconclusive_details': 'controlled'}
    result = OracleResult({2: fault}, 1.25, {2: _timing(1.25)})
    assert_type(result, OracleResult)
    reporting.update_stats(result, 2, 0.75, {'2': escalations})
    reporting.update_stats(OracleResult({}, 0.5, {3: _timing(0.5)},
                                        inconclusive={4: unresolved}), 2, 0.25)
    before = deepcopy(reporting.stats)
    reporting.save_stats()
    assert reporting.stats == before
    stats = json.loads((root / 'stats.json').read_text())
    json_value(stats)
    assert record(stats)['totals'] == {'passed': 2, 'failed': 1, 'inconclusive': 1}
    assert record(stats)['time'] == 1.0
    assert record(stats)['compilation_time'] == 1.75
    assert not {'faults', 'inconclusive', 'escalations', 'time_metrics'} & record(stats).keys()
    assert json.loads((root / 'faults.json').read_text()) == {'2': fault}
    assert json.loads((root / 'inconclusive.json').read_text()) == {'4': unresolved}
    assert reporting.stats['faults'] == {2: fault}
    assert reporting.stats['inconclusive'] == {4: unresolved}
    assert reporting.stats['time_metrics'] == {2: _timing(1.25), 3: _timing(0.5)}
    if driver.options.disable_metrics:
        assert not (root / 'escalations.json').exists()
        assert reporting.stats['escalations'] == {}
    else:
        assert json.loads((root / 'escalations.json').read_text()) == {
            '2': [{'reason': 'scope', 'chain': ['a', 'b']}]}
        assert reporting.stats['escalations'] == {'2': escalations}
    if driver.options.time_metrics:
        assert json.loads((root / 'time_metrics.json').read_text()) == {
            '2': _timing(1.25), '3': _timing(0.5)}
    else:
        assert not (root / 'time_metrics.json').exists()


def preservation() -> None:
    driver = load_driver()
    artifacts = driver.artifacts
    root = Path(driver.options.test_directory)
    artifacts.preserve_final_program_dir(1)
    assert not (root / '1').exists()
    source = root / 'tmp/1'
    source.mkdir(parents=True)
    (source / 'program.kt').write_text('first')
    artifacts.preserve_final_program_dir(1)
    (source / 'program.kt').write_text('second')
    (source / 'trace.txt').write_text('diagnostic')
    artifacts.preserve_final_program_dir(1)
    assert (root / '1/program.kt').read_text() == 'second'
    assert (root / '1/trace.txt').read_text() == 'diagnostic'
    artifacts._cleanup_program_tmp(1)
    artifacts._cleanup_program_tmp(1)
    assert not source.exists()
    assert (root / '1/trace.txt').read_text() == 'diagnostic'


def failed_write() -> None:
    driver = load_driver()
    reporting = driver.reporting
    root = Path(driver.options.test_directory)
    # This is a genuine failed filesystem write, not a replaced save_stats.
    (root / 'faults.json').mkdir(parents=True)
    before = deepcopy(reporting.stats)
    try:
        reporting.save_stats()
    except IsADirectoryError as error:
        assert error.filename == str(root / 'faults.json')
    else:
        raise AssertionError('Expected the filesystem write to fail')
    if reporting.stats == before:
        return
    assert reporting.stats == {
        key: value for key, value in before.items()
        if key not in {'faults', 'inconclusive', 'escalations', 'time_metrics'}}
    print('CONFIRMED_PERSISTENCE_DEFECT: records lost after failed write')
    raise SystemExit(73)
