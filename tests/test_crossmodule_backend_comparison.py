import json
import os
from pathlib import Path
import shutil

import pytest

from src import utils
from src.generators.config import cfg
from src.ir import ast, kotlin_types as kt
from src.ir.context import Context
from src.modules.crossmodule import PublicationError
from src.modules.models import ProgramRes
from tests.test_backend_comparison import application, configure_variants, fixture_tool


def configured_application(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 1)
    monkeypatch.setattr(cfg.prob, 'max_module_chain_depth', 0)
    monkeypatch.setattr(cfg.prob, 'drop_indirect_deps_prob', 0)
    app = application(tmp_path)
    tool = fixture_tool(tmp_path)
    configure_variants(app, tool,
                        {backend: 'full compiler version'
                         for backend in app.compilation.settings_by_variant})
    app.setup_crossmodule_manager('versions')
    return app


def test_variant_publication_matches_published_paths(tmp_path, monkeypatch):
    app = configured_application(tmp_path, monkeypatch)
    publication = app.manager.variant_publication(17, 'wasm')
    assert publication == {
        'backend': 'wasm', 'module_name': 'd17',
        'klib': os.path.join('klibs', 'd17-wasm.klib'),
        'metadata': os.path.join('klibs', 'd17-wasm.meta.json'),
    }
    assert app.manager.published_klib_path(17, 'wasm') == str(
        Path(app.manager.test_directory) / publication['klib'])
    jvm = app.manager.variant_publication(17, 'jvm')
    assert jvm == {
        'backend': 'jvm', 'module_name': 'd17',
        'klib': os.path.join('klibs', 'd17-jvm.jar'),
        'metadata': os.path.join('klibs', 'd17-jvm.meta.json'),
    }
    assert app.manager.published_klib_path(17, 'jvm') == str(
        Path(app.manager.test_directory) / jvm['klib'])


def test_publication_logic_errors_use_publication_error(tmp_path, monkeypatch):
    app = configured_application(tmp_path, monkeypatch)
    with pytest.raises(PublicationError, match='incomplete backend bundle'):
        app.manager.publish_comparison_provider(1, {}, None, [])


def case(app, tmp_path, pid, direct=None, closure=()):
    batch = tmp_path / f'batch-{pid}'
    source = batch / 'src' / f'd{pid}' / 'program.kt'
    source.parent.mkdir(parents=True)
    program = ast.Program(Context(), 'kotlin')
    program.add_declaration(ast.VariableDeclaration(
        f'src.d{pid}.x', ast.IntegerConstant(1, kt.Integer), var_type=kt.Integer))
    app.artifacts.save_program(program, 'val x = 1', str(source))
    app.artifacts.save_program(program, 'val x = 1', str(
        Path(app.options.test_directory) / 'tmp' / str(pid) / 'program.kt'))
    return str(batch), ProgramRes(
        False, {'programs': {str(source): True}, 'time': 0},
        direct_dependency_pid=direct,
        dep_transitive_closure_pids=tuple(closure))


def commit(app, result):
    app.manager.commit_publications(result.publications)


CROSSMODULE_STATS_FIELDS = {
    'crossmodule', 'crossmodule_manifest_diff', 'crossmodule_variant_manifest_diff'}


