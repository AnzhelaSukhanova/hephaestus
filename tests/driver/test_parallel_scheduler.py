"""Completion-driven scheduling, with controlled completion order and real pools."""
from collections.abc import Callable
from dataclasses import dataclass
import json
import multiprocessing as mp
import os
from pathlib import Path
from queue import Queue
import time
from unittest.mock import Mock

import pytest

import hephaestus
from src.generators.config import cfg
from src.modules import scheduler
from src.modules.models import ProgramRes


@dataclass
class Task:
    function: Callable
    args: tuple
    callback: Callable
    error_callback: Callable

    @property
    def key(self):
        if self.function is scheduler.gen_program_mul:
            return 'generate', (self.args[0],)
        return 'compile', tuple(self.args[1])


class ControlledPool:
    """Workers finish only when the test delivers their completion callbacks."""
    def __init__(self, workers: int, temporary: Path):
        self.workers = workers
        self.temporary = temporary
        self.tasks: list[Task] = []
        self.history = []
        self.lifecycle = []
        self.max_live_batches = 0

    def apply_async(self, function, args, callback, error_callback):
        task = Task(function, args, callback, error_callback)
        self.tasks.append(task)
        self.history.append(task.key)
        assert len(self.tasks) <= self.workers
        live_batches = len(list(self.temporary.iterdir()))
        self.max_live_batches = max(self.max_live_batches, live_batches)
        assert live_batches <= self.workers

    def take(self, stage: str, *pids: int) -> Task:
        task, = [task for task in self.tasks if task.key == (stage, pids)]
        self.tasks.remove(task)
        return task

    def generated(self, pid: int, *, failed=False):
        task = self.take('generate', pid)
        task.callback(ProgramRes(failed, {
            'time': float(pid), 'programs': {}, 'escalations': [f'log-{pid}'],
        }))

    def compiled(self, *pids: int):
        task = self.take('compile', *pids)
        task.callback(({}, 1.0, {}, {}))

    def close(self):
        self.lifecycle.append('close')

    def join(self):
        if self.lifecycle[-1] == 'terminate':
            assert list(self.temporary.iterdir())
        self.lifecycle.append('join')

    def terminate(self):
        # Incomplete sources have to survive until termination/join.
        assert list(self.temporary.iterdir())
        self.lifecycle.append('terminate')
        self.tasks.clear()


@pytest.fixture
def controlled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def create(*, iterations=5, batch=1, workers=2, dry=False, seconds=None):
        options = hephaestus.parse_args([
            '--language', 'java', '-i', str(iterations), '--batch', str(batch),
            '--workers', str(workers), '-b', str(tmp_path / 'bugs'),
            '-n', 'session',
        ])
        options.dry_run = dry
        if seconds is not None:
            options.seconds = seconds
            options.stop_cond = 'timeout'
        fuzzer = hephaestus.Fuzzer(options)
        fuzzer.scheduler.reporting = Mock()
        monkeypatch.setattr(fuzzer.scheduler, '_finish', Mock())
        temporary = tmp_path / 'temporary'
        temporary.mkdir()
        make_directory = scheduler.tempfile.mkdtemp
        monkeypatch.setattr(scheduler.tempfile, 'mkdtemp',
                            lambda: make_directory(dir=temporary))
        pool = ControlledPool(workers, temporary)
        monkeypatch.setattr(scheduler.mp, 'Pool', lambda *args, **kwargs: pool)

        def run(steps, *, interrupted=False):
            actions = iter(steps)

            class CompletionQueue(Queue):
                def get(self, block=True, timeout=None):
                    if block and self.empty():
                        action = next(actions)
                        action()
                    return super().get(block=False)

            monkeypatch.setattr(scheduler, 'Queue', CompletionQueue)
            fuzzer.scheduler.run_parallel('test compiler')
            assert list(actions) == []
            assert pool.tasks == []
            expected_shutdown = 'terminate' if interrupted else 'close'
            assert pool.lifecycle == [expected_shutdown, 'join']
            assert list(temporary.iterdir()) == []
            fuzzer.scheduler._finish.assert_called_once_with()

        return fuzzer.scheduler, pool, run
    return create


def test_batch_one_overlaps_generations_and_slow_compilation(controlled):
    runner, pool, run = controlled()

    def first_completion():
        assert pool.history == [('generate', (1,)), ('generate', (2,))]
        pool.generated(2)

    def slow_becomes_ready():
        pool.generated(1)
        pool.generated(3)

    run([
        first_completion, lambda: pool.compiled(2), slow_becomes_ready,
        lambda: pool.compiled(3), lambda: pool.generated(4),
        lambda: pool.compiled(4), lambda: pool.generated(5),
        lambda: pool.compiled(5), lambda: pool.compiled(1),
    ])
    assert pool.history == [
        ('generate', (1,)), ('generate', (2,)), ('compile', (2,)),
        ('generate', (3,)), ('compile', (1,)), ('compile', (3,)),
        ('generate', (4,)), ('compile', (4,)),
        ('generate', (5,)), ('compile', (5,)),
    ]
    # Slow compilation 1 does not block newer independent work, but its
    # submission precedes compilation 3, which became ready just after it.
    assert [call.args[2] for call in runner.reporting.update_stats.call_args_list] == [
        2.0, 3.0, 4.0, 5.0, 1.0,
    ]
    assert pool.max_live_batches == 2


