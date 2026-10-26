import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.compilers.groovy import GroovyCompiler
from src.compilers.java import JavaCompiler
from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.compilers.results import (
    CommandObservation, ParsedDiagnostics, ProcessResult, VariantObservation,
)
from src.compilers.scala import ScalaCompiler
from src.generators.config import cfg
from src.modules.artifacts import Artifacts
from src.modules.compilation import Compilation
from src.modules.compilation import ProviderCompilationError
from src.modules.crossmodule import PublicationError
from src.modules.evaluation import Evaluation
from src.modules.models import OracleResult, ProgramRes
from src.modules.oracle_modes import (
    BackendComparisonMode, CompilationMode, InliningComparisonMode,
)
from src.modules.scheduler import Scheduler
from src.modules.worker_reporting import Reporting


COMPILER_TYPES = {
    'kotlin': KotlinCompiler,
    'java': JavaCompiler,
    'scala': ScalaCompiler,
    'groovy': GroovyCompiler,
}
EXTENSIONS = {'kotlin': 'kt', 'java': 'java', 'scala': 'scala', 'groovy': 'groovy'}


@pytest.fixture(autouse=True)
def disable_crossmodule_by_default(monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)


def compiler_diagnostic(language, program):
    if language == 'kotlin':
        return f'{program}:1:1: error: rejected\n'
    if language == 'java':
        return f'{program}:1: error: rejected\n\n'
    if language == 'scala':
        return f'-- Error: {program}:1:1 --------\n rejected\n'
    return f'{program}: rejected\n\n'


class FakeCompilation:
    def __init__(self, language, *, returncode=0, output='', launch_failure=None,
                 crash_message=None, exception=None):
        self.language = language
        self.returncode = returncode
        self.output = output
        self.launch_failure = launch_failure
        self.crash_message = crash_message
        self.exception = exception
        self.requests = ()
        self.compiled_pid_dirs = []
        self.input_root = None

    def make_compiler(self, input_name, filter_patterns, **kwargs):
        self.input_root = input_name
        compiler_type = COMPILER_TYPES[self.language]
        if self.language == 'kotlin':
            compiler = compiler_type(input_name, filter_patterns,
                                     settings=KotlinSettings())
        else:
            compiler = compiler_type(input_name, filter_patterns, settings=None)
        return compiler

    def run_variants(self, requests):
        self.requests = tuple(requests)
        if self.exception is not None:
            raise self.exception
        observations = []
        for request in self.requests:
            compiler = request.compiler
            source_root = Path(self.input_root)
            self.compiled_pid_dirs = sorted(
                path.name for path in source_root.iterdir() if path.is_dir())
            diagnostics, _ = compiler.analyze_compiler_output(self.output)
            if self.crash_message is not None:
                compiler.crash_msg = self.crash_message
            process = ProcessResult(
                self.returncode, self.output, self.launch_failure)
            command = CommandObservation(
                process, diagnostics or {}, ParsedDiagnostics(), (),
                compiler.crash_msg, tuple(compiler.get_compiler_cmd()),
                request.working_directory, 0.25)
            observations.append(VariantObservation(
                request.variant_id, command, settings=request.compiler.settings))
        return tuple(observations)

    @staticmethod
    def timed(function, *args, **kwargs):
        return function(*args, **kwargs), 0.0

    def enrich_error(self, *args, **kwargs):
        raise RuntimeError('diagnostic enrichment unavailable')


class CoverageStub:
    def __init__(self):
        self.calls = []

    def maybe_run_live_jacoco(self, pid, phase_changes):
        self.calls.append((pid, phase_changes))


class ManagerStub:
    @staticmethod
    def dependency_klibs(closure):
        return []

    @staticmethod
    def apply_drop_knob(klibs):
        return klibs, []


def options_for(run_dir, *, language='kotlin', dry_run=False):
    return SimpleNamespace(
        language=language, error_filter_patterns='', test_directory=str(run_dir),
        debug=False, disable_metrics=True, time_metrics=False, rerun=False,
        transformations=0, keep_everything=False, dump_ir=False,
        print_stacktrace=False, dry_run=dry_run, stop_cond='iterations',
        seconds=0, iterations=2, batch=2, workers=2, oracle='compilation',
        transformation_types=[], bugs=str(run_dir.parent), name='test',
        log_file=str(run_dir.parent / 'test.log'),
    )


