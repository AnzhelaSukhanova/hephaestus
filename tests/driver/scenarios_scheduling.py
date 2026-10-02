"""Deterministic scheduling checks, imported only after the runner sets argv."""
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import traceback
from unittest.mock import patch

import pytest

import hephaestus as driver
from src.generators.generator import EscalationLog
from tests.driver import contracts
from tests.driver.fixtures import replay


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


class Result[T]:
    def __init__(self, function: Callable[[], T], drain: Callable[[], None]) -> None:
        self.function = function
        self.drain = drain
        self.values: list[T] = []
        self.gets = 0

    def resolve(self) -> None:
        assert not self.values, 'work executed twice'
        self.values.append(self.function())

    def get(self) -> T:
        self.gets += 1
        assert self.gets == 1, 'result consumed twice'
        self.drain()
        assert len(self.values) == 1
        return self.values[0]


class Pool:
    """Run generation groups and queued oracle callbacks in reverse order."""
    def __init__(self, failure: str = '') -> None:
        self.failure = failure
        self.events: list[str] = []
        self.initialized: list[str] = []
        self.generation: list[Callable[[], None]] = []
        self.callbacks: list[Callable[[], None]] = []
        self.generated: list[int] = []
        self.consumed: list[int] = []
        self.completed: list[tuple[int, ...]] = []
        self.created = False
        self.closed = False
        self.terminated = False
        self.joins = 0

    def factory(self, processes: int | None, initializer: Callable[[str], object],
                initargs: tuple[str]) -> 'Pool':
        assert not self.created
        assert processes == 2
        assert initializer is driver.setup_crossmodule_manager
        assert initargs == (VERSION,)
        self.created = True
        self.events.append('create')
        for _ in range(processes):
            initializer(*initargs)
            self.initialized.append(initargs[0])
        return self

    def drain(self) -> None:
        while self.generation:
            self.generation.pop()()

    def apply_async[T](self, function: Callable[..., T], args: tuple[object, ...],
                       callback: Callable[[T], object] | None = None) -> Result[T]:
        assert not self.closed and not self.terminated
        if callback is None:
            assert function is driver.gen_program_mul
            pid, directory = args
            assert isinstance(pid, int) and isinstance(directory, str)
            self.events.append(f'submit:{pid}')
            if self.failure == 'generation_submit':
                raise KeyboardInterrupt

            def generate() -> T:
                self.generated.append(pid)
                return function(*args)

            def consume() -> None:
                self.consumed.append(pid)
                self.drain()
                if self.failure == 'result_get':
                    raise KeyboardInterrupt

            result = Result(generate, consume)
            self.generation.append(result.resolve)
            return result
        assert function is driver.check_oracle_mul
        directory, oracles = args
        assert isinstance(directory, str)
        pids: list[int] = []
        for pid in contracts.record(oracles):
            assert isinstance(pid, int)
            pids.append(pid)
        self.events.append('oracle_submit')
        if self.failure == 'oracle_submit':
            raise KeyboardInterrupt
        result = Result(lambda: function(*args), self.drain)

        def complete() -> None:
            result.resolve()
            callback(result.get())
            self.completed.append(tuple(pids))

        self.callbacks.append(complete)
        return result

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
            while self.callbacks:
                self.callbacks.pop()()
        self.events.append('joined')

    def terminate(self) -> None:
        self.events.append('terminate')
        self.terminated = True
        self.callbacks.clear()
        self.generation.clear()
        if self.failure == 'terminate':
            raise SchedulingFailure('termination failed')


def _prepare() -> None:
    assert driver.cli_args.language == 'kotlin'
    assert driver.cli_args.only_correctness_preserving_transformations
    assert driver.cfg.prob.crossmodule_probability == 0
    source = Path.cwd() / 'replay.bin'
    replay(source)
    driver.cli_args.replay = str(source)
    driver.utils.random.r.seed(41)


