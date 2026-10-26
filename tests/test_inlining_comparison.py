"""Inlining comparison: one KLIB backend with the IR inliner disabled versus cross-module."""
import json
import os
from pathlib import Path
import pickle

import pytest

from hephaestus import Fuzzer
from src.args import parse_args, validate_args
from src.compilers.kotlin import KotlinCompiler, KotlinSettings, _compiler_executable
from src.generators.config import cfg
from src.modules.oracle_modes import InliningComparisonMode
from tests.test_backend_comparison import configure_variants, fixture_tool
from tests.test_crossmodule_backend_comparison import case, commit
from tests.test_kotlin_real_backend_comparison import compile_source


KLIB_BACKENDS = ('js', 'native', 'wasm')
VARIANT_IDS = ('disabled', 'crossmodule')
INLINER_MODES = {'disabled': 'disabled', 'crossmodule': 'full'}
VERSION = 'Kotlin compiler 2.4.255-SNAPSHOT'


def inlining_application(tmp_path, backend='native', extra=()):
    return Fuzzer(parse_args(['-P', '-t', '0', '--batch', '1', '--oracle',
                              'inlining-comparison', '--backend', backend,
                              '-b', str(tmp_path), '-n', 'case', *extra]))


def inliner_tool(tmp_path):
    """The fixture compiler, rejecting only the inliner mode named by REJECT_INLINER."""
    tool = fixture_tool(tmp_path)
    tool.write_text(tool.read_text().replace('a = sys.argv[1:]\n',
        'a = sys.argv[1:]\n'
        'import os\n'
        "if '-Xklib-ir-inliner=' + os.environ.get('REJECT_INLINER', '') in a:\n"
        "    print('program.kt:1:1: error: controlled rejection'); sys.exit(1)\n"))
    return tool


def configured_inlining_application(tmp_path, monkeypatch, tool=None):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 1)
    monkeypatch.setattr(cfg.prob, 'max_module_chain_depth', 0)
    monkeypatch.setattr(cfg.prob, 'drop_indirect_deps_prob', 0)
    app = inlining_application(tmp_path)
    configure_variants(app, tool or inliner_tool(tmp_path), dict.fromkeys(VARIANT_IDS, VERSION))
    manager = app.setup_crossmodule_manager('versions')
    return app, manager


@pytest.mark.parametrize('backend', KLIB_BACKENDS)
def test_variants_compare_inliner_modes_on_the_selected_backend(tmp_path, backend):
    app = inlining_application(tmp_path, backend)
    expected = {'disabled': KotlinSettings(backend, inlining_mode='disabled'),
                'crossmodule': KotlinSettings(backend, inlining_mode='full')}

    assert app.mode == InliningComparisonMode(backend=backend)
    assert app.mode.compares_variants()
    assert app.mode.variant_settings() == expected
    assert tuple(app.compilation.settings_by_variant) == VARIANT_IDS
    assert app.compilation.settings_by_variant == expected


@pytest.mark.parametrize('backend', KLIB_BACKENDS)
def test_worker_inputs_restore_mode_and_variants(tmp_path, backend):
    app = inlining_application(tmp_path, backend)

    inputs = pickle.loads(pickle.dumps(app.scheduler.worker_inputs('version')))

    assert inputs.mode == app.mode == InliningComparisonMode(backend=backend)
    assert inputs.mode.variant_settings() == app.mode.variant_settings()
    assert inputs.settings_by_variant == app.compilation.settings_by_variant


@pytest.mark.parametrize('backend,extra,message', [
    ('native', ['--language', 'java'], 'inlining-comparison requires Kotlin'),
    ('native', ['--batch', '2'], 'inlining-comparison requires --batch 1'),
    ('native', ['--rerun'], 'inlining-comparison does not support'),
    ('native', ['--dump-ir'], 'inlining-comparison does not support'),
    ('native', ['--jacoco-always'], 'inlining-comparison does not support'),
    ('jvm', [], 'inlining-comparison requires --backend js, native or wasm'),
])
def test_inlining_comparison_restrictions(tmp_path, monkeypatch, backend, extra, message):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    with pytest.raises(SystemExit, match=message):
        validate_args(inlining_application(tmp_path, backend, extra).options)


@pytest.mark.parametrize('backend', KLIB_BACKENDS)
def test_klib_backends_pass_validation(tmp_path, monkeypatch, backend):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    validate_args(inlining_application(tmp_path, backend).options)