def make_evaluation(run_dir, *, language='kotlin', returncode=0, output='',
                    launch_failure=None, crash_message=None, exception=None,
                    mode=None, options=None):
    run_dir.mkdir(parents=True, exist_ok=True)
    options = options or options_for(run_dir, language=language)
    artifacts = Artifacts(options)
    compilation = FakeCompilation(
        language, returncode=returncode, output=output,
        launch_failure=launch_failure, crash_message=crash_message,
        exception=exception)
    mode = mode or CompilationMode()
    reporting = Reporting(options, mode=mode)
    coverage = CoverageStub()
    evaluation = Evaluation(options, compilation, artifacts, coverage, reporting,
                            ManagerStub(), mode=mode)
    return evaluation, compilation, artifacts, coverage, reporting


def generated_program(dirname, pid, language, expected=True):
    extension = EXTENSIONS[language]
    program = Path(dirname) / 'src' / f'd{pid}' / f'Main.{extension}'
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_text('generated source')
    return str(program)


def healthy_program(dirname, pid, language='kotlin', expected=True):
    program = generated_program(dirname, pid, language)
    return ProgramRes(False, {
        'programs': {program: expected},
        'error': None if expected else 'expected rejection', 'time': 0.1,
    })


@pytest.mark.parametrize('language', ['kotlin', 'java', 'scala', 'groovy'])
def test_real_adapter_diagnostics_keep_expected_rejection_conclusive(
        tmp_path, language):
    batch_dir = tmp_path / 'batch'
    result = healthy_program(batch_dir, 1, language, expected=False)
    program, = result.stats['programs']
    evaluation, _, _, _, _ = make_evaluation(
        tmp_path / 'run', language=language, returncode=1,
        output=compiler_diagnostic(language, program))

    outcomes = evaluation.check_oracle(str(batch_dir), {1: result})

    assert outcomes.faults == {}
    assert outcomes.inconclusive == {}


@pytest.mark.parametrize('language', ['kotlin', 'java', 'scala', 'groovy'])
def test_real_adapter_diagnostics_preserve_reliable_mismatch_as_fault(
        tmp_path, language):
    batch_dir = tmp_path / 'batch'
    result = healthy_program(batch_dir, 1, language, expected=True)
    program, = result.stats['programs']
    evaluation, _, _, _, _ = make_evaluation(
        tmp_path / 'run', language=language, returncode=1,
        output=compiler_diagnostic(language, program))

    outcomes = evaluation.check_oracle(str(batch_dir), {1: result})

    assert set(outcomes.faults) == {1}
    assert outcomes.inconclusive == {}
    assert 'rejected' in outcomes.faults[1]['error']


@pytest.mark.parametrize(('returncode', 'launch_failure', 'reason'), [
    (None, 'executable not found', 'compiler_launch_failed'),
    (2, None, 'unattributable_compiler_failure'),
])
def test_unavailable_compiler_evidence_is_inconclusive_and_retained(
        tmp_path, returncode, launch_failure, reason):
    batch_dir = tmp_path / 'batch'
    result = healthy_program(batch_dir, 1)
    output = 'executable not found' if launch_failure else 'fatal internal failure'
    evaluation, _, _, _, _ = make_evaluation(
        tmp_path / 'run', returncode=returncode, output=output,
        launch_failure=launch_failure)

    outcomes = evaluation.check_oracle(str(batch_dir), {1: result})

    record = outcomes.inconclusive[1]
    assert record['inconclusive_reason'] == reason
    assert record['oracle'] == 'compilation'
    assert record['inconclusive_evidence']['compilation']['output'] == output
    assert (tmp_path / 'run' / '1' / 'program.kt').exists() is False
    assert not batch_dir.exists()


def test_actual_process_runner_reports_missing_executable_as_shell_exit(tmp_path):
    options = options_for(tmp_path / 'run')
    compilation = Compilation(
        options, Artifacts(options), {'kotlin': KotlinCompiler},
        settings=KotlinSettings())

    missing = compilation.run_process(['hephaestus-tool-that-does-not-exist'])

    assert missing.returncode is not None and missing.returncode != 0
    assert missing.launch_failure is None
    assert 'hephaestus-tool-that-does-not-exist' in missing.output