def boundaries() -> None:
    args = driver.cli_args
    args.iterations = 5
    args.seconds = None
    args.batch = 2
    args.stop_cond = 'iterations'
    assert [driver.stop_condition(i, 100) for i in (1, 4, 5, 6)] == [True, True, True, False]
    assert [driver.get_batches(i) for i in (0, 2, 4, 5)] == [2, 2, 1, 0]
    args.seconds = 3
    args.stop_cond = 'timeout'
    assert [driver.stop_condition(99, t) for t in (0, 2.999, 3, 3.001)] == [True, True, False, False]
    assert [driver.get_batches(i) for i in (0, 4, 99)] == [2, 2, 2]
    args.seconds = None
    args.iterations = None
    assert driver.stop_condition(100000, 100000)
    args.seconds = args.iterations = 0
    assert driver.stop_condition(100000, 100000)
    driver.STOP_COND = True
    assert not driver.stop_condition(1, 0)
    args.seconds = 10
    args.iterations = 5
    assert not driver.stop_condition(1, 0)


def _loop(mode: str) -> None:
    args = driver.cli_args
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
        driver.STOP_COND = True
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
            driver.STOP_COND = True
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
            driver.STOP_COND = True

    with (patch.object(driver.time, 'perf_counter', clock),
          patch.object(driver.utils.random, 'reset_word_pool', reset)):
        if mode == 'exception':
            with pytest.raises(SchedulingFailure, match='^generation probe$'):
                driver._run(generate, consume, VERSION)
        else:
            driver._run(generate, consume, VERSION)
    assert batches == expected
    assert generated == ([1] if mode == 'exception' else [1, 2] if 'interrupt' in mode
                         else [pid for batch in expected for pid in batch])
    assert resets == len(directories)
    assert len(set(directories)) == len(directories)
    clock.finished()
    assert driver.STATS['Info']['compiler'] == VERSION


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


