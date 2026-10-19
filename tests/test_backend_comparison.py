from dataclasses import replace
import multiprocessing as mp
import pickle
from pathlib import Path
import sys

import pytest

from hephaestus import Fuzzer, configure_generator
from src.args import parse_args, validate_args
from src.compilers.kotlin import KOTLIN_COMPARISON_VARIANTS, KotlinCompiler, KotlinSettings
from src.compilers.results import (
    CommandObservation, ParsedDiagnostics, ProcessResult, VariantObservation,
)
from src.generators.config import cfg
from src.modules.compilation import ORDINARY_VARIANT
from src.modules.worker import RunOptions, initialize_worker
from src.modules.scheduler import _create_worker_scheduler
from src.modules.oracle_modes import (
    BackendComparisonMode, CompilationMode, create_oracle_mode,
)


def application(tmp_path, extra=()):
    return Fuzzer(parse_args(['-P', '-t', '0', '--batch', '1', '--oracle',
                             'backend-comparison', '-b', str(tmp_path), '-n', 'case',
                              *extra]))


def configure_variants(app, tool, versions=None):
    app.compilation.settings_by_variant = {
        variant_id: replace(settings, executable=str(tool))
        for variant_id, settings in app.compilation.settings_by_variant.items()}
    if versions is not None:
        app.compilation.versions_by_variant = dict(versions)


@pytest.mark.parametrize('extra,message', [
    (['--language', 'java'], 'requires Kotlin'),
    (['--batch', '2'], 'requires --batch 1'),
    (['--rerun'], 'does not support'),
    (['--dump-ir'], 'does not support'),
    (['--jacoco-always'], 'does not support'),
])
def test_comparison_restrictions(tmp_path, monkeypatch, extra, message):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    with pytest.raises(SystemExit, match=message):
        validate_args(application(tmp_path, extra).options)


def test_defaults_internal_paths_and_crossmodule_configuration(tmp_path, monkeypatch):
    assert parse_args([]).oracle == 'compilation'
    # The selection layer chooses the preset; Compilation owns the selected mapping.
    assert application(tmp_path).compilation.settings_by_variant == (
        KOTLIN_COMPARISON_VARIANTS)
    ordinary = Fuzzer(parse_args(['-b', str(tmp_path), '-n', 'ordinary']))
    assert ordinary.compilation.settings_by_variant == {
        ORDINARY_VARIANT: ordinary.compilation.settings}
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0.7)
    with pytest.raises(SystemExit, match='requires --keep-all'):
        validate_args(application(tmp_path).options)
    app = application(tmp_path, ['--keep-all'])
    validate_args(app.options)
    assert cfg.prob.crossmodule_probability == 0.7
    assert app.options.backend == 'native'


def test_standalone_comparison_does_not_require_keep_all(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    validate_args(application(tmp_path).options)


def test_comparison_preserves_kotlin_crossmodule_probability(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0.5)
    configure_generator(application(tmp_path).options)
    assert cfg.prob.crossmodule_probability == 0.5


def test_comparison_generator_config_opts_into_crossmodule_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0.5)
    config = tmp_path / 'generator.json'
    config.write_text('{"prob": {"crossmodule_probability": 0.7}}')
    configure_generator(application(tmp_path, ['--generator-config', str(config)]).options)
    assert cfg.prob.crossmodule_probability == 0.7


def test_comparison_generator_config_selects_standalone_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0.5)
    config = tmp_path / 'generator.json'
    config.write_text('{"prob": {"crossmodule_probability": 0.0}}')
    app = application(tmp_path, ['--generator-config', str(config)])
    configure_generator(app.options)
    validate_args(app.options)
    assert cfg.prob.crossmodule_probability == 0


def fixture_tool(tmp_path):
    tool = tmp_path / 'compiler'
    tool.write_text('#!' + sys.executable + '\n' +
        "import sys, pathlib, zipfile\n"
        "a = sys.argv[1:]\n"
        "if '-version' in a:\n    print('Kotlin compiler 2.4.255-SNAPSHOT'); sys.exit(0)\n"
        "if '-o' in a: p = pathlib.Path(a[a.index('-o')+1] + '.klib')\n"
        "else: p = pathlib.Path(a[a.index('-ir-output-dir')+1]) / (a[a.index('-ir-output-name')+1] + '.klib')\n"
        "with zipfile.ZipFile(p, 'w') as z: z.writestr('default/manifest', 'depends=stdlib\\n')\n")
    tool.chmod(0o755)
    return tool


def test_preflight_produces_three_dependency_artifacts_and_versions(tmp_path, monkeypatch):
    app = application(tmp_path)
    tool = fixture_tool(tmp_path)
    library = tmp_path / 'stdlib'
    library.mkdir()
    monkeypatch.setattr(KotlinCompiler, 'stdlib_path', lambda self: str(library))
    configure_variants(app, tool)
    commands = []
    run_command = app.compilation.run_command

    def recorded(arguments, get_stdout=True, cwd=None):
        commands.append(arguments)
        return run_command(arguments, get_stdout, cwd)

    monkeypatch.setattr(app.compilation, 'run_command', recorded)
    app.mode.preflight(app.compilation)
    versions = app.compilation.versions_by_variant
    assert set(versions) == {'js', 'native', 'wasm'}
    assert all(version == 'Kotlin compiler 2.4.255-SNAPSHOT' for version in versions.values())
    # One probe per variant, prepared by the compiler for its backend.
    assert [('-ir-output-name' in command) for command in commands] == [True, False, True]
    tool.unlink()
    with pytest.raises(ValueError, match='missing compiler executable'):
        app.mode.preflight(app.compilation)