def test_larger_batches_wait_only_for_their_own_inputs(controlled):
    runner, pool, run = controlled(iterations=7, batch=3)
    run([
        lambda: pool.generated(2), lambda: pool.generated(3),
        lambda: pool.generated(4), lambda: pool.generated(5),
        lambda: pool.generated(6), lambda: pool.compiled(4, 5, 6),
        lambda: pool.generated(7), lambda: pool.compiled(7),
        lambda: pool.generated(1), lambda: pool.compiled(1, 2, 3),
    ])
    assert [key for key in pool.history if key[0] == 'compile'] == [
        ('compile', (4, 5, 6)), ('compile', (7,)), ('compile', (1, 2, 3)),
    ]
    updates = runner.reporting.update_stats.call_args_list
    assert [call.args[1:3] for call in updates] == [(3, 15.0), (1, 7.0), (3, 6.0)]
    assert updates[-1].args[3] == {
        '1': ['log-1'], '2': ['log-2'], '3': ['log-3'],
    }


def test_ready_compilations_dispatch_before_new_generations(controlled):
    runner, pool, run = controlled(iterations=4)

    def ready_together():
        pool.generated(2)
        pool.generated(1)

    def finish_compilations():
        assert pool.history[2:] == [('compile', (2,)), ('compile', (1,))]
        pool.compiled(2)
        pool.compiled(1)

    run([ready_together, finish_compilations,
         lambda: pool.generated(3), lambda: pool.generated(4),
         lambda: pool.compiled(3), lambda: pool.compiled(4)])
    assert runner.reporting.update_stats.call_count == 4


@pytest.mark.parametrize('disabled_metrics', [False, True])
def test_dry_run_and_generation_failures_preserve_counts_and_cleanup(
        controlled, disabled_metrics):
    runner, pool, run = controlled(iterations=5, batch=2, dry=True)
    runner.options.disable_metrics = disabled_metrics
    run([lambda: pool.generated(2, failed=True), lambda: pool.generated(1),
         lambda: pool.generated(4), lambda: pool.generated(3),
         lambda: pool.generated(5)])
    assert all(stage == 'generate' for stage, _ in pool.history)
    updates = runner.reporting.update_stats.call_args_list
    assert [call.args[1] for call in updates] == [2, 2, 1]
    assert all(call.args[0] == ({}, 0, {}, {}) for call in updates)
    if disabled_metrics:
        assert all(call.args[3] == {} for call in updates)


@pytest.mark.parametrize('stopping', ['timeout', 'explicit'])
def test_stop_admission_but_drain_admitted_batches(controlled, monkeypatch, stopping):
    runner, pool, run = controlled(iterations=20, seconds=1 if stopping == 'timeout' else None)
    elapsed = [0.0]
    monkeypatch.setattr(scheduler.time, 'perf_counter', lambda: elapsed[0])

    def stop():
        elapsed[0] = 2.0
        if stopping == 'explicit':
            runner.stopped = True
        pool.generated(1)

    run([stop, lambda: pool.generated(2),
         lambda: pool.compiled(1), lambda: pool.compiled(2)])
    assert [key for key in pool.history if key[0] == 'generate'] == [
        ('generate', (1,)), ('generate', (2,)),
    ]
    assert runner.reporting.update_stats.call_count == 2


def test_timeout_drains_generations_not_yet_submitted_in_an_admitted_batch(
        controlled, monkeypatch):
    runner, pool, run = controlled(iterations=20, batch=3, seconds=1)
    elapsed = [0.0]
    monkeypatch.setattr(scheduler.time, 'perf_counter', lambda: elapsed[0])

    def timeout():
        elapsed[0] = 2.0
        pool.generated(2)

    run([timeout, lambda: pool.generated(3),
         lambda: pool.generated(1), lambda: pool.compiled(1, 2, 3)])
    assert pool.history == [
        ('generate', (1,)), ('generate', (2,)), ('generate', (3,)),
        ('compile', (1, 2, 3)),
    ]
    assert runner.reporting.update_stats.call_count == 1


def test_single_worker_alternates_stages_without_waiting_on_itself(controlled):
    runner, pool, run = controlled(iterations=2, workers=1)
    run([lambda: pool.generated(1), lambda: pool.compiled(1),
         lambda: pool.generated(2), lambda: pool.compiled(2)])
    assert pool.max_live_batches == 1
    assert runner.reporting.update_stats.call_count == 2


def test_preset_stop_submits_nothing(controlled):
    runner, pool, run = controlled()
    runner.stopped = True
    run([])
    assert pool.history == []
    runner.reporting.logging.assert_called_once_with('test compiler')


