from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import functools
import multiprocessing as mp
from multiprocessing.pool import Pool
import os
from queue import Empty, Queue
import shutil
import tempfile
import time
import traceback

from src import utils
from src.compilers.base import BaseCompiler
from src.modules.artifacts import Artifacts
from src.modules.compilation import Compilation
from src.modules.coverage import LiveCoverage
from src.modules.evaluation import Evaluation
from src.modules.generation import Generation
from src.modules.models import ProgramRes, OracleResult
from src.modules.worker_reporting import TimingMetrics
from src.modules.worker import (
    RunOptions, WorkerInputs, WorkerResources, get_worker_resources,
    initialize_worker, snapshot_generator_config,
)
from src.generators.generator import EscalationLog


@dataclass
class _Batch:
    start_index: int
    size: int
    directory: str
    results: dict[int, ProgramRes] = field(default_factory=dict)

    def ordered_results(self) -> list[ProgramRes]:
        return [self.results[pid]
                for pid in range(self.start_index, self.start_index + self.size)]


@dataclass(frozen=True)
class _GenerationCompleted:
    batch: _Batch
    pid: int
    result: ProgramRes | None


@dataclass(frozen=True)
class _CompilationCompleted:
    batch: _Batch
    result: OracleResult


@dataclass(frozen=True)
class _TaskFailed:
    error: BaseException


type _Completion = _GenerationCompleted | _CompilationCompleted | _TaskFailed


class Scheduler[S]:
    def __init__(
            self, options: RunOptions, generation: Generation,
            evaluation: Evaluation[S], reporting: TimingMetrics,
            compiler_cls: type[BaseCompiler[S]], *,
            compiler_settings: S) -> None:
        self.options = options
        self.generation = generation
        self.evaluation = evaluation
        self.reporting = reporting
        self.compiler_cls = compiler_cls
        self.compiler_settings = compiler_settings
        self.stopped = False

    def stop_condition(self, iteration: int, time_passed: float) -> bool:
        if self.stopped:
            return False
        if self.options.seconds:
            return time_passed < self.options.seconds
        if self.options.iterations:
            return iteration < self.options.iterations + 1
        return True

    def get_batches(self, programs: int) -> int:
        if self.options.stop_cond == 'timeout':
            return self.options.batch
        return min(self.options.batch, self.options.iterations - programs)

    def _run[T](self, process_program: Callable[[int, str], T],
                process_res: Callable[[int, list[T], str, int], None],
                compiler_version: str) -> None:
        self.reporting.logging(compiler_version)
        iteration = 1
        time_passed = 0
        start_time = time.perf_counter()
        while self.stop_condition(iteration, time_passed):
            try:
                utils.random.reset_word_pool()
                tmpdir = tempfile.mkdtemp()
                res = []
                batches = self.get_batches(iteration - 1)
                for i in range(batches):
                    dirname = os.path.join(tmpdir, 'src')
                    pid = iteration + i
                    r = process_program(pid, dirname)
                    res.append(r)

                process_res(iteration, res, tmpdir, batches)

                time_passed = time.perf_counter() - start_time
                iteration += batches
            except KeyboardInterrupt:
                return

    def _batch_metrics(
            self, start_index: int, results: list[ProgramRes]
            ) -> tuple[float, dict[str, list[EscalationLog]]]:
        batch_time = functools.reduce(lambda acc, x: acc + x.stats["time"],
                                      results, 0)
        batch_escalations = {}
        if not self.options.disable_metrics:
            batch_escalations = {
                str(start_index + i): r.stats.get("escalations", [])
                for i, r in enumerate(results)
                if r.stats.get("escalations")
            }
        return batch_time, batch_escalations

    @staticmethod
    def _batch_oracles(start_index: int, results: list[ProgramRes]
                       ) -> OrderedDict[int, ProgramRes]:
        oracles = OrderedDict()
        for i, r in enumerate(results):
            oracles[start_index + i] = r
        return oracles

    def _finish(self) -> None:
        path = os.path.join(self.options.test_directory, 'tmp')
        if os.path.exists(path):
            shutil.rmtree(path)
        print()
        print("Total faults: " + str(self.reporting.stats['totals']['failed']))

    def run(self, compiler_version: str) -> None:

        def process_program(pid: int, dirname: str) -> ProgramRes:
            return self.generation.gen_program(pid, dirname)

        def process_res(start_index: int, res: list[ProgramRes],
                        testdir: str, batch: int) -> None:
            oracles = self._batch_oracles(start_index, res)
            batch_time, batch_escalations = self._batch_metrics(start_index, res)
            self.reporting.update_stats(
                ({}, 0, {}, {}) if self.options.dry_run
                else self.evaluation.check_oracle(testdir, oracles),
                batch, batch_time, batch_escalations)

        try:
            self._run(process_program, process_res, compiler_version)
        except KeyboardInterrupt:
            pass
        self._finish()

    def run_parallel(self, compiler_version: str) -> None:
        batches: dict[int, _Batch] = {}
        pool = mp.Pool(
            self.options.workers,
            initializer=initialize_scheduler_worker,
            initargs=(self.worker_inputs(compiler_version),))

        try:
            self._run_parallel(pool, compiler_version, batches)
            pool.close()
            pool.join()
        except KeyboardInterrupt:
            self.stopped = True
            pool.terminate()
            pool.join()
        except BaseException:
            pool.terminate()
            pool.join()
            raise
        finally:
            # Sources must remain available until all workers have stopped.
            for batch in batches.values():
                shutil.rmtree(batch.directory, ignore_errors=True)
                for pid in range(batch.start_index, batch.start_index + batch.size):
                    shutil.rmtree(os.path.join(
                        self.options.test_directory, 'tmp', str(pid)),
                        ignore_errors=True)
        self._finish()

    def _run_parallel(self, pool: Pool, compiler_version: str,
                      batches: dict[int, _Batch]) -> None:
        self.reporting.logging(compiler_version)
        completions: Queue[_Completion] = Queue()
        generations: deque[tuple[_Batch, int]] = deque()
        compilations: deque[_Batch] = deque()
        iteration = 1
        active = 0
        start_time = time.perf_counter()

        def finish_batch(batch: _Batch, result: OracleResult) -> None:
            batch_time, escalations = self._batch_metrics(
                batch.start_index, batch.ordered_results())
            self.reporting.update_stats(result, batch.size, batch_time, escalations)
            shutil.rmtree(batch.directory, ignore_errors=True)
            del batches[batch.start_index]

        def complete(event: _Completion) -> None:
            nonlocal active
            active -= 1
            if isinstance(event, _TaskFailed):
                raise event.error
            if isinstance(event, _CompilationCompleted):
                finish_batch(event.batch, event.result)
                return
            if event.result is None:
                raise KeyboardInterrupt
            batch = event.batch
            batch.results[event.pid] = event.result
            if len(batch.results) == batch.size:
                if self.options.dry_run:
                    finish_batch(batch, ({}, 0, {}, {}))
                else:
                    compilations.append(batch)

        def drain_completions() -> None:
            while True:
                try:
                    event = completions.get_nowait()
                except Empty:
                    return
                complete(event)

        def task_failed(error: BaseException) -> None:
            completions.put(_TaskFailed(error))

        while True:
            drain_completions()
            while active < self.options.workers:
                # Process publications and ready compilations before refilling
                # the pool. Callbacks only notify; the parent owns reporting.
                drain_completions()
                if compilations:
                    batch = compilations.popleft()
                    oracles = self._batch_oracles(
                        batch.start_index, batch.ordered_results())
                    pool.apply_async(
                        check_oracle_mul, args=(batch.directory, oracles),
                        callback=lambda result, batch=batch: completions.put(
                            _CompilationCompleted(batch, result)),
                        error_callback=task_failed)
                elif generations:
                    batch, pid = generations.popleft()
                    pool.apply_async(
                        gen_program_mul,
                        args=(pid, os.path.join(batch.directory, 'src')),
                        callback=lambda result, batch=batch, pid=pid: completions.put(
                            _GenerationCompleted(batch, pid, result)),
                        error_callback=task_failed)
                elif (len(batches) < self.options.workers and
                      self.stop_condition(iteration, time.perf_counter() - start_time)):
                    # Bound both submitted tasks and live batch directories.
                    # A batch remains live through compilation, not just generation.
                    size = self.get_batches(iteration - 1)
                    batch = _Batch(iteration, size, tempfile.mkdtemp())
                    batches[iteration] = batch
                    generations.extend((batch, pid)
                                       for pid in range(iteration, iteration + size))
                    iteration += size
                    continue
                else:
                    break
                active += 1
            if not active:
                return
            complete(completions.get())

    def worker_inputs(self, compiler_version: str) -> WorkerInputs[S]:
        return WorkerInputs(RunOptions.from_namespace(self.options),
                            snapshot_generator_config(), compiler_version,
                            self.compiler_cls, self.compiler_settings)


