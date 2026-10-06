import importlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import pickle
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch

import pytest

from src import utils
from src.compilers.java import JavaCompiler
from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.ir import ast
from src.modules.models import ProgramRes
from src.modules.worker_reporting import Reporting, TimingMetrics
from src.modules.scheduler import (
    Scheduler, check_oracle_mul, gen_program_mul, initialize_scheduler_worker,
)
from src.modules.worker import (
    RunOptions, WorkerInputs, get_worker_resources, snapshot_generator_config,
)
from tests.driver.fixtures import replay
from tests.driver.support import ROOT


COMPONENTS = ('artifacts', 'compilation', 'coverage', 'evaluation', 'generation',
              'models', 'worker_reporting', 'scheduler', 'worker')


def _worker_reporting_observation(dirname: str) -> tuple:
    scheduler = get_worker_resources().scheduler
    assert isinstance(scheduler, Scheduler)
    reporter = scheduler.reporting
    assert type(reporter) is TimingMetrics
    assert not isinstance(reporter, Reporting)
    assert scheduler.evaluation.reporting is reporter
    assert not any(hasattr(reporter, name)
                   for name in ('logging', 'stats', 'update_stats'))
    with ExitStack() as stack:
        parent_methods = [stack.enter_context(patch.object(
            Scheduler, name, side_effect=AssertionError('parent-only ' + name)))
            for name in ('run', 'run_parallel', '_run', '_finish')]
        metrics = stack.enter_context(patch.object(
            reporter, 'get_time_metrics', wraps=reporter.get_time_metrics))
        attach = stack.enter_context(patch.object(
            reporter, 'attach_time_metrics', wraps=reporter.attach_time_metrics))
        result = gen_program_mul(1, dirname)
        assert isinstance(result, ProgramRes)
        assert not result.failed, result.stats
        oracle_result = check_oracle_mul(str(Path(dirname).parent), {1: result})
        metrics.assert_called_once()
        for method in parent_methods:
            method.assert_not_called()
    return os.getpid(), oracle_result, attach.call_count


def _worker_observation(dirname: str) -> tuple:
    resources = get_worker_resources()
    scheduler = resources.scheduler
    assert isinstance(scheduler, Scheduler)
    assert not hasattr(resources, 'runner')
    assert scheduler.generation.manager is resources.manager
    assert scheduler.evaluation.manager is resources.manager
    assert type(scheduler.reporting) is TimingMetrics
    assert scheduler.evaluation.reporting is scheduler.reporting
    assert not any(hasattr(scheduler.reporting, name)
                   for name in ('logging', 'stats', 'update_stats'))
    compilation = scheduler.evaluation.compilation
    assert compilation.settings is resources.compiler_settings
    assert scheduler.compiler_settings is resources.compiler_settings
    result = gen_program_mul(1, dirname)
    compiler = compilation.make_compiler(dirname)
    assert compiler.settings is resources.compiler_settings
    command = compiler.get_compiler_cmd()
    if resources.options.language == 'kotlin':
        observation = compilation.observe(compiler, cwd=str(Path(dirname).parent))
        assert observation.status, observation.output
        assert not observation.diagnostics
        assert observation.command == tuple(command)
    return (result, os.getpid(), snapshot_generator_config(),
            resources.options.language, resources.compiler_cls.__module__,
            type(scheduler.reporting).__name__, hasattr(scheduler.reporting, 'stats'),
            resources.compiler_settings, compilation.version_command(), command)


def test_module_layout_has_no_old_package_or_wrappers() -> None:
    import hephaestus

    assert not (ROOT / 'src/driver').exists()
    assert importlib.util.find_spec('src.driver') is None
    assert not (ROOT / 'src/modules/runner.py').exists()
    assert importlib.util.find_spec('src.modules.runner') is None
    assert not (ROOT / 'src/modules/runtime.py').exists()
    assert importlib.util.find_spec('src.modules.runtime') is None
    assert not (ROOT / 'src/modules/reporting.py').exists()
    assert importlib.util.find_spec('src.modules.reporting') is None
    for name in COMPONENTS:
        module = importlib.import_module('src.modules.' + name)
        assert Path(module.__file__).parent == ROOT / 'src/modules'
    for name in ('Artifacts', 'Compilation', 'LiveCoverage', 'Evaluation',
                 'Generation', 'Reporting', 'Scheduler', 'RunOptions'):
        assert getattr(hephaestus, name).__module__.startswith('src.modules.')
    assert Scheduler.__module__ == 'src.modules.scheduler'
    assert not hasattr(hephaestus, 'Runner')
    assert initialize_scheduler_worker.__module__ == 'src.modules.scheduler'
    assert gen_program_mul.__module__ == 'src.modules.scheduler'
    assert ProgramRes.__module__ == 'src.modules.models'
    assert RunOptions.__module__ == WorkerInputs.__module__ == 'src.modules.worker'
    assert Reporting.__module__ == TimingMetrics.__module__ == 'src.modules.worker_reporting'