def _lifecycle(parallel: bool) -> list[Path]:
    _prepare()
    args = driver.cli_args
    assert args.iterations == 5 and args.batch == 2
    pool = Pool()
    produced: dict[int, driver.ProgramRes] = {}
    directories: list[Path] = []
    checked: list[tuple[int, ...]] = []
    updates: list[tuple[int, float, tuple[int, ...], dict[str, list[EscalationLog]]]] = []
    initialized: list[str] = []
    commands: list[list[str]] = []
    current: tuple[int, ...] = ()
    generate = driver.gen_program
    oracle = driver.check_oracle
    update = driver.update_stats
    initialize = driver.setup_crossmodule_manager

    def gen(pid: int, dirname: str) -> driver.ProgramRes:
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

    def check(dirname: str, oracles: Mapping[int, driver.ProgramRes]) -> driver.OracleResult:
        nonlocal current
        current = tuple(oracles)
        checked.append(current)
        for pid, result in oracles.items():
            assert result is produced[pid]
            assert set(contracts.record(result.stats['programs'])) == {
                str(Path(dirname) / 'src' / f'd{pid}' / 'program.kt')}
        result = oracle(dirname, oracles)
        contracts.oracle_result(result)
        assert set(result[0]) == ({3, 4} if current == (3, 4) else set())
        assert not Path(dirname).exists()
        return result

    def account(result: driver.OracleResult, batch: int, elapsed: float,
                escalations: Mapping[str, list[EscalationLog]] | None = None) -> None:
        contracts.oracle_result(result)
        updates.append((batch, elapsed, tuple(result[0]), dict(escalations or {})))
        update(result, batch, elapsed, escalations)

    def initialize_worker(version: str) -> object:
        initialized.append(version)
        return initialize(version)

    def command(arguments: list[str], get_stdout: bool = True,
                cwd: str | None = None) -> tuple[bool, str]:
        contracts_args = list(arguments)
        assert contracts_args and get_stdout and cwd is None
        commands.append(contracts_args)
        if '-version' in arguments:
            assert len(commands) == 1
            return True, f'  {VERSION}\n'
        assert current
        if current == (3, 4):
            return False, 'org.jetbrains.kotlin.CompilerException\ncontrolled batch crash'
        return True, ''

    output = StringIO()
    with (patch.object(driver.mp, 'Pool', pool.factory),
          patch.object(driver, 'gen_program', gen),
          patch.object(driver, 'check_oracle', check),
          patch.object(driver, 'update_stats', account),
          patch.object(driver, 'setup_crossmodule_manager', initialize_worker),
          patch.object(driver, 'run_command', command),
          patch.object(driver.time, 'process_time', Ticks()),
          patch.object(driver.time, 'perf_counter', Ticks()),
          redirect_stdout(output)):
        driver.main()
    dry = args.dry_run
    order = [(5,), (3, 4), (1, 2)] if parallel else [(1, 2), (3, 4), (5,)]
    expected_updates = [(len(pids), len(pids) / 4,
                         pids if pids == (3, 4) and not dry else (),
                         {str(pid): [{'reason': f'pid:{pid}'}] for pid in pids if pid % 2}
                         if not args.disable_metrics else {})
                        for pids in ([(1, 2), (3, 4), (5,)] if dry else order)]
    assert updates == expected_updates
    assert checked == ([] if dry else order)
    assert set(produced) == {1, 2, 3, 4, 5}
    assert len(commands) == (1 if dry else 4)
    assert sum('-version' in command for command in commands) == 1
    assert initialized == [VERSION] * (3 if parallel else 1)
    assert pool.created is parallel
    if parallel:
        assert pool.initialized == [VERSION, VERSION]
        assert pool.generated == [2, 1, 4, 3, 5]
        assert pool.consumed == [1, 2, 3, 4, 5]
        assert pool.completed == ([] if dry else order)
        assert pool.events[-3:] == ['close', 'join', 'joined']
        assert not pool.callbacks and not pool.generation
        assert not pool.terminated
    assert driver.STATS['totals'] == {'passed': 5 if dry else 3, 'failed': 0 if dry else 2}
    assert driver.STATS['time'] == 1.25
    assert driver.STATS['compilation_time'] == (0 if dry else 0.75)
    assert set(driver.STATS['faults']) == (set() if dry else {3, 4})
    expected_escalations = {str(pid): [{'reason': f'pid:{pid}'}] for pid in (1, 3, 5)}
    assert driver.STATS['escalations'] == ({} if args.disable_metrics else expected_escalations)
    destination = Path(args.test_directory)
    assert not (destination / 'tmp').exists()
    saved = contracts.record(json.loads((destination / 'stats.json').read_text()))
    assert saved['totals'] == driver.STATS['totals']
    assert saved['time'] == 1.25
    assert saved['compilation_time'] == (0 if dry else 0.75)
    assert ('Total faults: 0' if dry else 'Total faults: 2') in output.getvalue()
    assert not driver.STOP_COND
    assert len(directories) == 3
    if not dry:
        assert all(not directory.exists() for directory in directories)
    return directories


def serial() -> None:
    _lifecycle(False)


def parallel() -> None:
    _lifecycle(True)


