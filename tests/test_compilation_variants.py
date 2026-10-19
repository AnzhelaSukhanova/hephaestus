import json
import os
import pickle
import sys
from pathlib import Path

import pytest

from src.args import parse_args
from src.compilers.base import BaseCompiler
from src.compilers.groovy import GroovyCompiler
from src.compilers.java import JavaCompiler
from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.compilers.results import (
    CommandObservation, CompilationRequest, ParsedDiagnostics, ProcessResult,
)
from src.compilers.scala import ScalaCompiler
from src.generators.config import cfg
from src.modules.artifacts import Artifacts
from src.modules.compilation import ORDINARY_VARIANT, Compilation
from src.modules import compilation as compilation_module
from src.modules.coverage import LiveCoverage
from src.modules.evaluation import Evaluation
from src.modules.models import OracleResult, ProgramRes
from src.modules.worker import RunOptions, create_manager
from src.modules.worker_reporting import TimingMetrics


def components(tmp_path):
    options = RunOptions.from_namespace(parse_args([
        '--language', 'kotlin', '-b', str(tmp_path), '-n', 'run',
        '--disable-metrics', '--time-metrics']))
    artifacts = Artifacts(options)
    compilation = Compilation(options, artifacts, {'kotlin': KotlinCompiler},
                              settings=KotlinSettings())
    return options, artifacts, compilation


def command_observation(status, output, diagnostics, crash, command, cwd, elapsed):
    return CommandObservation(
        ProcessResult(0 if status else 1, output), diagnostics,
        ParsedDiagnostics(), (), crash, tuple(command), cwd, elapsed)


def test_named_runner_collects_each_attempt(tmp_path, monkeypatch):
    _, _, compilation = components(tmp_path)
    attempts = []

    def observe(compiler, cwd=None):
        attempts.append((compiler, cwd))
        return command_observation(False, 'error', {}, None, ('compiler',), cwd, 2)

    monkeypatch.setattr(compilation, 'observe', observe)
    requests = tuple(CompilationRequest(name, compilation.make_compiler(name), name)
                     for name in ('js', 'native', 'wasm'))
    results = compilation.run_variants(requests)
    assert tuple(result.variant_id for result in results) == ('js', 'native', 'wasm')
    assert attempts == [(request.compiler, request.working_directory)
                        for request in requests]
    assert len({id(compiler) for compiler, _ in attempts}) == 3
    assert tuple(result.settings for result in results) == tuple(
        request.compiler.settings for request in requests)
    assert sum(result.command.elapsed for result in results) == 6


def test_blocked_attempt_retains_its_settings(tmp_path, monkeypatch):
    _, _, compilation = components(tmp_path)
    monkeypatch.setattr(compilation, 'observe', lambda *args, **kwargs: pytest.fail(
        'A blocked attempt must not be compiled'))
    compiler = compilation.make_compiler('program.kt')

    observation, = compilation.run_variants((CompilationRequest(
        'blocked-variant', compiler, blocked_reason='Missing dependency'),))

    assert observation.command is None
    assert observation.incomplete_reason == 'Missing dependency'
    assert observation.settings is compiler.settings


def test_compilation_owns_selected_settings_and_versions(tmp_path):
    options, artifacts, compilation = components(tmp_path)
    assert compilation.settings_by_variant == {ORDINARY_VARIANT: compilation.settings}
    assert compilation.versions_by_variant == {}

    class Injected(KotlinCompiler):
        pass

    shared = {'native-a': KotlinSettings('native', executable='/a/kotlinc'),
              'native-b': KotlinSettings('native', executable='/b/kotlinc')}
    selected = Compilation(options, artifacts, {'kotlin': Injected}, settings=KotlinSettings(),
                           settings_by_variant=shared, versions_by_variant={'native-a': '1'})
    assert selected.settings_by_variant == shared
    assert selected.settings_by_variant is not shared
    assert selected.versions_by_variant == {'native-a': '1'}
    for variant_id, settings in selected.settings_by_variant.items():
        assert settings is shared[variant_id]
        compiler = selected.make_compiler('src', module_name='d1', settings=settings)
        assert type(compiler) is Injected
        assert compiler.settings is settings
    assert pickle.loads(pickle.dumps(selected.settings_by_variant)) == shared


def test_variant_outputs_are_validated_by_their_compiler(tmp_path, monkeypatch):
    _, _, compilation = components(tmp_path)
    artifact = tmp_path / 'd1.klib'

    def observe(compiler, cwd=None):
        (artifact / 'default').mkdir(parents=True)
        (artifact / 'default' / 'manifest').write_text('depends=stdlib\n')
        return command_observation(True, '', {}, None, ('compiler',), cwd, 1)

    monkeypatch.setattr(compilation, 'observe', observe)
    kotlin, = compilation.run_variants((CompilationRequest(
        'native', KotlinCompiler('src', module_name='d1'), str(tmp_path), str(artifact)),))
    assert kotlin.dependency_artifact_valid
    assert kotlin.produced_dependency_artifact_path == str(artifact)
    assert kotlin.incomplete_reason is None
    generic, = compilation.run_variants((CompilationRequest(
        'native', BaseCompiler('src', settings=None), str(tmp_path), str(artifact)),))
    assert not generic.dependency_artifact_valid
    assert generic.produced_dependency_artifact_path is None
    assert generic.incomplete_reason.startswith('Successful compilation did not produce')


