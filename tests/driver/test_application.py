import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock, call, patch
import zipfile

import pytest

from src.compilers.groovy import GroovyCompiler
from src.compilers.java import JavaCompiler
from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.compilers.scala import ScalaCompiler
from src.modules import generation, scheduler
from src.modules.worker_reporting import Reporting
from tests.driver.fixtures import replay
from tests.driver.support import ROOT


def test_main_configures_before_constructing_and_starting() -> None:
    import hephaestus

    arguments = ['--language', 'java']
    options = hephaestus.parse_args(arguments)
    events = Mock()
    with (
        patch.object(hephaestus, 'parse_args', return_value=options) as parsing,
        patch.object(hephaestus, 'configure_generator') as configuration,
        patch.object(hephaestus, 'Fuzzer') as application,
    ):
        events.attach_mock(parsing, 'parse')
        events.attach_mock(configuration, 'configure')
        events.attach_mock(application, 'application')
        hephaestus.main(arguments)
    assert events.mock_calls == [
        call.parse(arguments), call.configure(options),
        call.application(options), call.application().start(),
    ]


def test_application_wires_independent_options_and_components(tmp_path: Path) -> None:
    import hephaestus

    options = hephaestus.parse_args([
        '--language', 'java', '-b', str(tmp_path / 'bugs'), '-n', 'session',
    ])
    application = hephaestus.Fuzzer(options)
    other = hephaestus.Fuzzer(options)
    assert application.options is not options
    assert application.options is not other.options
    options.transformation_types.clear()
    assert application.options.transformation_types
    for component in (application.artifacts, application.generation,
                      application.compilation, application.coverage,
                      application.reporting, application.evaluation,
                      application.scheduler):
        assert component.options is application.options
    assert type(application.scheduler) is hephaestus.Scheduler
    assert not hasattr(application, 'runner')
    assert application.generation.artifacts is application.artifacts
    assert application.evaluation.compilation is application.compilation
    assert application.evaluation.coverage is application.coverage
    assert application.evaluation.reporting is application.reporting
    assert application.scheduler.generation is application.generation
    assert application.scheduler.evaluation is application.evaluation
    assert application.scheduler.reporting is application.reporting
    assert application.reporting.stats is not other.reporting.stats
    assert list(tmp_path.iterdir()) == []


def test_scheduler_requires_explicit_compiler_class() -> None:
    import hephaestus

    fuzzer = hephaestus.Fuzzer(hephaestus.parse_args(['--language', 'java']))
    with pytest.raises(TypeError, match='compiler_cls'):
        hephaestus.Scheduler(
            fuzzer.options, fuzzer.generation, fuzzer.evaluation, fuzzer.reporting,
            compiler_settings=None)
    scheduler = hephaestus.Scheduler(
        fuzzer.options, fuzzer.generation, fuzzer.evaluation, fuzzer.reporting,
        GroovyCompiler, compiler_settings=None)
    assert scheduler.compiler_cls is GroovyCompiler
    assert scheduler.worker_inputs('version').compiler_cls is GroovyCompiler


def test_manager_is_cached_per_process_and_shared_with_owners() -> None:
    import hephaestus

    application = hephaestus.Fuzzer(hephaestus.parse_args(['--language', 'java']))
    first, second = Mock(), Mock()
    with (
        patch.object(hephaestus.os, 'getpid', side_effect=[123, 123, 456]),
        patch.object(hephaestus, 'create_manager', side_effect=[first, second]) as create,
    ):
        assert application.setup_crossmodule_manager('version') is first
        assert application.setup_crossmodule_manager('version') is first
        assert application.setup_crossmodule_manager('version') is second
    assert create.call_args_list == [
        call(application.options, 'version', application.scheduler.compiler_cls),
        call(application.options, 'version', application.scheduler.compiler_cls),
    ]
    assert application.manager_pid == 456
    for component in (application.generation, application.evaluation,
                      application.reporting):
        assert component.manager is second


