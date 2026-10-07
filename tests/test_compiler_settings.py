from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from collections.abc import Collection

import pytest

from src.compilers.base import BaseCompiler
from src.compilers.groovy import GroovyCompiler
from src.compilers.java import JavaCompiler
from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.compilers.scala import ScalaCompiler
from src.args import parse_args
from src.modules.artifacts import Artifacts
from src.modules.compilation import Compilation
from src.modules.models import ProgramRes
from src.modules.worker import RunOptions


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('compiler_cls, command, version', [
    (JavaCompiler, ['javac', '-nowarn', 'src/*/*.java'], ['javac', '-version']),
    (GroovyCompiler, ['groovyc-l', '--compile-static', 'src/*/*.groovy'],
     ['groovyc-l', '-version']),
    (ScalaCompiler, ['scalac', '-color', 'never', '-nowarn', 'src/*/*.scala'],
     ['scalac', '-version']),
])
def test_unconfigured_adapter_defaults(compiler_cls, command, version):
    default = compiler_cls('src', ['filter'])
    explicit = compiler_cls('src', ['filter'], settings=None)
    assert default.get_compiler_cmd() == explicit.get_compiler_cmd() == command
    assert compiler_cls.get_compiler_version() == version
    assert compiler_cls.get_compiler_version(None) == version
    assert default.settings is explicit.settings is None
    assert default.filter_patterns == explicit.filter_patterns == ['filter']
    assert default.crash_msg is explicit.crash_msg is None


def test_kotlin_default_settings():
    default = KotlinCompiler('src')
    explicit = KotlinCompiler('src', settings=KotlinSettings())
    command = ['$HOME/kotlin/kotlin-native/dist/bin/kotlinc-native', 'src',
               '-J-Dorg.jetbrains.kotlin.cliMessageRenderer=FullPath',
               '-produce', 'library', '-o', 'src', '-nowarn',
               '-Xklib-ir-inliner=full']
    assert default.get_compiler_cmd() == explicit.get_compiler_cmd() == command
    assert KotlinCompiler.get_compiler_version() == [command[0], '-version']
    assert KotlinCompiler.get_compiler_version(KotlinSettings()) == [
        command[0], '-version']
    assert default.settings == KotlinSettings()


@pytest.mark.parametrize('backend', ['native', 'js', 'wasm'])
@pytest.mark.parametrize('dump_ir', [False, True])
def test_explicit_kotlin_commands(backend, dump_ir):
    settings = KotlinSettings(backend, dump_ir, '/custom/kotlinc')
    compiler = KotlinCompiler('src', ['filter'], ['direct.klib', 'indirect.klib'],
                              ['direct.klib'], 'module', settings=settings)
    if backend == 'native':
        command = ['/custom/kotlinc', 'src',
                   '-J-Dorg.jetbrains.kotlin.cliMessageRenderer=FullPath',
                   '-produce', 'library', '-o',
                   'module', '-nowarn', '-Xklib-ir-inliner=full',
                   '-library', 'direct.klib', '-library', 'indirect.klib',
                   '-friend-modules', 'direct.klib']
    else:
        stdlib = ('$HOME/kotlin/libraries/stdlib/build/libs/kotlin-stdlib-' +
                  ('wasm-' if backend == 'wasm' else '') +
                  'js-2.4.255-SNAPSHOT.klib')
        command = ['/custom/kotlinc', 'src',
                   '-J-Dorg.jetbrains.kotlin.cliMessageRenderer=FullPath',
                   '-Xir-produce-klib-file',
                   '-ir-output-dir', '.', '-ir-output-name', 'module',
                   '-libraries', os.pathsep.join([
                       stdlib, 'direct.klib', 'indirect.klib']),
                   '-nowarn', '-Xklib-ir-inliner=full', '-Xfriend-modules',
                   'direct.klib']
    dump = (['-Xphases-to-dump=' + compiler.IR_MODIFYING_INLINER_PHASES,
             '-Xdump-directory=src/ir'] if dump_ir else [])
    assert compiler.settings is settings
    assert compiler.get_compiler_cmd() == command + dump
    assert compiler.get_error_enrichment_cmds('file.kt') == {
        'xprofile-phases': [command[0], 'file.kt', *command[2:],
                            '-Xprofile-phases']}
    assert KotlinCompiler.get_compiler_version(settings) == [
        '/custom/kotlinc', '-version']


def test_base_requires_explicit_settings():
    with pytest.raises(TypeError, match='settings'):
        inspect.signature(BaseCompiler).bind('src')
    assert BaseCompiler('src', settings=None).settings is None