def test_startup_failure_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    app = application(tmp_path)
    configure_variants(app, tmp_path / 'missing-compiler')
    with pytest.raises(SystemExit, match='missing compiler executable'):
        app.start()


def worker_snapshot(inputs, queue):
    resources = initialize_worker(inputs, scheduler_factory=_create_worker_scheduler)
    compilation = resources.scheduler.evaluation.compilation
    queue.put((dict(resources.settings_by_variant), dict(resources.versions_by_variant),
               dict(compilation.settings_by_variant), dict(compilation.versions_by_variant),
               dict(resources.manager.settings_by_variant),
               dict(resources.manager.versions_by_variant), resources.mode.name))


@pytest.mark.parametrize('method', [method for method in ('spawn', 'fork')
                                    if method in mp.get_all_start_methods()])
def test_worker_settings_transport(tmp_path, method):
    app = application(tmp_path)
    # Two variants share the native backend and differ only in their settings.
    selected = {'native-a': KotlinSettings('native', executable='/a/kotlinc-native'),
                'native-b': KotlinSettings('native', executable='/b/kotlinc-native'),
                'js': KotlinSettings('js')}
    app.compilation.settings_by_variant = selected
    app.compilation.versions_by_variant = {
        variant_id: variant_id + ' full version' for variant_id in selected}
    inputs = pickle.loads(pickle.dumps(app.scheduler.worker_inputs('full version')))
    context = mp.get_context(method)
    queue = context.Queue()
    process = context.Process(target=worker_snapshot, args=(inputs, queue))
    process.start()
    (settings, versions, compilation_settings, compilation_versions,
     manager_settings, manager_versions, mode_name) = queue.get(timeout=20)
    process.join(timeout=20)
    assert process.exitcode == 0
    assert settings == compilation_settings == manager_settings == selected
    assert versions == compilation_versions == manager_versions == {
        variant_id: variant_id + ' full version' for variant_id in selected}
    assert mode_name == app.mode.name
    queue.close()


def test_preflight_reuses_compilation_construction(tmp_path, monkeypatch):
    library = tmp_path / 'stdlib'
    library.mkdir()
    monkeypatch.setattr(KotlinCompiler, 'stdlib_path', lambda self: str(library))
    monkeypatch.setattr('src.compilers.kotlin.shutil.which', lambda executable: executable)
    app = application(tmp_path)
    requests = []

    def run_variants(variant_requests):
        requests.extend(variant_requests)
        return tuple(VariantObservation(
            request.variant_id,
            CommandObservation(
                ProcessResult(0, ''), {}, ParsedDiagnostics(), (), None,
                tuple(request.compiler.get_compiler_cmd()),
                request.working_directory, 0.0),
            request.expected_dependency_artifact_path, request.expected_dependency_artifact_path,
            settings=request.compiler.settings)
            for request in variant_requests)

    monkeypatch.setattr(app.compilation, 'run_command', lambda arguments, get_stdout=True,
                        cwd=None: (True, 'Kotlin compiler version 2.4.255-SNAPSHOT\n'))
    monkeypatch.setattr(app.compilation, 'run_variants', run_variants)
    app.mode.preflight(app.compilation)
    assert [request.variant_id for request in requests] == ['js', 'native', 'wasm']
    assert all(isinstance(request.compiler, KotlinCompiler)
               and request.compiler.klib_output for request in requests)
    assert [request.compiler.settings for request in requests] == list(
        KOTLIN_COMPARISON_VARIANTS.values())
    assert app.compilation.versions_by_variant == dict.fromkeys(
        KOTLIN_COMPARISON_VARIANTS, 'Kotlin compiler version 2.4.255-SNAPSHOT')

    # The JS and Wasm launchers report another version than Native.
    monkeypatch.setattr(app.compilation, 'run_command', lambda arguments, get_stdout=True,
                        cwd=None: (True, '2.4.1' if '-ir-output-name' in arguments else '2.4.2'))
    with pytest.raises(ValueError, match='versions differ'):
        app.mode.preflight(app.compilation)
    monkeypatch.setattr(app.compilation, 'run_command', lambda arguments, get_stdout=True,
                        cwd=None: (False, 'broken launcher'))
    with pytest.raises(ValueError, match='js: version probe failed: broken launcher'):
        app.mode.preflight(app.compilation)
    monkeypatch.setattr(app.compilation, 'run_command', lambda arguments, get_stdout=True,
                        cwd=None: (True, 'no version here'))
    with pytest.raises(ValueError, match='cannot identify compiler version'):
        app.mode.preflight(app.compilation)


@pytest.mark.parametrize('arguments,mode_type,name,counts,compares', [
    ([], CompilationMode, 'compilation', True, False),
    (['--oracle', 'backend-comparison'], BackendComparisonMode,
     'backend-comparison', True, True),
])
def test_oracle_mode_selection_and_pickling(
        arguments, mode_type, name, counts, compares):
    options = RunOptions.from_namespace(parse_args(arguments))
    mode = create_oracle_mode(options)
    assert isinstance(mode, mode_type)
    assert mode.name == name
    assert mode.counts_inconclusive() is counts
    assert mode.compares_variants() is compares
    assert pickle.loads(pickle.dumps(mode)) == mode