@pytest.mark.parametrize('language, compiler_cls', [
    ('kotlin', KotlinCompiler), ('java', JavaCompiler),
    ('groovy', GroovyCompiler), ('scala', ScalaCompiler),
])
def test_fuzzer_selects_and_supplies_settings(
        language: str, compiler_cls: type[KotlinCompiler] | type[JavaCompiler] |
        type[GroovyCompiler] | type[ScalaCompiler]) -> None:
    import hephaestus

    options = hephaestus.parse_args([
        '--language', language, '--backend', 'wasm', '--dump-ir'])
    fuzzer = hephaestus.Fuzzer(options)
    settings = fuzzer.compilation.settings
    if language == 'kotlin':
        assert settings == KotlinSettings('wasm', True)
    else:
        assert settings is None
    assert fuzzer.scheduler.compiler_cls is compiler_cls
    assert fuzzer.scheduler.compiler_settings is settings
    inputs = fuzzer.scheduler.worker_inputs('version')
    assert inputs.compiler_cls is compiler_cls
    assert inputs.compiler_settings is settings
    assert fuzzer.compilation.make_compiler('src').settings is settings


def test_fuzzer_ignores_non_cli_executable_attribute() -> None:
    import hephaestus

    options = hephaestus.parse_args(['--backend', 'wasm', '--dump-ir'])
    options.compiler_executable = '/test-only/kotlinc'
    fuzzer = hephaestus.Fuzzer(options)
    assert fuzzer.compilation.settings == KotlinSettings('wasm', True)
    assert fuzzer.compilation.version_command() == KotlinCompiler.get_compiler_version(
        fuzzer.compilation.settings)


def test_injected_kotlin_subclass_controls_settings_selection() -> None:
    import hephaestus

    class InjectedKotlin(KotlinCompiler):
        pass

    # Selection follows the adapter, not the language key.
    options = hephaestus.parse_args(['--language', 'java', '--backend', 'js', '--dump-ir'])
    fuzzer = hephaestus.Fuzzer(options, {'java': InjectedKotlin})
    assert fuzzer.compilation.settings == KotlinSettings('js', True)
    compiler = fuzzer.compilation.make_compiler('src')
    assert type(compiler) is InjectedKotlin
    assert compiler.settings is fuzzer.compilation.settings
    assert fuzzer.compilation.version_command() == InjectedKotlin.get_compiler_version(
        fuzzer.compilation.settings)
    assert fuzzer.scheduler.worker_inputs('version').compiler_cls is InjectedKotlin


@pytest.mark.parametrize('compiler_cls', [JavaCompiler, GroovyCompiler, ScalaCompiler])
def test_non_kotlin_adapter_under_kotlin_key_receives_none(compiler_cls) -> None:
    import hephaestus

    options = hephaestus.parse_args(['--language', 'kotlin', '--backend', 'wasm', '--dump-ir'])
    fuzzer = hephaestus.Fuzzer(options, {'kotlin': compiler_cls})
    assert fuzzer.compilation.settings is None
    assert fuzzer.scheduler.compiler_settings is None
    inputs = fuzzer.scheduler.worker_inputs('version')
    assert inputs.compiler_cls is compiler_cls
    assert inputs.compiler_settings is None
    compiler = fuzzer.compilation.make_compiler('src')
    assert type(compiler) is compiler_cls
    assert compiler.settings is None
    assert fuzzer.compilation.version_command() == compiler_cls.get_compiler_version()


@pytest.mark.parametrize('language', ['kotlin', 'java', 'groovy', 'scala'])
@pytest.mark.parametrize('cast_numbers', [False, True])
def test_generation_passes_parser_boolean_options_directly_to_translator(
        tmp_path: Path, language: str, cast_numbers: bool,
        monkeypatch: pytest.MonkeyPatch) -> None:
    import hephaestus

    source = tmp_path / 'input.bin'
    replay(source, language)
    arguments = ['--language', language, '-P', '--replay', str(source),
                 '-b', str(tmp_path / 'bugs'), '-n', 'session']
    if cast_numbers:
        arguments.append('--cast-numbers')
    options = hephaestus.parse_args(arguments)
    assert type(options.options['Translator']) is dict
    assert options.options['Translator'] == {'cast_numbers': cast_numbers}
    assert type(options.options['Translator']['cast_numbers']) is bool
    fuzzer = hephaestus.Fuzzer(options)
    translator_options = fuzzer.options.options['Translator']
    assert type(translator_options) is dict
    assert type(translator_options['cast_numbers']) is bool
    translator = Mock(wraps=generation.TRANSLATORS[language])
    monkeypatch.setitem(generation.TRANSLATORS, language, translator)
    monkeypatch.setattr(generation.cfg.prob, 'crossmodule_probability', 0.0)
    result = fuzzer.generation.gen_program(1, str(tmp_path / 'batch/src'))
    assert not result.failed, result.stats
    translator.assert_called_once_with('src.d1', translator_options)
    assert translator.call_args.args[1] is translator_options
    assert translator_options == {'cast_numbers': cast_numbers}


