from collections.abc import Callable
import json
import multiprocessing as mp
from multiprocessing.pool import Pool
import os
from pathlib import Path
import re
from unittest.mock import patch

from src import utils
from src.translators.kotlin import KotlinTranslator
from tests.driver import contracts
from tests.driver.fixtures import replay
from tests.driver.support import load_driver
from tests.driver.workers import initialize_worker, probe


def normalize(value: object, workspace: Path) -> object:
    if isinstance(value, str):
        value = re.sub(re.escape(str(workspace)) + r'/temporary/tmp[^/\s]+',
                       '<batch>', value)
        return value.replace(str(workspace), '<workspace>')
    if contracts.is_mapping(value):
        return {normalize(key, workspace): normalize(item, workspace)
                for key, item in value.items()
                if key not in ('time', 'compilation_time', 'generation', 'compilation', 'profiling')}
    if contracts.is_sequence(value):
        return [normalize(item, workspace) for item in value]
    return value


def snapshot() -> None:
    driver = load_driver()
    root = Path(driver.cli_args.test_directory)
    stats = contracts.record(json.loads((root / 'stats.json').read_text()))
    faults = contracts.record(json.loads((root / 'faults.json').read_text()))
    assert not (root / 'tmp').exists()
    sources: dict[str, str] = {}
    for pid in range(1, 4):
        directory = root / ('generator/iter_' + str(pid) if driver.cli_args.dry_run else str(pid))
        source = (directory / 'program.kt').read_text()
        restored = contracts.program(utils.load_program(str(directory / 'program.kt.bin')))
        translator = KotlinTranslator(f'src.d{pid}')
        translator.visit(restored)
        assert translator.result() == source
        sources[str(pid)] = source
    commands = [contracts.record(json.loads(line))['arguments']
                for line in Path('tool.jsonl').read_text().splitlines()]
    assert commands[0] == ['-version']
    assert len(commands) == (1 if driver.cli_args.dry_run else 4)
    result = {'totals': stats['totals'], 'faults': faults, 'sources': sources,
              'commands': commands, 'compiler': contracts.record(stats['Info'])['compiler']}
    normalized = normalize(result, Path.cwd())
    contracts.json_value(normalized)
    Path('snapshot.json').write_text(json.dumps(normalized, sort_keys=True))


def sequential() -> None:
    driver = load_driver()
    driver.main()
    snapshot()


def parallel(method: str) -> None:
    driver = load_driver()
    context = mp.get_context(method)

    def create_pool(processes: int, initializer: Callable[[str], object],
                    initargs: tuple[str]) -> Pool:
        assert initializer is driver.setup_crossmodule_manager
        assert initargs == ('hermetic compiler 1.0',)
        assert driver.CROSSMODULE_MANAGER_PID == os.getpid()
        return context.Pool(processes, initializer=initialize_worker, initargs=initargs)

    with patch.object(driver.mp, 'Pool', create_pool):
        driver.main()
    workers = list(Path.cwd().glob('worker_*.json'))
    assert len(workers) == 2
    for worker in workers:
        values = contracts.record(json.loads(worker.read_text()))
        assert type(values['pid']) is int and values['pid'] != os.getpid()
        assert values['version'] == 'hermetic compiler 1.0'
    snapshot()
    assert not mp.active_children()


def spawn() -> None:
    parallel('spawn')


def fork() -> None:
    parallel('fork')


def coverage_worker() -> None:
    driver = load_driver()
    source = Path('probe.bin').resolve()
    replay(source)
    # Spawn imports src.args afresh, so pass the same replay path through argv.
    import sys
    sys.argv += ['--replay', str(source)]
    driver.cli_args.replay = str(source)
    assert driver.get_generator_dir(987).endswith('/generator/iter_987')
    with mp.get_context('spawn').Pool(1, initializer=initialize_worker,
                                     initargs=('sentinel',)) as pool:
        pid, result = pool.apply_async(probe).get(timeout=15)
        pool.close()
        pool.join()
    assert type(pid) is int and pid != os.getpid()
    contracts.program_result(result)
    assert result.failed is False
    assert result.direct_dependency_pid is None
    assert result.dep_transitive_closure_klibs is None
    assert Path(driver.cli_args.test_directory, 'tmp/4321/escalations.json').read_text() == '[]'