@pytest.mark.parametrize('stage', ['generate', 'compile', 'report'])
def test_errors_terminate_join_then_clean_incomplete_batches(controlled, stage):
    runner, pool, run = controlled()
    error = RuntimeError('task failed')

    def fail():
        staging = Path(runner.options.test_directory) / 'tmp'
        (staging / '1').mkdir(parents=True)
        (staging / '1/program.bin').write_text('incomplete artifact')
        (staging / '999').mkdir()
        if stage == 'report':
            runner.reporting.update_stats.side_effect = error
            pool.compiled(1)
        else:
            task = pool.take(stage, 1)
            task.error_callback(error)

    steps = [fail] if stage == 'generate' else [lambda: pool.generated(1), fail]
    with pytest.raises(RuntimeError, match='task failed'):
        run(steps)
    assert pool.lifecycle == ['terminate', 'join']
    assert list(pool.temporary.iterdir()) == []
    staging = Path(runner.options.test_directory) / 'tmp'
    assert not (staging / '1').exists()
    assert (staging / '999').exists()
    runner._finish.assert_not_called()


@pytest.mark.parametrize('missing_result', [False, True])
def test_interruption_terminates_workers_before_cleanup(controlled, missing_result):
    runner, pool, run = controlled()

    def interrupt():
        if missing_result:
            pool.take('generate', 1).callback(None)
        else:
            raise KeyboardInterrupt

    run([interrupt], interrupted=True)
    assert pool.lifecycle == ['terminate', 'join']
    assert runner.stopped
    assert list(pool.temporary.iterdir()) == []
    runner.reporting.update_stats.assert_not_called()
    runner._finish.assert_called_once_with()


@pytest.mark.parametrize('exception', [KeyboardInterrupt, RuntimeError])
def test_submission_errors_also_clean_after_worker_shutdown(
        controlled, monkeypatch, exception):
    runner, pool, run = controlled()
    monkeypatch.setattr(pool, 'apply_async', Mock(side_effect=exception))
    if exception is KeyboardInterrupt:
        run([], interrupted=True)
    else:
        with pytest.raises(RuntimeError):
            run([])
        assert pool.lifecycle == ['terminate', 'join']
        assert list(pool.temporary.iterdir()) == []


def _probe_generation(pid: int, directory: str) -> ProgramRes:
    root = Path(os.environ['SCHEDULER_PROBE_DIRECTORY'])
    (root / f'generated-{pid}').write_text(str(os.getpid()))
    deadline = time.monotonic() + 10
    while not all((root / f'generated-{other}').exists() for other in (1, 2)):
        if time.monotonic() > deadline:
            raise RuntimeError('generations did not overlap')
        time.sleep(0.01)
    return ProgramRes(False, {'time': float(pid), 'programs': {}})


def _probe_compilation(directory: str, oracles):
    pid, = oracles
    root = Path(os.environ['SCHEDULER_PROBE_DIRECTORY'])
    (root / f'compiled-{pid}').write_text(str(os.getpid()))
    return {}, 0.1, {}, {pid: {'direct': None, 'closure': []}}


@pytest.mark.parametrize('method', ['spawn', 'fork'])
def test_real_pool_overlaps_generation_and_publishes_only_in_parent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method):
    monkeypatch.setattr(scheduler.mp, 'Pool', mp.get_context(method).Pool)
    monkeypatch.setattr(scheduler, 'gen_program_mul', _probe_generation)
    monkeypatch.setattr(scheduler, 'check_oracle_mul', _probe_compilation)
    monkeypatch.setenv('SCHEDULER_PROBE_DIRECTORY', str(tmp_path))
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 1.0)
    options = hephaestus.parse_args([
        '--language', 'kotlin', '-i', '2', '--workers', '2',
        '-b', str(tmp_path / 'bugs'), '-n', 'session',
        '-F', str(tmp_path / 'run.log'),
    ])
    fuzzer = hephaestus.Fuzzer(options)
    fuzzer.setup_crossmodule_manager('probe compiler')
    parent_pid = os.getpid()
    original_update = fuzzer.reporting.update_stats
    original_save = fuzzer.reporting.manager.save_ledger
    updates = []

    def update(*args):
        assert os.getpid() == parent_pid
        updates.append(args)
        original_update(*args)

    def save(ledger):
        assert os.getpid() == parent_pid
        original_save(ledger)

    monkeypatch.setattr(fuzzer.reporting, 'update_stats', update)
    monkeypatch.setattr(fuzzer.reporting.manager, 'save_ledger', save)
    fuzzer.scheduler.run_parallel('probe compiler')
    workers = {int((tmp_path / f'generated-{pid}').read_text()) for pid in (1, 2)}
    assert len(workers) == 2
    assert parent_pid not in workers
    assert len(updates) == 2
    assert fuzzer.reporting.stats['totals'] == {'passed': 2, 'failed': 0}
    ledger = json.loads((tmp_path / 'bugs/session/crossmodule.json').read_text())
    assert ledger == {'1': {'direct': None, 'closure': []},
                      '2': {'direct': None, 'closure': []}}
