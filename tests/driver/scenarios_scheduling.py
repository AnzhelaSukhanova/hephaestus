"""Deterministic scheduling checks, imported only after the runner sets argv."""
from collections.abc import Callable, Mapping
from contextlib import ExitStack, redirect_stdout
from dataclasses import dataclass
from io import StringIO
import json
import multiprocessing as mp
from pathlib import Path
from queue import Queue
import shutil
import time
from unittest.mock import patch

import pytest

from src import utils
from src.compilers.kotlin import KotlinSettings
from src.compilers.results import ProcessResult
from src.generators.config import cfg
from src.generators.generator import EscalationLog
from src.modules import scheduler as scheduling
from src.modules.models import OracleResult, ProgramRes
from src.modules.scheduler import Scheduler
from src.modules.worker import WorkerInputs, get_worker_resources
from tests.driver import contracts
from tests.driver.fixtures import replay
from tests.driver.support import assert_arguments, load_driver


VERSION = 'scheduling compiler 1.0'


class Clock:
    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.calls = 0

    def __call__(self) -> float:
        assert self.calls < len(self.values), 'unexpected clock read'
        value = self.values[self.calls]
        self.calls += 1
        return value

    def finished(self) -> None:
        assert self.calls == len(self.values)


class Ticks:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.calls / 4


class SchedulingFailure(RuntimeError):
    pass


@dataclass
class Task:
    label: str
    run: Callable[[], object]
    callback: Callable[[object], object]
    error_callback: Callable[[BaseException], object]


class Completions[T](Queue[T]):
    """The parent's completion queue; blocking on it lets the pool make progress."""
    def __init__(self, pool: 'Pool') -> None:
        super().__init__()
        self.pool = pool

    def get(self, block: bool = True, timeout: float | None = None) -> T:
        if block:
            self.pool.events.append('wait')
            if self.pool.failure == 'wait':
                raise KeyboardInterrupt
            while self.empty():
                assert self.pool.pending, 'scheduler waits without submitted work'
                self.pool.step()
        return super().get(block, timeout)


class Pool:
    """Run submitted tasks in-process, most recent first, while the parent waits."""
    def __init__(self, failure: str = '',
                 prepare: Callable[[Scheduler[KotlinSettings]], None] | None = None) -> None:
        self.failure = failure
        self.prepare = prepare
        self.events: list[str] = []
        self.initialized: list[str] = []
        self.pending: list[Task] = []
        self.generated: list[int] = []
        self.completed: list[tuple[int, ...]] = []
        self.directories: list[Path] = []
        self.worker: Scheduler[KotlinSettings] | None = None
        self.created = False
        self.closed = False
        self.terminated = False
        self.joins = 0

    def factory(self, processes: int | None, initializer: Callable[[WorkerInputs[KotlinSettings]], object],
                initargs: tuple[WorkerInputs[KotlinSettings]]) -> 'Pool':
        driver = load_driver()
        assert not self.created
        assert processes == 2
        assert initializer is scheduling.initialize_scheduler_worker
        inputs, = initargs
        assert isinstance(inputs, WorkerInputs)
        assert inputs.compiler_version == VERSION
        assert inputs.options is not driver.options
        assert vars(inputs.options) == vars(driver.options)
        assert inputs.compiler_cls is driver.scheduler.compiler_cls
        self.created = True
        self.events.append('create')
        for _ in range(processes):
            initializer(*initargs)
            self.initialized.append(inputs.compiler_version)
        worker = get_worker_resources().scheduler
        assert isinstance(worker, Scheduler) and worker is not driver.scheduler
        assert worker.generation is not driver.generation
        assert worker.evaluation.compilation is not driver.compilation
        self.worker = worker
        if self.prepare is not None:
            self.prepare(worker)
        return self

    def queue(self) -> Completions[object]:
        return Completions(self)

    def apply_async(self, function: Callable[..., object], args: tuple[object, ...],
                    callback: Callable[[object], object],
                    error_callback: Callable[[BaseException], object]) -> None:
        assert not self.closed and not self.terminated
        if function is scheduling.gen_program_mul:
            pid, directory = args
            assert isinstance(pid, int) and isinstance(directory, str)
            assert Path(directory).name == 'src'
            if Path(directory).parent not in self.directories:
                self.directories.append(Path(directory).parent)
            self.events.append(f'submit:{pid}')
            if self.failure == 'generation_submit':
                raise KeyboardInterrupt

            def generate() -> object:
                self.generated.append(pid)
                return function(*args)

            self.pending.append(Task(f'generate:{pid}', generate, callback, error_callback))
            return
        assert function is scheduling.check_oracle_mul
        directory, oracles = args
        assert isinstance(directory, str)
        pids: list[int] = []
        for pid in contracts.record(oracles):
            assert isinstance(pid, int)
            pids.append(pid)
        label = ','.join(map(str, pids))
        self.events.append(f'oracle_submit:{label}')
        if self.failure == 'oracle_submit':
            raise KeyboardInterrupt

        def check() -> object:
            self.completed.append(tuple(pids))
            return function(*args)

        self.pending.append(Task(f'oracle:{label}', check, callback, error_callback))

    def step(self) -> None:
        task = self.pending.pop()
        self.events.append(task.label)
        try:
            result = task.run()
        except Exception as error:
            task.error_callback(error)
            return
        task.callback(result)

    def close(self) -> None:
        self.events.append('close')
        assert not self.closed
        if self.failure in ('close', 'terminate', 'cleanup_join'):
            raise KeyboardInterrupt
        self.closed = True

    def join(self) -> None:
        self.events.append('join')
        self.joins += 1
        if self.failure == 'join' and self.joins == 1:
            raise KeyboardInterrupt
        if self.failure == 'cleanup_join':
            raise SchedulingFailure('join after termination')
        assert self.closed or self.terminated
        if not self.terminated:
            assert not self.pending, 'closed pool joined with unfinished work'
        self.events.append('joined')

    def terminate(self) -> None:
        self.events.append('terminate')
        self.terminated = True
        self.pending.clear()
        if self.failure == 'terminate':
            raise SchedulingFailure('termination failed')