def wrappers() -> None:
    _prepare()
    driver.setup_crossmodule_manager(VERSION)
    source = Path.cwd() / 'wrapper-batch' / 'src'
    with patch.object(driver.utils.random.r, 'seed') as seed:
        result = driver.gen_program_mul(7, str(source))
        seed.assert_called_once_with()
    assert result is not None
    contracts.program_result(result)
    assert not result.failed
    with patch.object(driver, 'run_command', return_value=(True, '')) as command:
        checked = driver.check_oracle_mul(str(source.parent), {7: result})
        assert command.call_count == 1
    contracts.oracle_result(checked)
    assert checked[0] == {} and not driver.STOP_COND
    assert not source.parent.exists()
    driver.STOP_COND = True
    with (patch.object(driver, 'gen_program') as gen,
          patch.object(driver, 'check_oracle') as check,
          patch.object(driver.utils.random.r, 'seed') as seed):
        assert driver.gen_program_mul(8, str(source)) is None
        assert driver.check_oracle_mul(str(source.parent), {7: result}) == ({}, 0, {}, {})
        gen.assert_not_called()
        check.assert_not_called()
        seed.assert_not_called()
    driver.STOP_COND = False
    failure = SchedulingFailure('seed failure')
    with patch.object(driver.utils.random.r, 'seed', side_effect=failure):
        with pytest.raises(SchedulingFailure) as caught:
            driver.gen_program_mul(8, str(source))
    assert caught.value is failure and not driver.STOP_COND
    with patch.object(driver.utils.random, 'reset_word_pool', side_effect=KeyboardInterrupt):
        assert driver.gen_program_mul(8, str(source)) is None
    assert driver.STOP_COND
    driver.STOP_COND = False
    result = driver.gen_program(8, str(source))
    assert not result.failed
    with patch.object(driver, 'run_command', side_effect=KeyboardInterrupt) as command:
        assert driver.check_oracle_mul(str(source.parent), {8: result}) == ({}, 0, {}, {})
        assert command.call_count == 1
        assert driver.STOP_COND
        assert driver.check_oracle_mul(str(source.parent), {8: result}) == ({}, 0, {}, {})
        assert command.call_count == 1


def _shutdown(phase: str) -> None:
    _prepare()
    pool = Pool(phase)
    driver.cli_args.workers = 2
    driver.STOP_COND = True
    temporary = Path(driver.cli_args.test_directory) / 'tmp'
    temporary.mkdir(parents=True)
    (temporary / 'sentinel').write_text('remove me')
    with patch.object(driver.mp, 'Pool', pool.factory):
        driver.run_parallel(VERSION)
    assert not temporary.exists()
    assert pool.generated == pool.consumed == []
    expected = {
        'close': ['create', 'close', 'terminate', 'join', 'joined'],
        'join': ['create', 'close', 'join', 'terminate', 'join', 'joined'],
        'terminate': ['create', 'close', 'terminate'],
        'cleanup_join': ['create', 'close', 'terminate', 'join'],
        '': ['create', 'close', 'join', 'joined'],
    }
    assert pool.events == expected[phase]
    assert driver.STATS['totals'] == {'passed': 0, 'failed': 0}


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
    _prepare()
    temporary = Path(driver.cli_args.test_directory) / 'tmp'
    temporary.mkdir(parents=True)
    with patch.object(driver, 'logging', side_effect=KeyboardInterrupt):
        driver.run(VERSION)
    assert not temporary.exists()
    assert driver.STATS['totals'] == {'passed': 0, 'failed': 0}


def serial_stopped() -> None:
    driver.STOP_COND = True
    driver.run(VERSION)
    assert not (Path(driver.cli_args.test_directory) / 'tmp').exists()
    assert driver.STATS['totals'] == {'passed': 0, 'failed': 0}


def parallel_stopped() -> None:
    driver.cli_args.workers = 2
    driver.STOP_COND = True
    pool = Pool()
    with patch.object(driver.mp, 'Pool', pool.factory):
        driver.run_parallel(VERSION)
    assert pool.events == ['create', 'close', 'join', 'joined']
    assert not (Path(driver.cli_args.test_directory) / 'tmp').exists()
    assert driver.STATS['totals'] == {'passed': 0, 'failed': 0}


def _serial_inner_interrupt(oracle: bool) -> None:
    _prepare()
    driver.setup_crossmodule_manager(VERSION)
    driver.cli_args.iterations = 5
    driver.cli_args.batch = 2
    generate = driver.gen_program
    generated: list[int] = []

    def gen(pid: int, dirname: str) -> driver.ProgramRes:
        generated.append(pid)
        result = generate(pid, dirname)
        assert not result.failed
        if not oracle and pid == 2:
            raise KeyboardInterrupt
        return result

    with (patch.object(driver, 'gen_program', gen),
          patch.object(driver, 'run_command', side_effect=KeyboardInterrupt) as command):
        driver.run(VERSION)
        assert command.call_count == int(oracle)
    assert generated == [1, 2]
    assert not driver.STOP_COND
    assert driver.STATS['totals'] == {'passed': 0, 'failed': 0}
    assert not (Path(driver.cli_args.test_directory) / 'tmp').exists()