@pytest.mark.parametrize('expected,diagnostics,crash,fault', [
    (True, {}, None, False),
    (False, {'source': ['error']}, None, False),
    (True, {'source': ['error']}, None, True),
    (False, {}, None, True),
    (True, {}, 'crash', True),
])
def test_legacy_evaluation_preserves_verdicts_and_timings(
        tmp_path, monkeypatch, expected, diagnostics, crash, fault):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    options, artifacts, compilation = components(tmp_path)
    batch = tmp_path / 'batch'
    (batch / 'src').mkdir(parents=True)
    source = str(batch / 'src' / 'program.kt')
    Path(source).write_text('val answer = 1')
    retained = Path(options.test_directory) / 'tmp' / '1'
    retained.mkdir(parents=True)
    (retained / 'program.kt').write_text('val answer = 1')
    errors = {source: diagnostics['source']} if diagnostics else {}
    monkeypatch.setattr(compilation, 'observe', lambda compiler, cwd=None:
                        command_observation(not errors, 'output', errors, crash,
                                            ('compiler',), cwd, 3.5))
    evaluation = Evaluation(options, compilation, artifacts,
                            LiveCoverage(options, compilation), TimingMetrics(options),
                            create_manager(options, 'test', KotlinCompiler))
    result = evaluation.check_oracle(str(batch), {
        1: ProgramRes(False, {'programs': {source: expected}, 'error': 'mutation'})})
    assert bool(result.faults) == fault
    assert result.compilation_time == 3.5
    assert result.time_metrics[1]['compilation_with_ir_dumps'] == 3.5
    assert result.inconclusive == result.publications == {}
    assert (Path(options.test_directory) / '1' / 'program.kt').exists() == fault
    assert not batch.exists()


def test_result_envelope_round_trips():
    result = OracleResult({1: {'error': 'fault'}}, 1.5, {1: {'elapsed': 1.5}},
                          {}, {2: {'reason': 'unavailable'}})
    assert pickle.loads(pickle.dumps(result)) == result


def test_command_observation_exposes_composed_result_properties():
    parsed = ParsedDiagnostics(complete=False, unparsed=('opaque output',))
    observation = CommandObservation(
        ProcessResult(None, 'launch failed', 'missing compiler'), {}, parsed,
        (), None, ('compiler',), None, 1.0)
    assert not observation.status
    assert observation.output == 'launch failed'
    assert observation.returncode is None
    assert observation.launch_failure == 'missing compiler'
    assert not observation.parsing_complete
    assert observation.unparsed == ('opaque output',)
    assert not observation.succeeded()


def test_runner_always_uses_shell_command(tmp_path, monkeypatch):
    _, _, compilation = components(tmp_path)
    calls = []

    class Process:
        returncode = 0

        @staticmethod
        def communicate():
            return b'', None

    def popen(command, *, shell, **kwargs):
        calls.append((command, shell))
        return Process()

    monkeypatch.setattr(compilation_module.sp, 'Popen', popen)
    compilation.run_process(['compiler', 'source with spaces'])
    assert calls == [('compiler source with spaces', True)]


@pytest.mark.parametrize(('language', 'name', 'compiler_type', 'extension'), [
    ('java', 'javac', JavaCompiler, 'java'),
    ('scala', 'scalac', ScalaCompiler, 'scala'),
    ('groovy', 'groovyc-l', GroovyCompiler, 'groovy'),
])
def test_shell_runner_expands_language_source_globs(
        tmp_path, monkeypatch, language, name, compiler_type, extension):
    if os.name == 'nt':
        pytest.skip('Compiler adapter wildcards rely on POSIX shell expansion')

    options, artifacts, _ = components(tmp_path)
    compilation = Compilation(
        options, artifacts, {language: compiler_type}, settings=None)
    source_root = tmp_path / 'sources'
    expected_sources = []
    for module in ('first', 'second'):
        module_dir = source_root / module
        module_dir.mkdir(parents=True)
        source = module_dir / f'Main.{extension}'
        source.write_text('class Main {}')
        expected_sources.append(str(source))

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake_compiler = bin_dir / name
    fake_compiler.write_text(
        f'#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
    fake_compiler.chmod(0o755)
    monkeypatch.setenv(
        'PATH', str(bin_dir) + os.pathsep + os.environ.get('PATH', ''))

    compiler = compiler_type(str(source_root), settings=None)
    result = compilation.run_process(compiler.get_compiler_cmd())

    assert result.status
    actual_arguments = json.loads(result.output)
    assert set(expected_sources).issubset(actual_arguments)
    assert not any('*' in argument for argument in actual_arguments)
