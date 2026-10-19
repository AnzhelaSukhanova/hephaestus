import json
import os
from dataclasses import replace
from pathlib import Path

from src.compilers.kotlin import KotlinBackend, KotlinCompiler, KotlinSettings
from src.compilers.results import (
    CommandObservation, ParsedDiagnostics, ProcessResult, VariantObservation,
)
from src.generators.config import cfg
from src.modules.oracle_modes import BackendComparisonMode
from src.oracles.backend_comparison import check_backend_compilation
from tests.test_backend_comparison import application, fixture_tool
from tests.test_crossmodule_backend_comparison import case, commit


VARIANTS: tuple[tuple[str, KotlinBackend], ...] = (
    ('web', 'js'),
    ('native-a', 'native'),
    ('native-b', 'native'),
)
VARIANT_IDS = tuple(variant_id for variant_id, _ in VARIANTS)


def comparison_observation(variant_id: str, backend: KotlinBackend
                           ) -> VariantObservation[KotlinSettings]:
    command = CommandObservation(
        ProcessResult(0, ''), {}, ParsedDiagnostics(), (), None,
        ('compiler',), '/work', 0.0)
    return VariantObservation(
        variant_id, command, settings=KotlinSettings(backend=backend))


def test_comparison_uses_configured_variant_ids():
    observations = {
        variant_id: comparison_observation(variant_id, backend)
        for variant_id, backend in VARIANTS
    }

    result = check_backend_compilation(observations, VARIANT_IDS)

    assert result.complete
    assert result.groups['accepted'] == VARIANT_IDS
    assert result.all_success

    missing = dict(observations)
    del missing['native-b']
    missing_result = check_backend_compilation(missing, VARIANT_IDS)
    assert not missing_result.complete
    assert any('native-b: missing observation' in reason
               for reason in missing_result.incomplete_reasons)

    extra = dict(observations)
    extra['unconfigured'] = comparison_observation('unconfigured', 'wasm')
    extra_result = check_backend_compilation(extra, VARIANT_IDS)
    assert not extra_result.complete
    assert any('unconfigured' in reason for reason in extra_result.incomplete_reasons)


def custom_variants(tmp_path) -> tuple[dict[str, KotlinSettings], dict[str, str]]:
    template = fixture_tool(tmp_path)
    settings: dict[str, KotlinSettings] = {}
    for variant_id, backend in VARIANTS:
        executable = tmp_path / f'{variant_id}-compiler'
        executable.write_text(template.read_text())
        executable.chmod(0o755)
        settings[variant_id] = KotlinSettings(
            backend=backend, executable=str(executable))
    versions = {variant_id: 'Kotlin compiler 2.4.255-SNAPSHOT'
                for variant_id in VARIANT_IDS}
    return settings, versions


def test_preflight_uses_backend_from_each_variant_settings(tmp_path, monkeypatch):
    library = tmp_path / 'stdlib'
    library.mkdir()
    monkeypatch.setattr(KotlinCompiler, 'stdlib_path', lambda self: str(library))
    monkeypatch.setattr('src.compilers.kotlin.shutil.which', lambda executable: executable)
    settings = {
        variant_id: KotlinSettings(backend=backend,
                                   executable=f'/fake/{variant_id}')
        for variant_id, backend in VARIANTS
    }
    app = application(tmp_path)
    app.compilation.settings_by_variant = settings
    version_commands = []
    requests = []

    def run_command(arguments, get_stdout=True, cwd=None):
        version_commands.append(tuple(arguments))
        return True, 'Kotlin compiler version 2.4.255-SNAPSHOT'

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

    monkeypatch.setattr(app.compilation, 'run_command', run_command)
    monkeypatch.setattr(app.compilation, 'run_variants', run_variants)
    app.mode.preflight(app.compilation)

    assert tuple(app.compilation.settings_by_variant) == VARIANT_IDS
    assert tuple(request.variant_id for request in requests) == VARIANT_IDS
    assert [request.compiler.settings for request in requests] == list(settings.values())
    assert app.compilation.versions_by_variant == {
        variant_id: 'Kotlin compiler version 2.4.255-SNAPSHOT'
        for variant_id in VARIANT_IDS
    }
    commands = {command[0]: command for command in version_commands}
    assert len(version_commands) == len(VARIANT_IDS)
    assert '-ir-output-name' in commands['/fake/web']
    assert '-ir-output-name' not in commands['/fake/native-a']
    assert '-ir-output-name' not in commands['/fake/native-b']
    selected = app.compilation.settings_by_variant
    assert selected['web'].backend == 'js'
    assert selected['native-a'].backend == selected['native-b'].backend == 'native'


def custom_crossmodule_application(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 1)
    monkeypatch.setattr(cfg.prob, 'max_module_chain_depth', 0)
    monkeypatch.setattr(cfg.prob, 'drop_indirect_deps_prob', 0)
    app = application(tmp_path, ['--keep-all'])
    settings, versions = custom_variants(tmp_path)
    app.compilation.settings_by_variant = settings
    app.compilation.versions_by_variant = versions
    manager = app.setup_crossmodule_manager('Kotlin compiler 2.4.255-SNAPSHOT')
    app.options.keep_everything = True
    return app, settings, manager