def test_shell_missing_compiler_in_original_oracle_is_inconclusive(tmp_path):
    run_dir = tmp_path / 'run'
    options = options_for(run_dir)
    artifacts = Artifacts(options)
    compilation = Compilation(
        options, artifacts, {'kotlin': KotlinCompiler},
        settings=KotlinSettings(executable=str(tmp_path / 'missing-kotlinc')))
    reporting = Reporting(options, mode=CompilationMode())
    evaluation = Evaluation(
        options, compilation, artifacts, CoverageStub(), reporting, ManagerStub(),
        mode=CompilationMode())
    batch_dir = tmp_path / 'batch'
    result = healthy_program(batch_dir, 1)

    outcomes = evaluation.check_oracle(str(batch_dir), {1: result})

    assert outcomes.faults == {}
    record = outcomes.inconclusive[1]
    assert record['inconclusive_reason'] == 'unattributable_compiler_failure'
    assert record['inconclusive_evidence']['compilation']['returncode'] != 0
    assert 'missing-kotlinc' in record['inconclusive_evidence']['compilation']['output']


def test_success_expected_acceptance_mismatch_and_recognized_crash(tmp_path):
    batch_dir = tmp_path / 'success-batch'
    success = healthy_program(batch_dir, 1)
    evaluation, _, _, _, _ = make_evaluation(tmp_path / 'success-run')
    assert evaluation.check_oracle(str(batch_dir), {1: success}).faults == {}

    mismatch_dir = tmp_path / 'mismatch-batch'
    mismatch = healthy_program(mismatch_dir, 2, expected=False)
    evaluation, _, _, _, _ = make_evaluation(tmp_path / 'mismatch-run')
    outcomes = evaluation.check_oracle(str(mismatch_dir), {2: mismatch})
    assert outcomes.faults[2]['error'].startswith('SHOULD NOT BE COMPILED')
    assert outcomes.inconclusive == {}

    crash_dir = tmp_path / 'crash-batch'
    crash = healthy_program(crash_dir, 3)
    evaluation, _, _, _, _ = make_evaluation(
        tmp_path / 'crash-run', returncode=1,
        output='recognized crash output', crash_message='recognized compiler crash')
    outcomes = evaluation.check_oracle(str(crash_dir), {3: crash})
    assert outcomes.faults[3]['error'] == 'recognized compiler crash'
    assert outcomes.inconclusive == {}


def test_mixed_generation_failure_compiles_only_healthy_sibling(tmp_path):
    batch_dir = tmp_path / 'batch'
    failed_source = generated_program(batch_dir, 1, 'kotlin')
    healthy = healthy_program(batch_dir, 2)
    run_dir = tmp_path / 'run'
    (run_dir / 'tmp' / '1').mkdir(parents=True)
    (run_dir / 'tmp' / '1' / 'partial.bin').write_text('partial generation evidence')
    failed = ProgramRes(True, {
        'programs': {}, 'error': 'translation failed after writing source', 'time': 0.0,
    })
    evaluation, compilation, _, _, _ = make_evaluation(run_dir)

    outcomes = evaluation.check_oracle(str(batch_dir), {1: failed, 2: healthy})

    assert compilation.compiled_pid_dirs == ['d2']
    assert set(outcomes.inconclusive) == {1}
    assert outcomes.inconclusive[1]['inconclusive_reason'] == 'generation_failed'
    assert outcomes.inconclusive[1]['error'] == 'translation failed after writing source'
    assert (run_dir / '1' / 'partial_source' / 'Main.kt').read_text() == 'generated source'
    assert (run_dir / '1' / 'partial.bin').read_text() == 'partial generation evidence'
    assert not Path(failed_source).exists()
    assert outcomes.faults == {}


def test_all_generation_failed_skips_compilation_and_preserves_partial_sources(tmp_path):
    batch_dir = tmp_path / 'batch'
    program = generated_program(batch_dir, 1, 'kotlin')
    result = ProgramRes(True, {'programs': {}, 'error': 'transform failed', 'time': 0})
    evaluation, compilation, _, _, _ = make_evaluation(tmp_path / 'run')

    outcomes = evaluation.check_oracle(str(batch_dir), {1: result})

    assert compilation.requests == ()
    assert outcomes.faults == {}
    assert set(outcomes.inconclusive) == {1}
    assert (tmp_path / 'run' / '1' / 'partial_source' / 'Main.kt').exists()
    assert not Path(program).exists()