def _pool(pool: Pool) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch.object(mp, 'Pool', pool.factory))
    stack.enter_context(patch.object(scheduling, 'Queue', pool.queue))
    return stack


def _prepare() -> None:
    driver = load_driver()
    assert driver.options.language == 'kotlin'
    assert driver.options.only_correctness_preserving_transformations
    assert cfg.prob.crossmodule_probability == 0
    source = Path.cwd() / 'replay.bin'
    replay(source)
    driver.options.replay = str(source)
    utils.random.r.seed(41)


def _empty_totals() -> None:
    assert load_driver().reporting.stats['totals'] == {
        'passed': 0, 'failed': 0, 'inconclusive': 0}


def boundaries() -> None:
    driver = load_driver()
    scheduler = driver.scheduler
    args = driver.options
    assert scheduler.options is args
    args.iterations = 5
    args.seconds = None
    args.batch = 2
    args.stop_cond = 'iterations'
    assert [scheduler.stop_condition(i, 100) for i in (1, 4, 5, 6)] == [True, True, True, False]
    assert [scheduler.get_batches(i) for i in (0, 2, 4, 5)] == [2, 2, 1, 0]
    args.seconds = 3
    args.stop_cond = 'timeout'
    assert [scheduler.stop_condition(99, t) for t in (0, 2.999, 3, 3.001)] == [
        True, True, False, False]
    assert [scheduler.get_batches(i) for i in (0, 4, 99)] == [2, 2, 2]
    args.seconds = None
    args.iterations = None
    assert scheduler.stop_condition(100000, 100000)
    args.seconds = args.iterations = 0
    assert scheduler.stop_condition(100000, 100000)
    scheduler.stopped = True
    assert not scheduler.stop_condition(1, 0)
    args.seconds = 10
    args.iterations = 5
    assert not scheduler.stop_condition(1, 0)