@pytest.mark.parametrize('keep_everything', [False, True])
def test_standalone_agreement_only_retains_artifacts_when_requested(
        tmp_path, monkeypatch, keep_everything):
    app = configured_application(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    app.options.keep_everything = keep_everything
    directory, program = case(app, tmp_path, 1)

    result = app.evaluation.check_oracle(directory, {1: program})

    assert not result.faults and not result.inconclusive and not result.publications
    retained = Path(app.options.test_directory) / '1'
    assert (retained / 'program.kt').exists() is keep_everything
    assert not (Path(app.options.test_directory) / 'klibs').exists()


def test_three_node_chain_matching_targets_friends_and_commit(tmp_path, monkeypatch):
    app = configured_application(tmp_path, monkeypatch)
    commands = []
    original = app.compilation.observe

    def observe(compiler, cwd=None):
        commands.append((compiler.settings.backend, compiler.get_compiler_cmd()))
        return original(compiler, cwd=cwd)

    monkeypatch.setattr(app.compilation, 'observe', observe)
    run_process = app.compilation.run_process

    def no_version_probe(arguments, *args, **kwargs):
        # Versions were discovered once at startup; evaluation never probes again.
        assert '-version' not in arguments
        return run_process(arguments, *args, **kwargs)

    monkeypatch.setattr(app.compilation, 'run_process', no_version_probe)
    for pid, direct, closure in ((1, None, []), (2, 1, [1]), (3, 2, [2, 1])):
        directory, program = case(app, tmp_path, pid, direct, closure)
        result = app.evaluation.check_oracle(directory, {pid: program})
        assert not result.faults and not result.inconclusive
        variant_count = len(app.compilation.settings_by_variant)
        # No correctness-based provider recompilation.
        assert len(commands) == pid * variant_count
        record = result.publications[pid]
        assert record['direct'] == direct and record['closure'] == closure
        assert set(record['variants']) == {'js', 'native', 'wasm', 'jvm'}
        assert pid not in app.manager.provider_candidates(99)  # No ledger advertisement yet.
        for backend, command in commands[-variant_count:]:
            expected = [str(Path(app.manager.published_klib_path(item, backend)).resolve())
                        for item in closure]
            if backend == 'native':
                libraries = [command[index + 1] for index, arg in enumerate(command) if arg == '-library']
                if expected:
                    assert command[command.index('-friend-modules') + 1] == expected[0]
            elif backend == 'jvm':
                assert all(path.endswith('-jvm.jar') for path in expected)
                assert Path(command[command.index('-d') + 1]).name == f'd{pid}.jar'
                assert command[command.index('-module-name') + 1] == f'd{pid}'
                libraries = (command[command.index('-classpath') + 1].split(os.pathsep)
                             if expected else [])
                if expected:
                    assert command[-1] == '-Xfriend-paths=' + expected[0]
            else:
                libraries = command[command.index('-libraries') + 1].split(':')[1:]
                if expected:
                    assert command[-1] == '-Xfriend-modules=' + expected[0]
            assert libraries == expected
            # Physical suffixes do not rename the module.
            assert not any(f'd{pid}-{backend}' in argument for argument in command)
            metadata = json.loads((Path(app.options.test_directory) /
                                   record['variants'][backend]['metadata']).read_text())
            assert metadata['backend'] == backend and metadata['module_name'] == f'd{pid}'
            assert metadata['settings']['backend'] == backend
        commit(app, result)
    assert app.manager.provider_candidates(99) == [1, 2, 3]
    # The shared publication store stays flat, named by pid and variant_id.
    assert sorted(path.name for path in Path(app.manager.published_klibs_dir()).iterdir()) == sorted(
        f'd{pid}-{variant_id}{suffix}' for pid in (1, 2, 3)
        for variant_id in app.compilation.settings_by_variant
        for suffix in ('.jar' if variant_id == 'jvm' else '.klib', '.meta.json'))
    monkeypatch.setattr(app.manager.random, 'choice', lambda choices: 3)
    prepared = app.manager.prepare_dependency(4)
    assert prepared['closure'] == [3, 2, 1]
    assert 'klibs' not in prepared
    app.manager.config.prob.max_module_chain_depth = 3
    assert app.manager.provider_candidates(99) == [1, 2]


@pytest.mark.parametrize('corruption', ['artifact', 'metadata', 'legacy', 'transitive'])
def test_stale_or_legacy_bundles_cannot_seed_consumers(tmp_path, monkeypatch, corruption):
    app = configured_application(tmp_path, monkeypatch)
    for pid, direct, closure in ((1, None, []), (2, 1, [1])):
        directory, program = case(app, tmp_path, pid, direct, closure)
        commit(app, app.evaluation.check_oracle(directory, {pid: program}))
    if corruption in ('artifact', 'transitive'):
        Path(app.manager.published_klib_path(1, 'wasm')).unlink()
    elif corruption == 'metadata':
        metadata = Path(app.options.test_directory) / 'klibs/d1-js.meta.json'
        value = json.loads(metadata.read_text())
        value['backend'] = 'native'
        metadata.write_text(json.dumps(value))
    else:
        ledger = app.manager.load_ledger()
        del ledger['1']['variants']
        app.manager.save_ledger(ledger)
    assert app.manager.provider_candidates(3) == []


def test_selected_missing_dependency_blocks_only_affected_variant(tmp_path, monkeypatch):
    app = configured_application(tmp_path, monkeypatch)
    directory, program = case(app, tmp_path, 1)
    commit(app, app.evaluation.check_oracle(directory, {1: program}))
    directory, program = case(app, tmp_path, 2, 1, [1])
    Path(app.manager.published_klib_path(1, 'native')).unlink()
    result = app.evaluation.check_oracle(directory, {2: program})
    assert not result.faults and not result.publications
    assert 2 in result.inconclusive
    groups = program.stats['comparison']['groups']
    assert groups['accepted'] == ('js', 'wasm', 'jvm')
    assert groups['unavailable'] == ('native',)


@pytest.mark.parametrize('probability,drop', [(0, False), (1, True), (0.5, True)])
def test_logical_drops_draw_once_and_preserve_full_closure(tmp_path, monkeypatch, probability, drop):
    app = configured_application(tmp_path, monkeypatch)
    for pid, direct, closure in ((1, None, []), (2, 1, [1])):
        directory, program = case(app, tmp_path, pid, direct, closure)
        commit(app, app.evaluation.check_oracle(directory, {pid: program}))
    app.manager.config.prob.drop_indirect_deps_prob = probability
    draws = []
    monkeypatch.setattr(app.manager.random, 'bool', lambda value: draws.append(value) or drop)
    directory, program = case(app, tmp_path, 3, 2, [2, 1])
    result = app.evaluation.check_oracle(directory, {3: program})
    dependencies = program.stats['comparison_dependencies']
    assert list(dependencies.closure) == result.publications[3]['closure'] == [2, 1]
    assert dependencies.kept == ((2,) if drop else (2, 1))
    assert dependencies.dropped == ((1,) if drop else ())
    assert draws == ([probability] if probability else [])
    for backend, variant_dependencies in dependencies.variants.items():
        assert list(variant_dependencies.dependencies) == [
            str(Path(app.manager.published_klib_path(item, backend)).resolve())
            for item in dependencies.kept]
        assert variant_dependencies.friends == variant_dependencies.dependencies[:1]


def test_interrupted_publication_never_advertises_partial_provider(tmp_path, monkeypatch):
    app = configured_application(tmp_path, monkeypatch)
    original = __import__('os').replace
    writes = []

    def interrupted(source, destination):
        writes.append(destination)
        if len(writes) == 2:
            raise OSError('interrupted bundle publication')
        original(source, destination)

    monkeypatch.setattr('src.modules.crossmodule.os.replace', interrupted)
    directory, program = case(app, tmp_path, 1)
    result = app.evaluation.check_oracle(directory, {1: program})
    assert 1 in result.inconclusive
    assert not result.publications and not app.manager.provider_candidates(2)
    assert 'publication failed' in program.stats['comparison']['incomplete_reasons'][0]
    assert (Path(app.options.test_directory) / '1/program.kt').exists()
    app.reporting.update_stats(result, 1, 0)
    assert not Path(app.manager.ledger_path()).exists()


def test_variant_manifest_disagreement_is_evidence_not_ledger_or_statistics(
        tmp_path, monkeypatch, capsys):
    app = configured_application(tmp_path, monkeypatch)
    directory, program = case(app, tmp_path, 1)
    app.reporting.update_stats(app.evaluation.check_oracle(directory, {1: program}), 1, 0)
    read = app.manager.read_manifest_depends

    def with_ghost(path):
        depends = read(path)
        return None if depends is None else depends | {'ghost'}

    monkeypatch.setattr(app.manager, 'read_manifest_depends', with_ghost)
    directory, consumer = case(app, tmp_path, 2, 1, [1])
    result = app.evaluation.check_oracle(directory, {2: consumer})

    # The disagreement does not change the verdict or block publication.
    assert not result.faults and not result.inconclusive
    record = result.publications[2]
    diffs = record['variant_manifest_diff']
    assert set(diffs) == set(app.compilation.settings_by_variant)
    for variant_id, diff in diffs.items():
        assert 'ghost' in diff['unexpected'] and diff['closure'] == ['d1']
        metadata = json.loads((Path(app.options.test_directory) /
                               record['variants'][variant_id]['metadata']).read_text())
        assert metadata['manifest_diff'] == diff
    assert '2' not in app.manager.load_ledger()
    capsys.readouterr()

    app.reporting.update_stats(result, 1, 0)
    output = capsys.readouterr().out
    for variant_id in diffs:
        assert f'Program 2 ({variant_id}) records dependencies outside its closure' in output
    ledger = app.manager.load_ledger()
    assert ledger['2'] == {'direct': 1, 'closure': [1], 'variants': record['variants']}
    assert set(ledger) == {'1', '2'}
    assert record['variant_manifest_diff'] == diffs
    stats = json.loads((Path(app.options.test_directory) / 'stats.json').read_text())
    assert not CROSSMODULE_STATS_FIELDS & stats.keys()
    assert not CROSSMODULE_STATS_FIELDS & app.reporting.stats.keys()
    assert app.reporting.stats['totals'] == {'passed': 2, 'failed': 0, 'inconclusive': 0}
    assert app.manager.provider_candidates(3) == [1, 2]


def test_unpacked_artifact_publication(tmp_path, monkeypatch):
    app = configured_application(tmp_path, monkeypatch)
    directory, program = case(app, tmp_path, 1)
    requests, _ = app.mode._prepare_requests(app.evaluation, directory, 1, program)
    observations = app.compilation.run_variants(requests)
    import zipfile
    # Only KLIBs have an unpacked form; the JVM variant keeps its JAR.
    for observation in observations:
        if observation.settings.backend == 'jvm':
            continue
        path = Path(observation.produced_dependency_artifact_path)
        with zipfile.ZipFile(path) as archive:
            manifest = archive.read('default/manifest')
        path.unlink()
        (path / 'default').mkdir(parents=True)
        (path / 'default/manifest').write_bytes(manifest)
    record = app.manager.publish_comparison_provider(
        1, {observation.variant_id: observation for observation in observations}, None, [])
    app.manager.save_ledger({'1': record})
    assert app.manager.provider_candidates(2) == [1]
    assert all(Path(app.manager.published_klib_path(1, backend)).is_dir()
               for backend in app.compilation.settings_by_variant if backend != 'jvm')
    assert Path(app.manager.published_klib_path(1, 'jvm')).is_file()


@pytest.mark.parametrize('target,mode,fault,inconclusive', [
    ('all', 'reject', False, False),
    ('js', 'reject', True, False),
    ('native', 'crash', True, False),
    ('jvm', 'reject', True, False),
    ('jvm', 'crash', True, False),
    ('wasm', 'missing', False, True),
    ('jvm', 'missing', False, True),
    ('js', 'opaque', False, True),
])
def test_unsuccessful_bundles_are_never_providers(
        tmp_path, monkeypatch, target, mode, fault, inconclusive):
    app = configured_application(tmp_path, monkeypatch)
    tool = Path(app.compilation.settings_by_variant['js'].executable)
    tool.write_text(tool.read_text().replace('a = sys.argv[1:]\n',
        'a = sys.argv[1:]\n'
        "backend = ('native' if '-produce' in a else 'jvm' if '-d' in a else 'wasm' if any(\n"
        "    'kotlin-stdlib-wasm-' in item for item in a) else 'js')\n"
        f"if {target!r} in ('all', backend):\n"
        f"    mode = {mode!r}\n"
        "    if mode == 'reject': print(a[0] + ':1:1: error: controlled rejection'); sys.exit(1)\n"
        "    if mode == 'crash': print('org.jetbrains.kotlin.CompilerException\\ncrash'); sys.exit(2)\n"
        "    if mode == 'missing': sys.exit(0)\n"
        "    if mode == 'opaque': print('unrecognized failure'); sys.exit(1)\n"))
    directory, program = case(app, tmp_path, 1)
    result = app.evaluation.check_oracle(directory, {1: program})
    assert bool(result.faults) == fault
    assert bool(result.inconclusive) == inconclusive
    assert not result.publications and not app.manager.provider_candidates(2)
    if target == 'jvm' and fault:
        comparison = result.faults[1]['comparison']
        assert ('jvm' in comparison['groups'].get('rejected', ())) == (mode == 'reject')
        assert ('jvm' in comparison['groups'].get('crashed', ())) == (mode == 'crash')
        kinds = [finding['kind'] for finding in comparison['findings']]
        assert kinds == (['status_mismatch'] if mode == 'reject' else ['crash'])
        retained = Path(app.options.test_directory) / '1' / 'variants' / 'jvm'
        assert (retained / 'result.json').exists() and (retained / 'output.txt').exists()