def test_generation_failure_without_source_or_tmp_is_still_reported(tmp_path):
    batch_dir = tmp_path / 'batch'
    failed = ProgramRes(True, {
        'programs': {}, 'error': 'translator failed before writing source', 'time': 0,
    })
    evaluation, compilation, _, _, _ = make_evaluation(tmp_path / 'run')

    outcomes = evaluation.check_oracle(str(batch_dir), {1: failed})

    assert compilation.requests == ()
    assert outcomes.inconclusive[1]['error'] == 'translator failed before writing source'
    assert not (tmp_path / 'run' / '1').exists()
    assert not batch_dir.exists()


def test_handled_evaluation_exception_does_not_become_a_pass(tmp_path):
    batch_dir = tmp_path / 'batch'
    result = healthy_program(batch_dir, 1)
    evaluation, _, _, _, _ = make_evaluation(
        tmp_path / 'run', exception=RuntimeError('oracle evaluation exploded'))

    outcomes = evaluation.check_oracle_safely(str(batch_dir), {1: result})

    assert outcomes.faults == {}
    assert outcomes.inconclusive[1]['inconclusive_reason'] == 'evaluation_error'
    assert 'oracle evaluation exploded' in outcomes.inconclusive[1]['inconclusive_details']


def test_secondary_enrichment_error_keeps_established_fault(tmp_path):
    batch_dir = tmp_path / 'batch'
    result = healthy_program(batch_dir, 1)
    program, = result.stats['programs']
    options = options_for(tmp_path / 'run')
    options.disable_metrics = False
    evaluation, _, _, _, _ = make_evaluation(
        tmp_path / 'run', language='kotlin', returncode=1,
        output=compiler_diagnostic('kotlin', program), options=options)

    outcomes = evaluation.check_oracle(str(batch_dir), {1: result})

    assert set(outcomes.faults) == {1}
    assert outcomes.inconclusive == {}
    assert outcomes.faults[1]['secondary_errors'][0]['operation'] == 'diagnostic_enrichment'


def test_ir_and_live_coverage_failures_are_secondary_to_established_fault(tmp_path):
    batch_dir = tmp_path / 'batch'
    result = healthy_program(batch_dir, 1)
    program, = result.stats['programs']
    options = options_for(tmp_path / 'run')
    options.dump_ir = True
    evaluation, _, _, coverage, _ = make_evaluation(
        tmp_path / 'run', language='kotlin', returncode=1,
        output=compiler_diagnostic('kotlin', program), options=options)

    def fail_ir(*args):
        raise OSError('IR output unavailable')

    def fail_coverage(pid, phase_changes):
        raise OSError('coverage instrumentation failed')

    evaluation.artifacts.preserve_ir_changes_to_tmp = fail_ir
    coverage.maybe_run_live_jacoco = fail_coverage

    outcomes = evaluation.check_oracle(str(batch_dir), {1: result})

    assert set(outcomes.faults) == {1}
    assert outcomes.inconclusive == {}
    assert {error['operation'] for error in outcomes.faults[1]['secondary_errors']} == {
        'preserve_ir_changes', 'live_coverage'}


@pytest.mark.parametrize('provider_failure', ['launch', 'publication', 'rejection'])
def test_crossmodule_provider_failure_policy_preserves_primary_verdict(
        tmp_path, monkeypatch, provider_failure):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 1.0)
    batch_dir = tmp_path / 'batch'
    program = generated_program(batch_dir, 1, 'kotlin')
    result = ProgramRes(False, {
        'programs': {program: True}, 'error': None, 'time': 0.1,
    })
    evaluation, compilation, _, _, _ = make_evaluation(tmp_path / 'run')
    evaluation.manager = ManagerStub()

    def build_provider(*args):
        if provider_failure == 'launch':
            raise ProviderCompilationError(
                ProcessResult(None, 'cannot start provider compiler', 'not found'),
                0.2)
        if provider_failure == 'publication':
            return ('/tmp/provider.klib', ['fake-provider']), 0.2
        return None, 0.2

    compilation._build_crossmodule_provider = build_provider
    if provider_failure == 'publication':
        def fail_publication(*args, **kwargs):
            raise PublicationError('ledger publication denied')

        evaluation.manager.publish_provider = fail_publication

    outcomes = evaluation.check_oracle(str(batch_dir), {1: result})

    assert outcomes.faults == {}
    if provider_failure == 'rejection':
        assert outcomes.inconclusive == {}
    else:
        record = outcomes.inconclusive[1]
        assert record['inconclusive_reason'] == 'provider_publication_failed'
        assert record['inconclusive_evidence']['primary_verdict'] == 'pass'
        assert (tmp_path / 'run' / '1' / 'partial_source' / 'Main.kt').exists()
        if provider_failure == 'launch':
            assert record['inconclusive_evidence']['provider_process']['output'] == \
                'cannot start provider compiler'
        else:
            assert 'ledger publication denied' in record['inconclusive_details']