@pytest.mark.parametrize('backend', KLIB_BACKENDS)
def test_requests_differ_in_inliner_mode_and_destination(tmp_path, monkeypatch, backend):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    app = inlining_application(tmp_path, backend)
    app.setup_crossmodule_manager('version')
    directory, program = case(app, tmp_path, 1)

    requests, _ = app.mode._prepare_requests(app.evaluation, directory, 1, program)

    assert tuple(request.variant_id for request in requests) == VARIANT_IDS
    commands = {request.variant_id: request.compiler.get_compiler_cmd() for request in requests}
    for variant_id, mode in INLINER_MODES.items():
        assert [argument for argument in commands[variant_id]
                if argument.startswith('-Xklib-ir-inliner')] == ['-Xklib-ir-inliner=' + mode]
    # Both variants run the same compiler executable of the selected backend.
    assert commands['disabled'][0] == commands['crossmodule'][0]
    assert commands['disabled'][0].endswith('/kotlinc-' + backend)
    for request in requests:
        assert Path(request.working_directory) == (
            Path(directory) / 'variants' / request.variant_id)


def test_preflight_builds_a_fixture_per_inliner_mode(tmp_path, monkeypatch):
    library = tmp_path / 'stdlib'
    library.mkdir()
    monkeypatch.setattr(KotlinCompiler, 'stdlib_path', lambda self: str(library))
    app = inlining_application(tmp_path)
    configure_variants(app, inliner_tool(tmp_path))
    built = []
    run_variants = app.compilation.run_variants

    def recorded(requests):
        built.extend((request.variant_id, request.compiler.get_compiler_cmd())
                     for request in requests)
        return run_variants(requests)

    monkeypatch.setattr(app.compilation, 'run_variants', recorded)
    app.mode.preflight(app.compilation)

    assert [variant_id for variant_id, _ in built] == list(VARIANT_IDS)
    for variant_id, command in built:
        assert '-Xklib-ir-inliner=' + INLINER_MODES[variant_id] in command
    assert app.compilation.versions_by_variant == dict.fromkeys(VARIANT_IDS, VERSION)
    # A mode the compiler does not accept fails before fuzzing starts.
    monkeypatch.setenv('REJECT_INLINER', 'disabled')
    with pytest.raises(ValueError, match='disabled: KLIB preflight failed'):
        app.mode.preflight(app.compilation)


def test_rejection_by_one_inliner_mode_is_a_fault_with_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv('REJECT_INLINER', 'full')
    app, _ = configured_inlining_application(tmp_path, monkeypatch)
    directory, program = case(app, tmp_path, 1)

    result = app.evaluation.check_oracle(directory, {1: program})
    app.reporting.update_stats(result, 1, 0)

    assert set(result.faults) == {1}
    assert not result.inconclusive and not result.publications
    fault = result.faults[1]
    assert fault['oracle'] == 'inlining-comparison'
    comparison = fault['comparison']
    assert [finding['kind'] for finding in comparison['findings']] == ['status_mismatch']
    assert comparison['groups']['accepted'] == ('disabled',)
    assert comparison['groups']['rejected'] == ('crossmodule',)
    run = Path(app.options.test_directory)
    assert (run / '1' / 'comparison.json').exists()
    for variant_id, mode in INLINER_MODES.items():
        evidence = json.loads((run / '1' / 'variants' / variant_id / 'result.json').read_text())
        assert evidence['variant_id'] == variant_id
        assert evidence['settings']['inlining_mode'] == mode
        assert evidence['status'] is (variant_id == 'disabled')
        assert '-Xklib-ir-inliner=' + mode in evidence['reproduce_argv']
    assert json.loads((run / 'faults.json').read_text())['1']['oracle'] == 'inlining-comparison'
    assert '## 1' in (run / 'divergences.md').read_text()
    assert app.reporting.stats['totals'] == {'passed': 0, 'failed': 1, 'inconclusive': 0}