def test_missing_shell_compiler_in_backend_comparison_is_inconclusive(
        tmp_path, monkeypatch):
    app, configured, manager = custom_crossmodule_application(tmp_path, monkeypatch)
    missing = str(tmp_path / 'missing-kotlinc')
    settings = {variant_id: replace(variant_settings, executable=missing)
                for variant_id, variant_settings in configured.items()}
    app.compilation.settings_by_variant = settings
    manager.settings_by_variant = settings
    directory, program = case(app, tmp_path, 1)

    result = app.evaluation.check_oracle(directory, {1: program})

    assert result.faults == {}
    assert set(result.inconclusive) == {1}
    assert result.inconclusive[1]['inconclusive_reason'] == (
        'backend_comparison_incomplete')
    retained = Path(app.options.test_directory) / '1' / 'variants' / 'web' / 'result.json'
    evidence = json.loads(retained.read_text())
    assert evidence['status'] is False
    assert evidence['returncode'] != 0
    output = retained.with_name('output.txt').read_text()
    assert 'missing-kotlinc' in output


def test_crossmodule_artifacts_and_compilation_use_variant_ids_and_backends(
        tmp_path, monkeypatch):
    app, settings, manager = custom_crossmodule_application(tmp_path, monkeypatch)
    directory, provider = case(app, tmp_path, 1)

    provider_result = app.evaluation.check_oracle(directory, {1: provider})

    assert not provider_result.faults and not provider_result.inconclusive
    record = provider_result.publications[1]
    published_variants = record.get('variants')
    assert published_variants is not None
    assert set(published_variants) == set(VARIANT_IDS)
    for variant_id, backend in VARIANTS:
        artifact = published_variants[variant_id]
        assert artifact['backend'] == backend
        assert os.path.basename(artifact['klib']) == f'd1-{variant_id}.klib'
        metadata = json.loads((Path(app.options.test_directory) /
                               artifact['metadata']).read_text())
        assert metadata['backend'] == backend
        assert metadata['settings'] == {
            'backend': backend, 'dump_ir': False,
            'executable': settings[variant_id].executable,
        }
        retained = Path(app.options.test_directory) / '1' / 'variants' / variant_id
        result = json.loads((retained / 'result.json').read_text())
        assert result['variant_id'] == variant_id
        assert result['backend'] == backend
        assert result['settings'] == metadata['settings']
    assert (published_variants['native-a']['klib']
            != published_variants['native-b']['klib'])

    commit(app, provider_result)
    assert manager.provider_candidates(2) == [1]

    commands = []
    original_observe = app.compilation.observe

    def observe(compiler, cwd=None):
        command = compiler.get_compiler_cmd()
        commands.append((compiler.settings, command))
        return original_observe(compiler, cwd=cwd)

    monkeypatch.setattr(app.compilation, 'observe', observe)
    directory, consumer = case(app, tmp_path, 2, direct=1, closure=[1])
    consumer_result = app.evaluation.check_oracle(directory, {2: consumer})
    assert not consumer_result.faults and not consumer_result.inconclusive

    native_commands = {
        settings.executable: command
        for settings, command in commands if settings.backend == 'native'
    }
    assert set(native_commands) == {
        settings['native-a'].executable, settings['native-b'].executable}
    for variant_id in ('native-a', 'native-b'):
        command = native_commands[settings[variant_id].executable]
        expected = str(Path(manager.published_klib_path(1, variant_id)).resolve())
        libraries = [command[index + 1]
                     for index, argument in enumerate(command) if argument == '-library']
        assert libraries == [expected]
        assert command[command.index('-friend-modules') + 1] == expected

    Path(manager.published_klib_path(1, 'native-a')).unlink()
    directory, blocked_program = case(app, tmp_path, 3, direct=1, closure=[1])
    blocked_result = app.evaluation.check_oracle(directory, {3: blocked_program})

    assert 3 in blocked_result.inconclusive
    retained = Path(app.options.test_directory) / '3' / 'variants'
    blocked = json.loads((retained / 'native-a' / 'result.json').read_text())
    assert blocked['status'] is None and blocked['incomplete_reason']
    assert blocked['variant_id'] == 'native-a'
    assert blocked['backend'] == 'native'
    assert blocked['settings']['executable'] == settings['native-a'].executable
    executed = json.loads((retained / 'native-b' / 'result.json').read_text())
    assert executed['status'] is True
    assert executed['backend'] == 'native'
    assert executed['settings']['executable'] == settings['native-b'].executable


def test_publication_metadata_records_the_settings_each_attempt_used(
        tmp_path, monkeypatch):
    app, _, manager = custom_crossmodule_application(tmp_path, monkeypatch)
    directory, provider = case(app, tmp_path, 1)
    assert isinstance(app.mode, BackendComparisonMode)
    requests, _ = app.mode._prepare_requests(app.evaluation, directory, 1, provider)
    observations = {observation.variant_id: observation
                    for observation in app.compilation.run_variants(requests)}
    drifted = KotlinSettings(backend='native', executable='/other/compiler')
    observations['native-b'] = replace(observations['native-b'], settings=drifted)

    record = manager.publish_comparison_provider(1, observations, None, ())

    published = record.get('variants')
    assert published is not None
    metadata = json.loads((Path(app.options.test_directory) /
                           published['native-b']['metadata']).read_text())
    assert metadata['backend'] == 'native'
    assert metadata['settings']['executable'] == '/other/compiler'
    manager.save_ledger({'1': record})
    ledger = manager.load_ledger()
    assert manager._validate_variant(ledger, 1, 'native-a') is None
    assert manager._validate_variant(ledger, 1, 'native-b') == (
        'd1/native-b: incompatible or stale metadata')
    assert manager.provider_candidates(2) == []