def test_module_layout_imports_are_side_effect_free(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(ROOT)
    result = subprocess.run([sys.executable, '-c', '''
import importlib
import sys
from pathlib import Path
from unittest.mock import patch
from src.generators.config import cfg
before = cfg.to_json()
sys.argv = ['unrelated', '--unknown-option']
with patch('subprocess.run', side_effect=AssertionError('tool execution')):
    for name in ('artifacts', 'compilation', 'coverage', 'evaluation', 'generation',
                 'models', 'worker_reporting', 'scheduler', 'worker'):
        importlib.import_module('src.modules.' + name)
    import src.oracles.compilation
assert cfg.to_json() == before
assert sys.argv == ['unrelated', '--unknown-option']
assert list(Path.cwd().iterdir()) == []
assert 'hephaestus' not in sys.modules
assert not any(name == 'src.driver' or name.startswith('src.driver.')
               for name in sys.modules)
'''], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('language, compiler_cls, settings', [
    ('java', JavaCompiler, None),
    ('kotlin', KotlinCompiler, KotlinSettings('wasm', True, '/custom/kotlinc')),
])
def test_module_layout_result_and_worker_inputs_pickle_round_trip(language, compiler_cls, settings) -> None:
    from src.args import parse_args

    result = ProgramRes(False, {'time': 0.5, 'programs': {'Main.java': True}},
                        3, ['/provider.klib'])
    assert pickle.loads(pickle.dumps(result)) == result
    inputs = WorkerInputs(RunOptions.from_namespace(parse_args(['--language', language])),
                          snapshot_generator_config(), 'hermetic', compiler_cls, settings)
    restored = pickle.loads(pickle.dumps(inputs))
    assert type(restored) is WorkerInputs
    assert vars(restored.options) == vars(inputs.options)
    assert restored.generator_config == inputs.generator_config
    assert restored.compiler_cls is compiler_cls
    assert restored.compiler_settings == settings


@pytest.mark.parametrize('method', ['spawn', 'fork'])
@pytest.mark.parametrize('language, backend', [
    ('java', 'native'), ('kotlin', 'native'), ('kotlin', 'js'), ('kotlin', 'wasm'),
])
def test_module_layout_restores_workers_and_returns_real_programs(
        tmp_path: Path, method: str, language: str, backend: str,
        monkeypatch: pytest.MonkeyPatch) -> None:
    from src.args import parse_args
    from hephaestus import Fuzzer

    source = tmp_path / 'input.bin'
    replay(source, language)
    options = RunOptions.from_namespace(parse_args([
        '--language', language, '--backend', backend, '--dump-ir', '-P', '--replay', str(source),
        '-b', str(tmp_path / 'bugs'), '-n', 'session',
    ]))
    tool = tmp_path / 'kotlinc'
    tool.write_text('#!' + sys.executable + '\n' +
                    (ROOT / 'tests/driver/fake_compiler.py').read_text())
    tool.chmod(0o755)
    command_log = tmp_path / 'commands.jsonl'
    monkeypatch.setenv('DRIVER_TOOL_LOG', str(command_log))
    monkeypatch.setenv('DRIVER_COMPILER_MODE', 'success')
    before = snapshot_generator_config()
    configuration = snapshot_generator_config()
    limits, probabilities = configuration['limits'], configuration['prob']
    assert isinstance(limits, dict) and isinstance(probabilities, dict)
    limits['max_depth'] = 7
    probabilities['crossmodule_probability'] = 0.0
    fuzzer = Fuzzer(options)
    settings = fuzzer.compilation.settings
    if language == 'kotlin':
        assert settings == KotlinSettings(backend, True)
        settings = replace(settings, executable=str(tool))
        fuzzer.compilation.settings = settings
        fuzzer.scheduler.compiler_settings = settings
    inputs = replace(fuzzer.scheduler.worker_inputs('hermetic'), generator_config=configuration)
    directory = tmp_path / 'batch/src'
    with mp.get_context(method).Pool(
            1, initializer=initialize_scheduler_worker, initargs=(inputs,)) as pool:
        (result, pid, restored, worker_language, compiler_module, reporter,
         has_stats, worker_settings, version, command) = (
            pool.apply_async(_worker_observation, (str(directory),)).get(timeout=20))
    assert pid != os.getpid()
    assert restored == configuration
    assert snapshot_generator_config() == before
    assert worker_language == language
    assert compiler_module == 'src.compilers.' + language
    assert worker_settings == settings
    if language == 'kotlin':
        assert isinstance(settings, KotlinSettings)
        expected = KotlinCompiler(str(directory), settings=settings)
        assert command == expected.get_compiler_cmd()
        assert version == KotlinCompiler.get_compiler_version(settings)
        trace, = [json.loads(line) for line in command_log.read_text().splitlines()]
        assert trace['arguments'] == [os.path.expandvars(arg) for arg in command[1:]]
    else:
        assert worker_settings is None
        assert command == JavaCompiler(str(directory)).get_compiler_cmd()
        assert version == JavaCompiler.get_compiler_version()
    assert reporter == 'TimingMetrics'
    assert not has_stats
    assert type(result) is ProgramRes
    assert not result.failed, result.stats
    filename = 'program.kt' if language == 'kotlin' else 'Main.java'
    program_file = directory / 'd1' / filename
    assert result.stats['programs'] == {str(program_file): True}
    assert program_file.is_file()
    program = utils.load_program(str(program_file) + '.bin')
    assert isinstance(program, ast.Program)
    assert ast.Program.__module__ == 'src.ir.ast'
    assert program.language == language
    assert (tmp_path / 'bugs/session/tmp/1' / (filename + '.bin')).is_file()


@pytest.mark.parametrize('method', ['spawn', 'fork'])
@pytest.mark.parametrize('mode', ['success', 'reject'])
def test_scheduler_worker_tasks_use_only_timing_reporting(
        tmp_path: Path, method: str, mode: str,
        monkeypatch: pytest.MonkeyPatch) -> None:
    from src.args import parse_args
    from hephaestus import Fuzzer

    source = tmp_path / 'input.bin'
    replay(source, 'java')
    compiler = tmp_path / 'javac'
    compiler.write_text('#!' + sys.executable + '\n' +
                        (ROOT / 'tests/driver/fake_compiler.py').read_text())
    compiler.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('DRIVER_COMPILER_MODE', mode)
    monkeypatch.chdir(tmp_path)
    fuzzer = Fuzzer(parse_args([
        '--language', 'java', '-P', '--replay', str(source), '--time-metrics',
        '--disable-metrics', '-b', str(tmp_path / 'bugs'), '-n', 'session',
    ]))
    configuration = snapshot_generator_config()
    configuration['prob']['crossmodule_probability'] = 0.0
    inputs = replace(fuzzer.scheduler.worker_inputs('hermetic'),
                     generator_config=configuration)
    with mp.get_context(method).Pool(
            1, initializer=initialize_scheduler_worker, initargs=(inputs,)) as pool:
        pid, oracle_result, attachments = pool.apply_async(
            _worker_reporting_observation, ('batch/src',)
        ).get(timeout=20)
    assert pid != os.getpid()
    faults, compilation_time, time_metrics, published = oracle_result
    assert set(faults) == ({1} if mode == 'reject' else set())
    assert attachments == (1 if mode == 'reject' else 0)
    assert compilation_time >= 0
    assert set(time_metrics) == {1}
    assert time_metrics[1]['compilation_with_ir_dumps'] == compilation_time
    if mode == 'reject':
        assert faults[1]['time_metrics'] == time_metrics[1]
    assert published == {}
    assert type(fuzzer.scheduler.reporting) is Reporting
    assert fuzzer.reporting.stats['totals'] == {'passed': 0, 'failed': 0}