def test_compilation_keeps_attempt_state_separate(tmp_path):
    options = RunOptions.from_namespace(parse_args(['--language', 'kotlin']))
    settings = KotlinSettings('wasm', True, '/custom/kotlinc')
    compilation = Compilation(options, Artifacts(options),
                              {'kotlin': KotlinCompiler}, settings=settings)
    first = compilation.make_compiler('first', ['first-filter'],
                                      dependency_klibs=['first.klib'],
                                      friend_klibs=['first.klib'], module_name='first')
    assert isinstance(first, KotlinCompiler)
    first.analyze_compiler_output('org.jetbrains.kotlin.Crash\nfirst crash')
    second = compilation.make_compiler('second', ['second-filter'],
                                       dependency_klibs=['second.klib'],
                                       friend_klibs=[], module_name='second')
    assert isinstance(second, KotlinCompiler)
    assert first is not second
    assert first.crash_msg == 'org.jetbrains.kotlin.Crash\nfirst crash'
    assert second.crash_msg is None
    assert first.input_name == first.module_name == 'first'
    assert second.input_name == second.module_name == 'second'
    assert first.filter_patterns == ['first-filter']
    assert second.filter_patterns == ['second-filter']
    assert first.dependency_klibs == first.friend_klibs == ['first.klib']
    assert second.dependency_klibs == ['second.klib']
    assert second.friend_klibs == []
    assert first.settings is second.settings is settings
    assert compilation.version_command() == ['/custom/kotlinc', '-version']


class TracedKotlin(KotlinCompiler):
    attempts: list[TracedKotlin] = []
    version_settings: list[KotlinSettings] = []

    def __init__(self, input_name: str,
                 filter_patterns: Collection[str] | None = None,
                 dependency_klibs: list[str] | None = None,
                 friend_klibs: list[str] | None = None,
                 module_name: str | None = None, *,
                 settings: KotlinSettings = KotlinSettings()) -> None:
        super().__init__(input_name, filter_patterns, dependency_klibs,
                         friend_klibs, module_name, settings=settings)
        self.attempts.append(self)

    @classmethod
    def get_compiler_version(cls, settings: KotlinSettings = KotlinSettings()) -> list[str]:
        cls.version_settings.append(settings)
        return super().get_compiler_version(settings)


@pytest.mark.parametrize('backend', ['native', 'js', 'wasm'])
def test_settings_reach_version_enrichment_reruns_and_correct_only_provider(
        tmp_path, monkeypatch, backend):
    tool = tmp_path / 'kotlinc'
    # In JS/Wasm provider commands '.' is an output directory, not a source.
    fake_source = (ROOT / 'tests/driver/fake_compiler.py').read_text().replace(
        'if path.is_dir():', "if path.is_dir() and argument != '.':")
    tool.write_text('#!' + sys.executable + '\n' +
                    fake_source)
    tool.chmod(0o755)
    log = tmp_path / 'commands.jsonl'
    monkeypatch.setenv('DRIVER_TOOL_LOG', str(log))
    monkeypatch.setenv('DRIVER_COMPILER_MODE', 'reject')
    monkeypatch.setattr(TracedKotlin, 'attempts', [])
    monkeypatch.setattr(TracedKotlin, 'version_settings', [])
    monkeypatch.chdir(tmp_path)
    options = RunOptions.from_namespace(parse_args([
        '--language', 'kotlin', '-b', str(tmp_path / 'bugs'), '-n', 'session']))
    settings = KotlinSettings(backend, True, str(tool))
    artifacts = Artifacts(options)
    compilation = Compilation(options, artifacts, {'kotlin': TracedKotlin}, settings=settings)
    assert compilation.run_command(compilation.version_command()) == (
        True, 'hermetic compiler 1.0\n')
    assert TracedKotlin.version_settings == [settings]
    assert TracedKotlin.version_settings[0] is settings

    # Relative source paths avoid the existing diagnostic regex's hyphen limit.
    batch = Path('batch')
    batch.mkdir()
    correct = batch / 'program.kt'
    incorrect = batch / 'program_incorrect.kt'
    correct.write_text('val answer = 1')
    incorrect.write_text('val answer = missing')
    normal = compilation.make_compiler(str(batch), {'filter'},
                                       dependency_klibs=['direct.klib', 'indirect.klib'],
                                       friend_klibs=['direct.klib'], module_name='consumer')
    observation = compilation.observe(normal, cwd=str(tmp_path))
    assert not observation.status
    assert set(observation.diagnostics) == {str(correct), str(incorrect)}
    assert observation.crash_message is None
    assert observation.command == tuple(normal.get_compiler_cmd())
    assert compilation.enrich_error(normal, str(correct), cwd=str(tmp_path)) == {
        'error_phase': 'frontend'}

    rerun_sources = []
    for tid in (2, 1):
        source = Path(artifacts.get_transformations_dir(1, tid)) / 'program.kt'
        source.parent.mkdir(parents=True)
        source.write_text('val answer = missing')
        rerun_sources.append(str(source))
    compilation._report_failed(1, 2, normal, True)
    assert [attempt.input_name for attempt in TracedKotlin.attempts] == [
        str(batch), *rerun_sources]
    assert all(attempt.settings is settings for attempt in TracedKotlin.attempts)

    monkeypatch.setenv('DRIVER_COMPILER_MODE', 'reject-incorrect')
    result = ProgramRes(False, {'programs': {str(correct): True, str(incorrect): False}})
    provider, elapsed = compilation._build_crossmodule_provider(
        result, {'filter'}, ['direct.klib', 'indirect.klib'], ['direct.klib'],
        'provider', str(tmp_path))
    assert provider is not None
    assert elapsed >= 0
    klib, provider_command = provider
    assert Path(klib).is_file()
    provider_attempt = TracedKotlin.attempts[-1]
    assert provider_attempt.input_name == str(correct)
    assert provider_attempt.settings is settings
    assert provider_attempt.module_name == 'provider'
    assert provider_attempt.get_klib_filename() == 'provider.klib'
    assert klib == str(tmp_path / 'provider.klib')
    assert provider_attempt.dependency_klibs == ['direct.klib', 'indirect.klib']
    assert provider_attempt.friend_klibs == ['direct.klib']
    assert provider_command == provider_attempt.get_compiler_cmd()
    assert str(incorrect) not in provider_command
    assert len({id(attempt) for attempt in TracedKotlin.attempts}) == 4
    expected_commands = [compilation.version_command(), normal.get_compiler_cmd(),
                         normal.get_error_enrichment_cmds(str(correct))['xprofile-phases'],
                         *[attempt.get_compiler_cmd() for attempt in TracedKotlin.attempts[1:]]]
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry['arguments'] for entry in commands] == [
        [os.path.expandvars(arg) for arg in cmd[1:]] for cmd in expected_commands]
    assert commands[1]['cwd'] == commands[2]['cwd'] == commands[-1]['cwd'] == str(tmp_path)
    assert all(entry['java_opts'] == '-Xmx8g' for entry in commands)