def test_agreement_passes_without_retaining_artifacts(tmp_path, monkeypatch):
    app, _ = configured_inlining_application(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    directory, program = case(app, tmp_path, 1)

    result = app.evaluation.check_oracle(directory, {1: program})

    assert not result.faults and not result.inconclusive and not result.publications
    run = Path(app.options.test_directory)
    assert not (run / '1' / 'program.kt').exists()
    assert not (run / 'klibs').exists()


def test_crossmodule_chain_links_only_klibs_of_the_same_inliner_mode(tmp_path, monkeypatch):
    app, manager = configured_inlining_application(tmp_path, monkeypatch)
    directory, provider = case(app, tmp_path, 1)

    provider_result = app.evaluation.check_oracle(directory, {1: provider})

    assert not provider_result.faults and not provider_result.inconclusive
    record = provider_result.publications[1]
    assert set(record['variants']) == set(VARIANT_IDS)
    run = Path(app.options.test_directory)
    for variant_id, mode in INLINER_MODES.items():
        publication = record['variants'][variant_id]
        assert publication['backend'] == 'native'
        assert (run / 'klibs' / f'd1-{variant_id}.klib').is_file()
        assert Path(manager.published_klib_path(1, variant_id)) == (
            run / 'klibs' / f'd1-{variant_id}.klib')
        metadata = json.loads((run / publication['metadata']).read_text())
        assert metadata['settings']['inlining_mode'] == mode
    commit(app, provider_result)
    assert manager.provider_candidates(2) == [1]

    commands = {}
    observe = app.compilation.observe

    def recorded(compiler, cwd=None):
        commands[compiler.settings.inlining_mode] = compiler.get_compiler_cmd()
        return observe(compiler, cwd=cwd)

    monkeypatch.setattr(app.compilation, 'observe', recorded)
    directory, consumer = case(app, tmp_path, 2, direct=1, closure=[1])
    consumer_result = app.evaluation.check_oracle(directory, {2: consumer})

    assert not consumer_result.faults and not consumer_result.inconclusive
    assert set(commands) == set(INLINER_MODES.values())
    for variant_id, mode in INLINER_MODES.items():
        command = commands[mode]
        expected = str(Path(manager.published_klib_path(1, variant_id)).resolve())
        libraries = [command[index + 1]
                     for index, argument in enumerate(command) if argument == '-library']
        assert libraries == [expected]
        assert command[command.index('-friend-modules') + 1] == expected
        assert '-Xklib-ir-inliner=' + mode in command


def test_klib_recorded_with_another_inliner_mode_is_not_a_provider(tmp_path, monkeypatch):
    app, manager = configured_inlining_application(tmp_path, monkeypatch)
    directory, provider = case(app, tmp_path, 1)
    commit(app, app.evaluation.check_oracle(directory, {1: provider}))
    assert manager.provider_candidates(2) == [1]

    metadata = Path(app.options.test_directory) / 'klibs' / 'd1-disabled.meta.json'
    recorded = json.loads(metadata.read_text())
    recorded['settings']['inlining_mode'] = 'full'
    metadata.write_text(json.dumps(recorded))

    assert manager.provider_candidates(2) == []


def test_missing_executable_during_evaluation_is_inconclusive(tmp_path, monkeypatch):
    app, _ = configured_inlining_application(tmp_path, monkeypatch, tmp_path / 'missing-kotlinc')
    directory, program = case(app, tmp_path, 1)

    result = app.evaluation.check_oracle(directory, {1: program})

    assert result.faults == {}
    assert set(result.inconclusive) == {1}
    assert result.inconclusive[1]['inconclusive_reason'] == 'comparison_incomplete'
    assert result.inconclusive[1]['oracle'] == 'inlining-comparison'


def test_missing_executable_fails_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    app = inlining_application(tmp_path)
    configure_variants(app, tmp_path / 'missing-kotlinc')
    with pytest.raises(SystemExit, match='inlining-comparison preflight failed: .*'
                                         'missing compiler executable'):
        app.start()


@pytest.mark.parametrize('backend', KLIB_BACKENDS)
def test_real_compilers_accept_both_inliner_modes_across_modules(tmp_path, backend):
    if os.environ.get('HEPHAESTUS_TEST_REAL_KOTLIN') != '1':
        pytest.skip('set HEPHAESTUS_TEST_REAL_KOTLIN=1 for installed Kotlin distributions')
    app = inlining_application(tmp_path / 'run', backend)
    missing = [path for settings in app.compilation.settings_by_variant.values()
               for path in (os.path.expandvars(_compiler_executable(settings)),
                            os.path.expandvars(KotlinCompiler('', settings=settings).stdlib_path()))
               if not os.path.exists(path)]
    if missing:
        pytest.skip('missing internally selected Kotlin tools/libraries: ' + ', '.join(missing))
    app.mode.preflight(app.compilation)
    assert set(app.compilation.versions_by_variant) == set(VARIANT_IDS)

    # The consumer calls an inline function of its provider, which each variant built.
    for module, source, direct in (
            ('d1', 'package src.d1\ninline fun twice(x: Int): Int = x + x\n', None),
            ('d2', 'package src.d2\nfun answer(): Int = src.d1.twice(21)\n', 'd1')):
        variants = compile_source(app, tmp_path, module, source, direct,
                                  (direct,) if direct else ())
        assert [variant.variant_id for variant in variants] == list(VARIANT_IDS)
        for variant in variants:
            assert variant.command.status and variant.dependency_artifact_valid, (
                module, variant.variant_id, variant.command.output)
            assert ('-Xklib-ir-inliner=' + INLINER_MODES[variant.variant_id]
                    in variant.command.command)