def test_provider_live_coverage_failure_keeps_primary_pass(tmp_path, monkeypatch,
                                                           capsys):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 1.0)
    batch_dir = tmp_path / 'batch'
    program = generated_program(batch_dir, 1, 'kotlin')
    result = ProgramRes(False, {
        'programs': {program: True}, 'error': None, 'time': 0.1,
    })
    options = options_for(tmp_path / 'run')
    options.keep_everything = True
    evaluation, compilation, _, coverage, _ = make_evaluation(
        tmp_path / 'run', options=options)
    evaluation.manager = ManagerStub()
    compilation._build_crossmodule_provider = lambda *args: (
        ('/tmp/provider.klib', ['fake-provider']), 0.2)
    evaluation.manager.publish_provider = lambda *args: {
        'direct': None, 'closure': [],
    }

    def fail_coverage(pid, phase_changes):
        raise OSError('live coverage failed after primary compile')

    coverage.maybe_run_live_jacoco = fail_coverage

    outcomes = evaluation.check_oracle(str(batch_dir), {1: result})

    assert outcomes.faults == {}
    assert outcomes.inconclusive == {}
    assert set(outcomes.publications) == {1}
    assert 'live_coverage' in capsys.readouterr().out


def test_dry_run_counts_only_generation_failures_as_inconclusive(tmp_path):
    batch_dir = tmp_path / 'batch'
    failed = ProgramRes(True, {'programs': {}, 'error': 'generation failed', 'time': 0})
    healthy = healthy_program(batch_dir, 2)
    evaluation, _, _, _, _ = make_evaluation(tmp_path / 'run')

    outcomes = evaluation.skipped_compilation_result(
        str(batch_dir), {1: failed, 2: healthy})

    assert set(outcomes.inconclusive) == {1}
    assert outcomes.inconclusive[1]['inconclusive_reason'] == 'generation_failed'
    assert outcomes.faults == {}


@pytest.mark.parametrize('mode', [CompilationMode(), BackendComparisonMode(),
                                  InliningComparisonMode()])
def test_every_mode_writes_zero_total_and_empty_inconclusive_report(tmp_path,
                                                                   monkeypatch,
                                                                   mode):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    options = options_for(tmp_path)
    options.oracle = mode.name
    reporting = Reporting(options, mode=mode)

    reporting.update_stats(OracleResult(), batch=1, batch_time=0)

    assert reporting.stats['totals']['inconclusive'] == 0
    assert (tmp_path / 'inconclusive.json').read_text() == '{}'
    divergences = tmp_path / 'divergences.md'
    assert divergences.exists() is mode.compares_variants()
    if mode.compares_variants():
        assert '0 divergent program(s)' in divergences.read_text()


class GenerationStub:
    def __init__(self, options, failed_pids):
        self.options = options
        self.failed_pids = failed_pids

    def gen_program(self, pid, dirname):
        extension = EXTENSIONS[self.options.language]
        package = Path(dirname) / f'd{pid}'
        package.mkdir(parents=True, exist_ok=True)
        source = package / f'Main.{extension}'
        source.write_text('partial source')
        if pid in self.failed_pids:
            tmp = Path(self.options.test_directory) / 'tmp' / str(pid)
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / 'partial.bin').write_text('partial')
            return ProgramRes(True, {
                'programs': {}, 'error': 'generation failed for {}'.format(pid), 'time': 0.1,
            })
        return ProgramRes(False, {
            'programs': {str(source): True}, 'error': None, 'time': 0.1,
        })


class ImmediatePool:
    def __init__(self, *args, **kwargs):
        self.closed = False

    def apply_async(self, function, args, callback, error_callback):
        try:
            callback(function(*args))
        except BaseException as error:
            error_callback(error)

    def close(self):
        self.closed = True

    def join(self):
        pass

    def terminate(self):
        pass


def run_scheduler_with_fake_pool(scheduler, monkeypatch):
    import src.modules.scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module.mp, 'Pool', ImmediatePool)
    monkeypatch.setattr(scheduler_module, 'initialize_scheduler_worker',
                        lambda *args: None)
    monkeypatch.setattr(scheduler_module, '_worker_scheduler', lambda: scheduler)
    monkeypatch.setattr(scheduler, 'worker_inputs', lambda version: None)
    scheduler.run_parallel('fake compiler')