def _create_worker_scheduler[S](resources: WorkerResources[S]) -> Scheduler[S]:
    options = resources.options
    artifacts = Artifacts(options)
    generation = Generation(options, artifacts, resources.manager)
    compilation = Compilation(
        options, artifacts, {options.language: resources.compiler_cls},
        settings=resources.compiler_settings)
    coverage = LiveCoverage(options, compilation)
    timing = TimingMetrics(options)
    evaluation = Evaluation(
        options, compilation, artifacts, coverage, timing, resources.manager)
    return Scheduler(options, generation, evaluation, timing, resources.compiler_cls,
                     compiler_settings=resources.compiler_settings)


def initialize_scheduler_worker[S](inputs: WorkerInputs[S]) -> None:
    initialize_worker(inputs, scheduler_factory=_create_worker_scheduler)


def _worker_scheduler() -> Scheduler:
    scheduler = get_worker_resources().scheduler
    if scheduler is None:
        raise RuntimeError('Worker scheduler has not been initialized')
    return scheduler


def gen_program_mul(pid: int, dirname: str) -> ProgramRes | None:
    scheduler = _worker_scheduler()
    if scheduler.stopped:
        return
    try:
        utils.random.r.seed()
        return scheduler.generation.gen_program(pid, dirname)
    except KeyboardInterrupt:
        scheduler.stopped = True


def check_oracle_mul(dirname: str,
                     oracles: Mapping[int, ProgramRes]) -> OracleResult:
    scheduler = _worker_scheduler()
    if scheduler.stopped:
        return {}, 0, {}, {}
    try:
        return scheduler.evaluation.check_oracle(dirname, oracles)
    except KeyboardInterrupt:
        scheduler.stopped = True
        return {}, 0, {}, {}
    except Exception as exc:
        if scheduler.options.print_stacktrace:
            err = str(traceback.format_exc())
        else:
            err = str(exc)
        print('Internal error while checking the oracle')
        print(err)
        return {}, 0, {}, {}