def _loop(mode: str) -> None:
    driver = load_driver()
    scheduler = driver.scheduler
    args = driver.options
    args.iterations = 5
    args.batch = 2
    args.seconds = None
    args.stop_cond = 'iterations'
    expected = [[1, 2], [3, 4], [5]]
    clock = Clock([10, 10.5, 11, 11.5])
    if mode in ('timeout', 'overshoot', 'stop', 'unbounded'):
        args.stop_cond = 'timeout'
        args.iterations = None
        args.seconds = None if mode == 'unbounded' else 1
        expected = [[1, 2], [3, 4]]
        clock = Clock([10, 10.5, 11 if mode == 'timeout' else 12])
        if mode in ('stop', 'unbounded'):
            expected = [[1, 2]]
            clock = Clock([10, 10.5])
    elif mode == 'preset_stop':
        scheduler.stopped = True
        expected = []
        clock = Clock([10])
    elif mode in ('generation_interrupt', 'result_interrupt', 'exception'):
        expected = []
        clock = Clock([10])
    generated: list[int] = []
    batches: list[list[int]] = []
    directories: list[Path] = []
    resets = 0

    def reset() -> None:
        nonlocal resets
        resets += 1

    def generate(pid: int, dirname: str) -> int:
        generated.append(pid)
        directory = Path(dirname)
        assert directory.name == 'src' and directory.parent.is_dir()
        if directory.parent not in directories:
            directories.append(directory.parent)
        if mode == 'generation_interrupt' and pid == 2:
            raise KeyboardInterrupt
        if mode == 'exception':
            raise SchedulingFailure('generation probe')
        if mode == 'stop' and pid == 1:
            scheduler.stopped = True
        return pid

    def consume(start: int, values: list[int], dirname: str, batch: int) -> None:
        assert values == list(range(start, start + batch))
        assert Path(dirname) == directories[-1]
        assert batch == len(values)
        if mode == 'result_interrupt':
            raise KeyboardInterrupt
        batches.append(values)
        shutil.rmtree(dirname)
        if mode == 'unbounded':
            scheduler.stopped = True

    with (patch.object(time, 'perf_counter', clock),
          patch.object(utils.random, 'reset_word_pool', reset)):
        if mode == 'exception':
            with pytest.raises(SchedulingFailure, match='^generation probe$'):
                scheduler._run(generate, consume, VERSION)
        else:
            scheduler._run(generate, consume, VERSION)
    assert batches == expected
    assert generated == ([1] if mode == 'exception' else [1, 2] if 'interrupt' in mode
                         else [pid for batch in expected for pid in batch])
    assert resets == len(directories)
    assert len(set(directories)) == len(directories)
    clock.finished()
    assert driver.reporting.stats['Info']['compiler'] == VERSION


def loop_iterations() -> None:
    _loop('iterations')


def loop_timeout() -> None:
    _loop('timeout')


def loop_overshoot() -> None:
    _loop('overshoot')


def loop_stop() -> None:
    _loop('stop')


def loop_unbounded() -> None:
    _loop('unbounded')


def loop_preset_stop() -> None:
    _loop('preset_stop')


def loop_generation_interrupt() -> None:
    _loop('generation_interrupt')


def loop_result_interrupt() -> None:
    _loop('result_interrupt')


def loop_exception() -> None:
    _loop('exception')


def _lifecycle_events(dry: bool) -> list[str]:
    if dry:
        return ['create', 'submit:1', 'submit:2', 'wait', 'generate:2',
                'submit:3', 'wait', 'generate:3', 'submit:4', 'wait', 'generate:4',
                'submit:5', 'wait', 'generate:5', 'wait', 'generate:1',
                'close', 'join', 'joined']
    return ['create', 'submit:1', 'submit:2', 'wait', 'generate:2',
            'submit:3', 'wait', 'generate:3', 'submit:4', 'wait', 'generate:4',
            'oracle_submit:3,4', 'wait', 'oracle:3,4',
            'submit:5', 'wait', 'generate:5', 'oracle_submit:5', 'wait', 'oracle:5',
            'wait', 'generate:1', 'oracle_submit:1,2', 'wait', 'oracle:1,2',
            'close', 'join', 'joined']