@pytest.mark.parametrize('parallel', [False, True])
@pytest.mark.parametrize('dry_run', [False, True])
def test_scheduler_accounts_mixed_generation_batch_in_serial_and_parallel(
        tmp_path, monkeypatch, parallel, dry_run):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    run_dir = tmp_path / 'run'
    options = options_for(run_dir, language='java', dry_run=dry_run)
    options.iterations = 2
    options.batch = 2
    options.workers = 2
    options.debug = True
    artifacts = Artifacts(options)
    generation = GenerationStub(options, {1})
    compilation = FakeCompilation('java')
    reporting = Reporting(options, mode=CompilationMode())
    reporting.logging = lambda version: None
    coverage = CoverageStub()
    evaluation = Evaluation(options, compilation, artifacts, coverage, reporting,
                            ManagerStub())
    scheduler = Scheduler(options, generation, evaluation, reporting, JavaCompiler,
                          compiler_settings=None)

    if parallel:
        run_scheduler_with_fake_pool(scheduler, monkeypatch)
    else:
        scheduler.run('fake compiler')

    totals = reporting.stats['totals']
    assert totals['failed'] == 0
    assert totals['inconclusive'] == 1
    assert totals['passed'] == 1
    record = json.loads((run_dir / 'inconclusive.json').read_text())
    assert set(record) == {'1'}


@pytest.mark.parametrize('parallel', [False, True])
def test_evaluation_exceptions_are_inconclusive_in_both_scheduler_paths(
        tmp_path, monkeypatch, parallel):
    run_dir = tmp_path / 'run'
    options = options_for(run_dir, language='java')
    options.iterations = 1
    options.batch = 1
    generation = GenerationStub(options, set())
    artifacts = Artifacts(options)
    compilation = FakeCompilation(
        'java', exception=RuntimeError('evaluation task failed'))
    reporting = Reporting(options, mode=CompilationMode())
    reporting.logging = lambda version: None
    evaluation = Evaluation(options, compilation, artifacts, CoverageStub(),
                            reporting, ManagerStub())
    scheduler = Scheduler(options, generation, evaluation, reporting, JavaCompiler,
                          compiler_settings=None)

    if parallel:
        run_scheduler_with_fake_pool(scheduler, monkeypatch)
    else:
        scheduler.run('fake compiler')

    assert reporting.stats['totals'] == {
        'passed': 0, 'failed': 0, 'inconclusive': 1}
    record = json.loads((run_dir / 'inconclusive.json').read_text())
    assert record['1']['inconclusive_reason'] == 'evaluation_error'


@pytest.mark.parametrize('parallel', [False, True])
def test_keyboard_interrupt_does_not_record_a_completed_batch(
        tmp_path, monkeypatch, parallel):
    run_dir = tmp_path / 'run'
    options = options_for(run_dir, language='java')
    options.iterations = 1
    options.batch = 1
    generation = GenerationStub(options, set())
    artifacts = Artifacts(options)
    compilation = FakeCompilation('java', exception=KeyboardInterrupt())
    reporting = Reporting(options, mode=CompilationMode())
    reporting.logging = lambda version: None
    evaluation = Evaluation(options, compilation, artifacts, CoverageStub(),
                            reporting, ManagerStub())
    scheduler = Scheduler(options, generation, evaluation, reporting, JavaCompiler,
                          compiler_settings=None)

    if parallel:
        run_scheduler_with_fake_pool(scheduler, monkeypatch)
    else:
        scheduler.run('fake compiler')

    assert reporting.stats['totals'] == {
        'passed': 0, 'failed': 0, 'inconclusive': 0}


def test_parallel_worker_cancellation_is_not_a_completed_result(monkeypatch, tmp_path):
    import src.modules.scheduler as scheduler_module

    class InterruptedEvaluation:
        def check_oracle_safely(self, dirname, oracles):
            raise KeyboardInterrupt

    worker = SimpleNamespace(stopped=False, evaluation=InterruptedEvaluation())
    monkeypatch.setattr(scheduler_module, '_worker_scheduler', lambda: worker)

    assert scheduler_module.check_oracle_mul(str(tmp_path), {}) is None
    assert worker.stopped