@pytest.mark.parametrize('method', [None, 'spawn', 'fork'])
def test_scheduling_logs_and_aggregates_with_parent_reporting(
        tmp_path: Path, method: str | None, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    import hephaestus

    source = tmp_path / 'input.bin'
    replay(source, 'java')
    compiler = tmp_path / 'javac'
    compiler.write_text('#!' + sys.executable + '\n' +
                        (ROOT / 'tests/driver/fake_compiler.py').read_text())
    compiler.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('DRIVER_COMPILER_MODE', 'success')
    monkeypatch.setattr(generation.cfg.prob, 'crossmodule_probability', 0.0)
    arguments = ['--language', 'java', '-P', '-i', '2', '--replay', str(source),
                 '-b', str(tmp_path / 'bugs'), '-n', 'session',
                 '-F', str(tmp_path / 'run.log')]
    if method is not None:
        arguments += ['--workers', '2']
        monkeypatch.setattr(scheduler.mp, 'Pool', mp.get_context(method).Pool)
    fuzzer = hephaestus.Fuzzer(hephaestus.parse_args(arguments))
    reporter = fuzzer.scheduler.reporting
    assert reporter is fuzzer.reporting is fuzzer.evaluation.reporting
    assert type(reporter) is Reporting
    parent_pid = os.getpid()
    events = []
    logging = reporter.logging
    update_stats = reporter.update_stats
    loop_name = '_run' if method is None else '_run_parallel'
    run = getattr(fuzzer.scheduler, loop_name)
    finish = fuzzer.scheduler._finish

    def record_run(*args):
        assert os.getpid() == parent_pid
        assert fuzzer.scheduler.reporting is reporter
        events.append((loop_name, os.getpid()))
        run(*args)

    def record_logging(version):
        events.append(('logging', os.getpid()))
        logging(version)

    def record_update(*args):
        events.append(('update_stats', os.getpid()))
        update_stats(*args)

    def record_finish():
        assert os.getpid() == parent_pid
        assert fuzzer.scheduler.reporting is reporter
        assert type(fuzzer.scheduler.reporting) is Reporting
        events.append(('_finish', os.getpid()))
        finish()

    with (
        patch.object(fuzzer.scheduler, loop_name, side_effect=record_run) as loop,
        patch.object(fuzzer.scheduler, '_finish', side_effect=record_finish) as final,
        patch.object(reporter, 'logging', side_effect=record_logging) as log,
        patch.object(reporter, 'update_stats', side_effect=record_update) as update,
    ):
        fuzzer.start()
    loop.assert_called_once()
    final.assert_called_once_with()
    log.assert_called_once_with('hermetic compiler 1.0')
    assert update.call_count == 2
    assert events == [(loop_name, parent_pid), ('logging', parent_pid),
                      ('update_stats', parent_pid), ('update_stats', parent_pid),
                      ('_finish', parent_pid)]
    assert reporter.stats['totals'] == {'passed': 2, 'failed': 0}
    assert 'Total faults: 0' in capsys.readouterr().out


def test_import_does_not_parse_arguments_run_tools_or_create_files(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(ROOT)
    result = subprocess.run([sys.executable, '-c', '''
import sys
from pathlib import Path
from unittest.mock import patch
from src.generators.config import cfg
before = cfg.to_json()
sys.argv = ['unrelated', '--unknown-option']
with patch('subprocess.run', side_effect=AssertionError('tool execution')):
    import hephaestus
assert cfg.to_json() == before
assert sys.argv == ['unrelated', '--unknown-option']
assert list(Path.cwd().iterdir()) == []
assert hephaestus.Fuzzer.__module__ == 'hephaestus'
'''], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('workers', [None, 2])
def test_script_runs_replay_with_real_workers(tmp_path: Path, workers: int | None) -> None:
    source = tmp_path / 'input.bin'
    replay(source, 'java')
    tools = tmp_path / 'tools'
    tools.mkdir()
    compiler = tools / 'javac'
    compiler.write_text('#!' + sys.executable + '\n' +
                        (ROOT / 'tests/driver/fake_compiler.py').read_text())
    compiler.chmod(0o755)
    temporary = tmp_path / 'temporary'
    temporary.mkdir()
    environment = os.environ.copy()
    environment['PATH'] = str(tools) + os.pathsep + environment['PATH']
    environment['TMPDIR'] = str(temporary)
    environment['DRIVER_TOOL_LOG'] = str(tmp_path / 'commands.jsonl')
    environment['DRIVER_COMPILER_MODE'] = 'success'
    command = [sys.executable, str(ROOT / 'hephaestus.py'), '--language', 'java',
               '-P', '-i', '2', '-t', '0', '--replay', str(source),
               '--keep-everything', '-b', str(tmp_path / 'bugs'), '-n', 'session',
               '-F', str(tmp_path / 'run.log')]
    if workers is not None:
        command += ['--workers', str(workers)]
    result = subprocess.run(command, cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    run = tmp_path / 'bugs/session'
    stats = json.loads((run / 'stats.json').read_text())
    assert stats['totals'] == {'passed': 2, 'failed': 0}
    assert stats['Info']['compiler'] == 'hermetic compiler 1.0'
    assert json.loads((run / 'faults.json').read_text()) == {}
    assert not (run / 'tmp').exists()
    for pid in (1, 2):
        assert (run / str(pid) / 'Main.java').is_file()
        assert (run / str(pid) / 'Main.java.bin').is_file()
    commands = [json.loads(line)['arguments'] for line in
                (tmp_path / 'commands.jsonl').read_text().splitlines()]
    assert commands[0] == ['-version']
    assert len(commands) == 3


def test_isolated_distribution_import_and_console_help(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    for name in ('pyproject.toml', 'README.md', 'LICENSE', 'hephaestus.py'):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / 'src', source / 'src',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    wheels = tmp_path / 'wheels'
    result = subprocess.run([
        sys.executable, '-m', 'pip', 'wheel', str(source), '--no-deps',
        '--wheel-dir', str(wheels),
    ], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    wheel, = wheels.iterdir()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert not any(name.startswith('src/driver/') for name in names)
        assert 'src/modules/runner.py' not in names
        assert 'src/modules/runtime.py' not in names
        assert 'src/modules/reporting.py' not in names
        for component in ('artifacts', 'compilation', 'coverage', 'evaluation',
                          'generation', 'models', 'worker_reporting', 'scheduler', 'worker',
                          'processor', 'crossmodule', 'logging'):
            assert f'src/modules/{component}.py' in names
        assert 'src/oracles/compilation.py' in names
    installed = tmp_path / 'installed'
    result = subprocess.run([
        sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-compile',
        '--target', str(installed), str(wheel),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    outside = tmp_path / 'outside'
    outside.mkdir()
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(installed)
    result = subprocess.run([sys.executable, '-c', '''
import hephaestus
import src.modules.scheduler
import src.modules.worker
import src.modules.worker_reporting
from pathlib import Path
assert Path(hephaestus.__file__).parent.name == 'installed'
assert hephaestus.Fuzzer.__module__ == 'hephaestus'
assert src.modules.scheduler.Scheduler.__module__ == 'src.modules.scheduler'
assert src.modules.scheduler.initialize_scheduler_worker.__module__ == 'src.modules.scheduler'
assert src.modules.worker.WorkerInputs.__module__ == 'src.modules.worker'
assert src.modules.worker_reporting.Reporting.__module__ == 'src.modules.worker_reporting'
'''], cwd=outside, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    for command in ([str(installed / 'bin/hephaestus'), '--help'],
                    [sys.executable, str(installed / 'hephaestus.py'), '--help']):
        result = subprocess.run(command, cwd=outside, env=environment,
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'usage:' in result.stdout
        assert '--workers' in result.stdout
    assert list(outside.iterdir()) == []