def test_static_adapter_settings_contract(tmp_path):
    source = tmp_path / 'pairings.py'
    source.write_text('''from src.compilers.base import BaseCompiler
from src.compilers.java import JavaCompiler
from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.modules.artifacts import Artifacts
from src.modules.compilation import Compilation
from src.modules.worker import RunOptions, WorkerInputs, WorkerResources, initialize_worker

def fields(compiler: KotlinCompiler) -> tuple[str, bool, str | None]:
    return (compiler.settings.backend, compiler.settings.dump_ir,
            compiler.settings.executable)

def attempt[S](compiler: type[BaseCompiler[S]], settings: S) -> BaseCompiler[S]:
    return compiler('src', settings=settings)

kotlin: BaseCompiler[KotlinSettings] = attempt(KotlinCompiler, KotlinSettings())
java: BaseCompiler[None] = attempt(JavaCompiler, None)
KotlinCompiler.get_compiler_version(KotlinSettings())
JavaCompiler.get_compiler_version(None)

def wiring(options: RunOptions, artifacts: Artifacts) -> None:
    kotlin: Compilation[KotlinSettings] = Compilation(
        options, artifacts, {'kotlin': KotlinCompiler}, settings=KotlinSettings())
    java: Compilation[None] = Compilation(
        options, artifacts, {'java': JavaCompiler}, settings=None)
    k_attempt: BaseCompiler[KotlinSettings] = kotlin.make_compiler('src')
    j_attempt: BaseCompiler[None] = java.make_compiler('src')
    inputs: WorkerInputs[KotlinSettings] = WorkerInputs(
        options, {}, 'version', KotlinCompiler, KotlinSettings())
    resources: WorkerResources[KotlinSettings] = initialize_worker(inputs)
    backend: str = resources.compiler_settings.backend
    Compilation(options, artifacts, {'kotlin': KotlinCompiler}, settings=None)  # mismatch
    Compilation(options, artifacts, {'java': JavaCompiler}, settings=KotlinSettings())  # mismatch
    WorkerInputs(options, {}, 'version', KotlinCompiler, None)  # mismatch
    WorkerInputs(options, {}, 'version', JavaCompiler, KotlinSettings())  # mismatch

attempt(KotlinCompiler, None)  # mismatch
attempt(JavaCompiler, KotlinSettings())  # mismatch
KotlinCompiler('src', settings=None)  # mismatch
JavaCompiler.get_compiler_version(KotlinSettings())  # mismatch
''')
    result = subprocess.run(
        [sys.executable, '-m', 'pyright', '--outputjson', str(source)],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode in (0, 1), result.stdout + result.stderr
    diagnostics = [item for item in json.loads(result.stdout)['generalDiagnostics']
                   if Path(item['file']) == source]
    expected_lines = {index for index, line in enumerate(
        source.read_text().splitlines()) if '# mismatch' in line}
    assert {item['range']['start']['line'] for item in diagnostics} == expected_lines
    assert all(item['severity'] == 'error' for item in diagnostics)
