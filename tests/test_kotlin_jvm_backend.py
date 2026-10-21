"""Kotlin/JVM in the ordinary compilation oracle: commands, verdicts and JARs."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import zipfile

import pytest

from hephaestus import Fuzzer
from src.args import parse_args, validate_args
from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.generators.config import cfg
from src.modules.artifacts import Artifacts
from src.modules.compilation import ORDINARY_VARIANT, Compilation
from src.modules.worker import RunOptions
from tests.test_crossmodule_backend_comparison import case


ROOT = Path(__file__).resolve().parents[1]
FULL_PATH = '-J-Dorg.jetbrains.kotlin.cliMessageRenderer=FullPath'


def jvm_application(tmp_path, monkeypatch, extra=()):
    log = tmp_path / 'commands.jsonl'
    monkeypatch.setenv('DRIVER_TOOL_LOG', str(log))
    tool = tmp_path / 'kotlinc-jvm'
    tool.write_text('#!' + sys.executable + '\n' +
                    (ROOT / 'tests/driver/fake_compiler.py').read_text())
    tool.chmod(0o755)
    app = Fuzzer(parse_args(['-P', '-t', '0', '--batch', '1', '--backend', 'jvm',
                             '-b', str(tmp_path / 'bugs'), '-n', 'case', *extra]))
    settings = replace(app.compilation.settings, executable=str(tool))
    app.compilation.settings = settings
    app.compilation.settings_by_variant = {ORDINARY_VARIANT: settings}
    return app, log


def logged_commands(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.parametrize('extra', [
    ['--dump-ir'],
    ['--dump-ir', '--keep-everything', '--jacoco-lowerings', 'FunctionInlining'],
    ['--keep-everything', '--jacoco-always'],
])
def test_jvm_rejects_ir_dumps_and_live_jacoco(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    options = parse_args(['-P', '-t', '0', '--backend', 'jvm',
                          '-b', str(tmp_path), '-n', 'case', *extra])
    with pytest.raises(SystemExit, match='--backend jvm does not support'):
        validate_args(options)
    validate_args(parse_args(['-P', '-t', '0', '--backend', 'jvm',
                              '-b', str(tmp_path), '-n', 'plain']))


def test_jvm_version_probe_has_no_scratch_flags(tmp_path, monkeypatch):
    app, log = jvm_application(tmp_path, monkeypatch)
    app.compilation.discover_versions()
    assert app.compilation.versions_by_variant == {ORDINARY_VARIANT: 'hermetic compiler 1.0'}
    assert [entry['arguments'] for entry in logged_commands(log)] == [['-version']]


def test_jvm_single_module_acceptance_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    app, log = jvm_application(tmp_path, monkeypatch)
    directory, program = case(app, tmp_path, 1)

    result = app.evaluation.check_oracle(directory, {1: program})

    assert not result.faults and not result.inconclusive and not result.publications
    entry, = logged_commands(log)
    source_root = os.path.join(directory, 'src')
    assert entry['arguments'] == [source_root, FULL_PATH,
                                  '-d', source_root + '.jar', '-nowarn']


def test_jvm_single_module_rejection_is_unexpected_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    monkeypatch.setenv('DRIVER_COMPILER_MODE', 'reject')
    app, log = jvm_application(tmp_path, monkeypatch)
    directory, program = case(app, tmp_path, 1)

    result = app.evaluation.check_oracle(directory, {1: program})

    assert not result.inconclusive
    stats = result.faults[1]
    assert 'controlled rejection' in stats['error']
    assert stats['error_phase'] == 'frontend'
    compile_command, enrichment = logged_commands(log)
    assert '-d' in compile_command['arguments']
    assert enrichment['arguments'][-1] == '-Xprofile-phases'
    assert '-d' in enrichment['arguments']


def test_jvm_crossmodule_chain_publishes_and_consumes_jars(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 1)
    monkeypatch.setattr(cfg.prob, 'max_module_chain_depth', 0)
    monkeypatch.setattr(cfg.prob, 'drop_indirect_deps_prob', 0)
    app, log = jvm_application(tmp_path, monkeypatch, ['--keep-all'])
    manager = app.setup_crossmodule_manager('versions')
    for pid, direct, closure in ((1, None, []), (2, 1, [1]), (3, 2, [2, 1])):
        directory, program = case(app, tmp_path, pid, direct, closure)
        log.write_text('')
        result = app.evaluation.check_oracle(directory, {pid: program})
        assert not result.faults and not result.inconclusive
        record = result.publications[pid]
        assert record == {'direct': direct, 'closure': closure}  # No manifest diff.
        expected = [str(Path(manager.published_klib_path(item)).resolve())
                    for item in closure]
        primary, provider = logged_commands(log)
        for entry in (primary, provider):
            command = entry['arguments']
            assert entry['cwd'] == os.path.join(directory, 'src', f'd{pid}')
            assert command[command.index('-d') + 1] == f'd{pid}.jar'
            assert command[command.index('-module-name') + 1] == f'd{pid}'
            if closure:
                assert command[command.index('-classpath') + 1] == os.pathsep.join(expected)
                assert command[-1] == '-Xfriend-paths=' + expected[0]
            else:
                assert '-classpath' not in command
                assert not any(arg.startswith('-Xfriend-paths') for arg in command)
        published = Path(manager.published_klib_path(pid))
        assert published.name == f'd{pid}.jar'
        assert KotlinCompiler.read_manifest_depends(str(published)) == set()
        metadata = json.loads((published.parent / f'd{pid}.meta.json').read_text())
        assert metadata['backend'] == 'jvm' and 'manifest_diff' not in metadata
        assert pid not in manager.provider_candidates(99)
        manager.commit_publications(result.publications)
    assert manager.provider_candidates(99) == [1, 2, 3]
    assert sorted(path.name for path in Path(manager.published_klibs_dir()).iterdir()) == sorted(
        f'd{pid}{suffix}' for pid in (1, 2, 3) for suffix in ('.jar', '.meta.json'))

    # The drop knob withholds indirect JARs from the classpath only.
    monkeypatch.setattr(cfg.prob, 'drop_indirect_deps_prob', 1)
    monkeypatch.setattr(manager.random, 'bool', lambda probability: True)
    directory, program = case(app, tmp_path, 4, 3, [3, 2, 1])
    log.write_text('')
    result = app.evaluation.check_oracle(directory, {4: program})
    assert not result.faults and not result.inconclusive
    assert result.publications[4]['closure'] == [3, 2, 1]
    assert program.stats['dropped_indirect_deps'] == [2, 1]
    direct_jar = str(Path(manager.published_klib_path(3)).resolve())
    for entry in logged_commands(log):
        command = entry['arguments']
        assert command[command.index('-classpath') + 1] == direct_jar
        assert command[-1] == '-Xfriend-paths=' + direct_jar


# Real kotlinc-jvm: opt-in, since every invocation takes a few seconds.

def real_jvm_settings():
    if os.environ.get('HEPHAESTUS_TEST_REAL_KOTLIN') != '1':
        pytest.skip('set HEPHAESTUS_TEST_REAL_KOTLIN=1 to run real kotlinc-jvm tests')
    settings = KotlinSettings('jvm')
    problem = KotlinCompiler('source', settings=settings).installation_problem()
    if problem is not None:
        pytest.skip(problem)
    return settings


def real_compilation(directory):
    options = RunOptions.from_namespace(parse_args([
        '--backend', 'jvm', '-b', str(directory / 'bugs'), '-n', 'session']))
    return Compilation(options, Artifacts(options), {'kotlin': KotlinCompiler},
                       settings=real_jvm_settings())


@pytest.mark.parametrize('text,phase', [
    (None, None),
    ('fun first(): Int = "wrong type"\n', 'frontend'),
    ('fun foo(x: List<String>) = 1\nfun foo(x: List<Int>) = 2\n', 'backend'),
])
def test_real_jvm_single_module(text, phase):
    # Avoid pytest's hyphenated temporary paths, unsupported by the existing parser.
    with TemporaryDirectory() as name:
        directory = Path(name).resolve()
        compilation = real_compilation(directory)
        source = directory / 'program.kt'
        source.write_text(text or 'fun answer(): Int = 42\n')
        compiler = compilation.make_compiler(str(source))
        observation = compilation.observe(compiler)
        jar = Path(str(source) + '.jar')
        if text is None:
            assert observation.status, observation.output
            with zipfile.ZipFile(jar) as archive:
                assert 'META-INF/MANIFEST.MF' in archive.namelist()
            assert KotlinCompiler.read_manifest_depends(str(jar)) == set()
            return
        assert not observation.status
        assert observation.crash_message is None, observation.output
        assert str(source) in observation.diagnostics, observation.output
        assert compilation.enrich_error(compiler, str(source)) == {'error_phase': phase}
        if phase == 'backend':
            assert 'platform declaration clash' in observation.output


def test_real_jvm_friend_path_grants_internal_access():
    with TemporaryDirectory() as name:
        directory = Path(name).resolve()
        compilation = real_compilation(directory)
        sources = {1: 'package src.d1\n\ninternal fun secret(): Int = 1\n',
                   2: 'package src.d2\n\nimport src.d1.secret\n\nfun use(): Int = secret()\n'}
        for pid, text in sources.items():
            (directory / f'd{pid}').mkdir()
            (directory / f'd{pid}' / 'program.kt').write_text(text)
        provider = compilation.make_compiler(str(directory / 'd1' / 'program.kt'),
                                             module_name='d1')
        assert compilation.observe(provider, cwd=str(directory / 'd1')).status
        jar = str(directory / 'd1' / 'd1.jar')
        assert KotlinCompiler.read_manifest_depends(jar) == set()
        consumer_source = str(directory / 'd2' / 'program.kt')
        consumer = compilation.make_compiler(
            consumer_source, dependency_klibs=[jar], friend_klibs=[jar], module_name='d2')
        observation = compilation.observe(consumer, cwd=str(directory / 'd2'))
        assert observation.status, observation.output
        stranger = compilation.make_compiler(
            consumer_source, dependency_klibs=[jar], module_name='d2')
        observation = compilation.observe(stranger, cwd=str(directory / 'd2'))
        assert not observation.status
        assert consumer_source in observation.diagnostics, observation.output