def _lifecycle(parallel: bool) -> list[Path]:
    driver = load_driver()
    _prepare()
    args = driver.options
    assert args.iterations == 5 and args.batch == 2
    assert driver.scheduler.evaluation.compilation is driver.compilation
    produced: dict[int, ProgramRes] = {}
    directories: list[Path] = []
    checked: list[tuple[int, ...]] = []
    updates: list[tuple[int, float, tuple[int, ...], tuple[int, ...],
                        dict[str, list[EscalationLog]]]] = []
    initialized: list[str] = []
    commands: list[list[str]] = []
    current: tuple[int, ...] = ()
    update = driver.reporting.update_stats
    initialize = driver.setup_crossmodule_manager
    stack = ExitStack()

    def command(arguments: list[str], get_stdout: bool = True,
                cwd: str | None = None) -> ProcessResult:
        assert_arguments(arguments)
        assert get_stdout and cwd is None
        commands.append(list(arguments))
        if '-version' in arguments:
            assert len(commands) == 1
            return ProcessResult(0, f'  {VERSION}\n')
        assert current
        if current == (3, 4):
            return ProcessResult(1, 'org.jetbrains.kotlin.CompilerException\n'
                                    'controlled batch crash')
        return ProcessResult(0, '')

    def instrument(scheduler: Scheduler[KotlinSettings]) -> None:
        generate = scheduler.generation.gen_program
        oracle = scheduler.evaluation.check_oracle

        def gen(pid: int, dirname: str) -> ProgramRes:
            assert pid not in produced
            result = generate(pid, dirname)
            contracts.program_result(result)
            assert not result.failed
            assert result.stats['time'] == 0.25
            if not args.disable_metrics and pid % 2:
                result.stats['escalations'] = [{'reason': f'pid:{pid}'}]
            produced[pid] = result
            directory = Path(dirname).parent
            if directory not in directories:
                directories.append(directory)
            return result

        def check(dirname: str, oracles: Mapping[int, ProgramRes]) -> OracleResult:
            nonlocal current
            current = tuple(oracles)
            checked.append(current)
            for pid, result in oracles.items():
                assert result is produced[pid]
                assert set(contracts.record(result.stats['programs'])) == {
                    str(Path(dirname) / 'src' / f'd{pid}' / 'program.kt')}
            result = oracle(dirname, oracles)
            contracts.oracle_result(result)
            assert set(result.faults) == ({3, 4} if current == (3, 4) else set())
            assert result.inconclusive == {}
            assert not Path(dirname).exists()
            return result

        stack.enter_context(patch.object(scheduler.generation, 'gen_program', gen))
        stack.enter_context(patch.object(scheduler.evaluation, 'check_oracle', check))
        stack.enter_context(patch.object(
            scheduler.evaluation.compilation, 'run_process', command))

    def account(result: OracleResult, batch: int, elapsed: float,
                escalations: Mapping[str, list[EscalationLog]] | None = None) -> None:
        contracts.oracle_result(result)
        updates.append((batch, elapsed, tuple(result.faults), tuple(result.inconclusive),
                        dict(escalations or {})))
        update(result, batch, elapsed, escalations)

    def initialize_manager(version: str) -> object:
        initialized.append(version)
        return initialize(version)

    pool = Pool(prepare=instrument)
    output = StringIO()
    with stack:
        instrument(driver.scheduler)
        stack.enter_context(_pool(pool))
        stack.enter_context(patch.object(driver.reporting, 'update_stats', account))
        stack.enter_context(patch.object(
            driver, 'setup_crossmodule_manager', initialize_manager))
        stack.enter_context(patch.object(time, 'process_time', Ticks()))
        stack.enter_context(patch.object(time, 'perf_counter', Ticks()))
        stack.enter_context(redirect_stdout(output))
        driver.start()
    dry = args.dry_run
    order = [(3, 4), (5,), (1, 2)] if parallel else [(1, 2), (3, 4), (5,)]
    expected_updates = [(len(pids), len(pids) / 4,
                         pids if pids == (3, 4) and not dry else (), (),
                         {str(pid): [{'reason': f'pid:{pid}'}] for pid in pids if pid % 2}
                         if not args.disable_metrics else {})
                        for pids in order]
    assert updates == expected_updates
    assert checked == ([] if dry else order)
    assert set(produced) == {1, 2, 3, 4, 5}
    assert len(commands) == (1 if dry else 4)
    assert sum('-version' in command for command in commands) == 1
    assert initialized == [VERSION]
    assert pool.created is parallel
    if parallel:
        assert pool.initialized == [VERSION, VERSION]
        assert pool.generated == [2, 3, 4, 5, 1]
        assert pool.completed == ([] if dry else order)
        assert pool.events == _lifecycle_events(dry)
        assert pool.directories == directories
        assert not pool.pending
        assert pool.closed and not pool.terminated
        assert pool.worker is not None and not pool.worker.stopped
    stats = driver.reporting.stats
    assert stats['totals'] == {'passed': 5 if dry else 3, 'failed': 0 if dry else 2,
                               'inconclusive': 0}
    assert stats['time'] == 1.25
    assert stats['compilation_time'] == (0 if dry else 0.75)
    assert set(stats['faults']) == (set() if dry else {3, 4})
    for pid in stats['faults']:
        assert 'controlled batch crash' in stats['faults'][pid]['error']
    assert stats['inconclusive'] == {}
    expected_escalations = {str(pid): [{'reason': f'pid:{pid}'}] for pid in (1, 3, 5)}
    assert stats['escalations'] == ({} if args.disable_metrics else expected_escalations)
    destination = Path(args.test_directory)
    assert not (destination / 'tmp').exists()
    saved = contracts.record(json.loads((destination / 'stats.json').read_text()))
    assert saved['totals'] == stats['totals']
    assert saved['time'] == 1.25
    assert saved['compilation_time'] == (0 if dry else 0.75)
    assert ('Total faults: 0' if dry else 'Total faults: 2') in output.getvalue()
    assert 'Total inconclusive: 0' in output.getvalue()
    assert not driver.scheduler.stopped
    assert len(directories) == 3
    assert all(not directory.exists() for directory in directories)
    return directories