def serial_generation_interrupt() -> None:
    _serial_inner_interrupt(False)


def serial_oracle_interrupt() -> None:
    _serial_inner_interrupt(True)


def parallel_get_interrupt() -> None:
    _prepare()
    driver.cli_args.iterations = 5
    driver.cli_args.workers = 2
    pool = Pool('result_get')
    with patch.object(driver.mp, 'Pool', pool.factory):
        driver.run_parallel(VERSION)
    assert not driver.STOP_COND
    assert pool.events == ['create', 'submit:1', 'close', 'join', 'joined']
    assert pool.generated == pool.consumed == [1]
    assert not pool.callbacks and not pool.generation
    assert driver.STATS['totals'] == {'passed': 0, 'failed': 0}
    assert not (Path(driver.cli_args.test_directory) / 'tmp').exists()


def oracle_submit_interrupt() -> None:
    _prepare()
    driver.cli_args.iterations = 5
    driver.cli_args.workers = 2
    pool = Pool('oracle_submit')
    with patch.object(driver.mp, 'Pool', pool.factory):
        driver.run_parallel(VERSION)
    assert driver.STOP_COND
    assert pool.generated == pool.consumed == [1]
    assert pool.completed == []
    assert pool.events == ['create', 'submit:1', 'oracle_submit', 'close', 'join', 'joined']
    assert not (Path(driver.cli_args.test_directory) / 'tmp').exists()
    assert driver.STATS['totals'] == {'passed': 0, 'failed': 0}


def _confirm(message: str) -> None:
    print(f'CONFIRMED_SCHEDULING_DEFECT: {message}')
    raise SystemExit(74)


def _none_result(submission: bool) -> None:
    _prepare()
    driver.cli_args.workers = 2
    pool = Pool('generation_submit' if submission else '')
    output = StringIO()
    with (patch.object(driver.mp, 'Pool', pool.factory),
          patch.object(driver.utils.random.r, 'seed', side_effect=KeyboardInterrupt),
          redirect_stdout(output)):
        try:
            driver.run_parallel(VERSION)
        except AttributeError as error:
            attribute = 'get' if submission else 'stats'
            assert str(error) == f"'NoneType' object has no attribute '{attribute}'"
            frames = traceback.extract_tb(error.__traceback__)
            assert Path(frames[-1].filename).name == 'hephaestus.py'
            assert frames[-1].name == ('process_res' if submission else '<lambda>')
            assert any(frame.name == '_run' for frame in frames)
            assert driver.STOP_COND
            assert pool.generated == ([] if submission else [1])
            assert pool.consumed == ([] if submission else [1])
            assert pool.events == ['create', 'submit:1']
            assert driver.STATS['totals'] == {'passed': 0, 'failed': 0}
        else:
            assert driver.STOP_COND
            assert not (Path(driver.cli_args.test_directory) / 'tmp').exists()
            assert pool.closed or pool.terminated
            return
    _confirm('missing generation handle consumed' if submission else 'None generation result consumed')


def none_worker_result() -> None:
    _none_result(False)


def none_submission_handle() -> None:
    _none_result(True)


def dry_run_cleanup() -> None:
    assert driver.cli_args.dry_run
    with redirect_stdout(StringIO()):
        directories = _lifecycle(driver.cli_args.workers is not None)
    remaining = [directory for directory in directories if directory.exists()]
    if remaining:
        assert len(remaining) == 3
        assert all((directory / 'src').is_dir() for directory in remaining)
        _confirm('dry-run batch directories leaked')
    assert all(not directory.exists() for directory in directories)