def serial() -> None:
    _lifecycle(False)


def parallel() -> None:
    _lifecycle(True)


def wrappers() -> None:
    driver = load_driver()
    _prepare()
    scheduling.initialize_scheduler_worker(driver.scheduler.worker_inputs(VERSION))
    worker = get_worker_resources().scheduler
    assert isinstance(worker, Scheduler) and worker is not driver.scheduler
    source = Path.cwd() / 'wrapper-batch' / 'src'
    with patch.object(utils.random.r, 'seed') as seed:
        result = scheduling.gen_program_mul(7, str(source))
        seed.assert_called_once_with()
    assert result is not None
    contracts.program_result(result)
    assert not result.failed
    with patch.object(worker.evaluation.compilation, 'run_process',
                      return_value=ProcessResult(0, '')) as command:
        checked = scheduling.check_oracle_mul(str(source.parent), {7: result})
        assert command.call_count == 1
    contracts.oracle_result(checked)
    assert checked == OracleResult(compilation_time=checked.compilation_time)
    assert not worker.stopped
    assert not source.parent.exists()
    worker.stopped = True
    with (patch.object(worker.generation, 'gen_program') as gen,
          patch.object(worker.evaluation, 'check_oracle') as check,
          patch.object(utils.random.r, 'seed') as seed):
        assert scheduling.gen_program_mul(8, str(source)) is None
        assert scheduling.check_oracle_mul(str(source.parent), {7: result}) is None
        gen.assert_not_called()
        check.assert_not_called()
        seed.assert_not_called()
    worker.stopped = False
    failure = SchedulingFailure('seed failure')
    with patch.object(utils.random.r, 'seed', side_effect=failure):
        with pytest.raises(SchedulingFailure) as caught:
            scheduling.gen_program_mul(8, str(source))
    assert caught.value is failure and not worker.stopped
    with patch.object(utils.random, 'reset_word_pool', side_effect=KeyboardInterrupt):
        assert scheduling.gen_program_mul(8, str(source)) is None
    assert worker.stopped
    worker.stopped = False
    result = worker.generation.gen_program(8, str(source))
    assert not result.failed
    with patch.object(worker.evaluation.compilation, 'run_process',
                      side_effect=KeyboardInterrupt) as command:
        assert scheduling.check_oracle_mul(str(source.parent), {8: result}) is None
        assert command.call_count == 1
        assert worker.stopped
        assert scheduling.check_oracle_mul(str(source.parent), {8: result}) is None
        assert command.call_count == 1
    assert not driver.scheduler.stopped


def _shutdown(phase: str) -> None:
    driver = load_driver()
    _prepare()
    pool = Pool(phase)
    driver.options.workers = 2
    driver.scheduler.stopped = True
    temporary = Path(driver.options.test_directory) / 'tmp'
    temporary.mkdir(parents=True)
    (temporary / 'sentinel').write_text('remove me')
    failures = {'terminate': 'termination failed', 'cleanup_join': 'join after termination'}
    output = StringIO()
    with _pool(pool), redirect_stdout(output):
        if phase in failures:
            # Pool cleanup failures propagate; the run summary is not reached.
            with pytest.raises(SchedulingFailure, match=f'^{failures[phase]}$'):
                driver.scheduler.run_parallel(VERSION)
        else:
            driver.scheduler.run_parallel(VERSION)
    assert temporary.exists() is (phase in failures)
    if phase in failures:
        assert (temporary / 'sentinel').read_text() == 'remove me'
    assert ('Total faults: 0' in output.getvalue()) is (phase not in failures)
    assert pool.generated == pool.directories == []
    expected = {
        'close': ['create', 'close', 'terminate', 'join', 'joined'],
        'join': ['create', 'close', 'join', 'terminate', 'join', 'joined'],
        'terminate': ['create', 'close', 'terminate'],
        'cleanup_join': ['create', 'close', 'terminate', 'join'],
        '': ['create', 'close', 'join', 'joined'],
    }
    assert pool.events == expected[phase]
    assert driver.scheduler.stopped
    _empty_totals()


def shutdown_close() -> None:
    _shutdown('close')


def shutdown_join() -> None:
    _shutdown('join')


def shutdown_terminate_error() -> None:
    _shutdown('terminate')


def shutdown_join_error() -> None:
    _shutdown('cleanup_join')


def shutdown_stopped() -> None:
    _shutdown('')


def serial_interrupt() -> None:
    driver = load_driver()
    _prepare()
    temporary = Path(driver.options.test_directory) / 'tmp'
    temporary.mkdir(parents=True)
    with patch.object(driver.reporting, 'logging', side_effect=KeyboardInterrupt) as logging:
        driver.scheduler.run(VERSION)
        logging.assert_called_once_with(VERSION)
    assert not temporary.exists()
    assert not driver.scheduler.stopped
    _empty_totals()


def serial_stopped() -> None:
    driver = load_driver()
    driver.scheduler.stopped = True
    driver.scheduler.run(VERSION)
    assert not (Path(driver.options.test_directory) / 'tmp').exists()
    _empty_totals()


def parallel_stopped() -> None:
    driver = load_driver()
    driver.options.workers = 2
    driver.scheduler.stopped = True
    pool = Pool()
    with _pool(pool):
        driver.scheduler.run_parallel(VERSION)
    assert pool.events == ['create', 'close', 'join', 'joined']
    assert not (Path(driver.options.test_directory) / 'tmp').exists()
    _empty_totals()


def _serial_inner_interrupt(oracle: bool) -> None:
    driver = load_driver()
    _prepare()
    driver.setup_crossmodule_manager(VERSION)
    driver.options.iterations = 5
    driver.options.batch = 2
    generate = driver.generation.gen_program
    generated: list[int] = []

    def gen(pid: int, dirname: str) -> ProgramRes:
        generated.append(pid)
        result = generate(pid, dirname)
        assert not result.failed
        if not oracle and pid == 2:
            raise KeyboardInterrupt
        return result

    with (patch.object(driver.generation, 'gen_program', gen),
          patch.object(driver.compilation, 'run_process',
                       side_effect=KeyboardInterrupt) as command):
        driver.scheduler.run(VERSION)
        assert command.call_count == int(oracle)
    assert generated == [1, 2]
    assert not driver.scheduler.stopped
    _empty_totals()
    assert not (Path(driver.options.test_directory) / 'tmp').exists()


def serial_generation_interrupt() -> None:
    _serial_inner_interrupt(False)


def serial_oracle_interrupt() -> None:
    _serial_inner_interrupt(True)


def _parallel_interrupt(failure: str, events: list[str], generated: list[int],
                        seed: object = None) -> None:
    driver = load_driver()
    _prepare()
    driver.options.iterations = 5
    driver.options.workers = 2
    pool = Pool(failure)
    output = StringIO()
    with (_pool(pool), redirect_stdout(output),
          patch.object(utils.random.r, 'seed', side_effect=seed)):
        driver.scheduler.run_parallel(VERSION)
    assert driver.scheduler.stopped
    assert pool.events == ['create', *events, 'terminate', 'join', 'joined'], pool.events
    assert pool.generated == generated
    assert pool.completed == []
    assert not pool.pending and pool.terminated and not pool.closed
    assert pool.directories and all(not directory.exists() for directory in pool.directories)
    assert not (Path(driver.options.test_directory) / 'tmp').exists()
    assert 'Total faults: 0' in output.getvalue()
    _empty_totals()


def parallel_wait_interrupt() -> None:
    _parallel_interrupt('wait', ['submit:1', 'submit:2', 'wait'], [])


def oracle_submit_interrupt() -> None:
    assert load_driver().options.batch == 1
    _parallel_interrupt('oracle_submit', [
        'submit:1', 'submit:2', 'wait', 'generate:2', 'oracle_submit:2'], [2])


def generation_none_result() -> None:
    # An interrupted worker returns None; the parent stops instead of consuming it.
    _parallel_interrupt('', ['submit:1', 'submit:2', 'wait', 'generate:2'], [2],
                        KeyboardInterrupt)
    worker = get_worker_resources().scheduler
    assert worker is not None and worker.stopped


def generation_submit_interrupt() -> None:
    _parallel_interrupt('generation_submit', ['submit:1'], [], KeyboardInterrupt)


def dry_run_cleanup() -> None:
    driver = load_driver()
    assert driver.options.dry_run
    with redirect_stdout(StringIO()):
        directories = _lifecycle(driver.options.workers is not None)
    assert len(directories) == 3
    assert all(not directory.exists() for directory in directories)
