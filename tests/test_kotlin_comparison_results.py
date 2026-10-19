import os
from pathlib import Path
import sys

import pytest

from src.compilers.base import BaseCompiler
from src.compilers.kotlin import (
    KOTLIN_COMPARISON_VARIANTS, KotlinCompiler, KotlinSettings, _compiler_executable,
)
from src.compilers.results import CompilationRequest, Diagnostic
from tests.test_compilation_variants import components


@pytest.mark.parametrize('backend', ['native', 'js', 'wasm'])
def test_isolated_klib_commands(tmp_path, backend):
    root = tmp_path / 'source with spaces'
    root.mkdir()
    source = root / 'føø-bar.kt'
    source.write_text('val x = 1')
    destination = tmp_path / backend
    compiler = KotlinCompiler(str(root), module_name='d2',
                              output_directory=str(destination),
                              klib_output=True,
                              dependency_klibs=['d1-' + backend + '.klib'],
                              friend_klibs=['d1-' + backend + '.klib'],
                              settings=KotlinSettings(backend))
    command = compiler.get_compiler_cmd()
    assert command[0].endswith('/kotlinc-' + backend)
    assert command[1] == str(source)
    assert command.count(str(source)) == 1
    if backend == 'native':
        assert command[command.index('-o') + 1] == str(destination / 'd2')
        assert command[command.index('-produce') + 1] == 'library'
    else:
        assert '-Xir-produce-klib-file' in command
        assert command[command.index('-ir-output-dir') + 1] == str(destination)
        assert '-Xwasm' not in command
        libraries = command[command.index('-libraries') + 1]
        assert ('kotlin-stdlib-wasm-js-' in libraries) == (backend == 'wasm')
    assert compiler.get_dependency_artifact_filename() == 'd2.klib'


def test_dependency_artifact_validation_is_compiler_owned(tmp_path):
    klib = tmp_path / 'd1.klib'
    (klib / 'default').mkdir(parents=True)
    (klib / 'default' / 'manifest').write_text('depends=stdlib\n')
    assert KotlinCompiler('source').validate_dependency_artifact(str(klib))
    assert not KotlinCompiler('source').validate_dependency_artifact(str(tmp_path / 'missing.klib'))
    assert not BaseCompiler('source', settings=None).validate_dependency_artifact(str(klib))


@pytest.mark.parametrize('backend', ['native', 'js', 'wasm'])
def test_version_probe_preparation(tmp_path, backend):
    settings = KotlinSettings(backend)
    assert KotlinCompiler.get_compiler_version(settings)[1:] == ['-version']
    command = KotlinCompiler.get_compiler_version(settings, str(tmp_path))
    if backend == 'native':
        assert command[1:] == ['-version']
    else:
        assert command[command.index('-ir-output-name') + 1]
        assert command[command.index('-ir-output-dir') + 1] == str(tmp_path)


def test_comparison_preset_is_plain_immutable_settings():
    assert tuple(KOTLIN_COMPARISON_VARIANTS) == ('js', 'native', 'wasm')
    assert all(settings.backend == variant_id
               for variant_id, settings in KOTLIN_COMPARISON_VARIANTS.items())
    wasm = KotlinSettings('wasm')
    assert KOTLIN_COMPARISON_VARIANTS['wasm'] == wasm
    assert _compiler_executable(wasm).endswith('/kotlinc-wasm')
    override = KotlinSettings('wasm', executable='/custom/kotlinc')
    assert _compiler_executable(override) == '/custom/kotlinc'
    assert KotlinCompiler('source', settings=override).get_compiler_cmd()[0] == '/custom/kotlinc'
    with pytest.raises(TypeError):
        KOTLIN_COMPARISON_VARIANTS['jvm'] = KotlinSettings()


@pytest.mark.parametrize('prefix', ['', 'e: '])
def test_structured_multiline_diagnostics_and_explicit_aliases(prefix):
    compiler = KotlinCompiler('source', source_aliases={
        'C:/my source/λ-file.kt': 'program.kt'}, dependency_aliases={
        '/libs/d1-js.klib': 'module:d1'})
    parsed = compiler.collect_diagnostics(
        '\x1b[31m' + prefix + 'C:\\my source\\λ-file.kt:2:3: error: [TYPE_MISMATCH] '
        'wrong type in /libs/d1-js.klib\x1b[0m\r\n'
        '    expected: List<String>\r\n    actual: List<Int>\r\n')
    assert parsed.complete
    assert parsed.errors == (Diagnostic('program.kt', 2, 3,
        'wrong type in module:d1\nexpected: List<String>\nactual: List<Int>',
        'TYPE_MISMATCH'),)
    other = compiler.collect_diagnostics('e: file:///C:/my%20source/λ-file.kt: (2, 3): wrong')
    assert other.errors[0].source == 'program.kt'


def test_unparsed_failures_and_filters_keep_raw_evidence():
    compiler = KotlinCompiler('source', ['ignore me'])
    parsed = compiler.collect_diagnostics(
        'source.kt:1:1: error: ignore me\nopaque fatal output\n')
    assert not parsed.complete
    assert parsed.unparsed == ('opaque fatal output',)
    assert len(parsed.errors) == 1
    assert compiler.comparison_errors(parsed.errors) == ()
    assert compiler.collect_diagnostics('warning: ignored\n').errors == ()
    assert compiler.collect_diagnostics('error: unsupported argument').errors[0].source is None


def test_kotlin_cli_source_excerpt_and_caret_are_presentation_only():
    compiler = KotlinCompiler('source', source_aliases={'/tmp/src/problem.kt': 'program.kt'})
    parsed = compiler.collect_diagnostics(
        '/tmp/src/problem.kt:2:17: error: expected Int\n'
        'val answer: Int = "not an Int"\n'
        '                ^\n')
    assert parsed.complete
    assert parsed.errors == (Diagnostic('program.kt', 2, 17, 'expected Int'),)


def test_realpath_aliases_are_one_logical_source(tmp_path):
    source = tmp_path / 'fóo-bar.kt'
    source.write_text('val x: Int = "bad"')
    compiler = KotlinCompiler(str(source), source_aliases={str(source): 'program.kt'})
    reported = str(source.resolve())
    assert compiler.collect_diagnostics(
        reported + ':1:1: error: wrong type').errors[0].source == 'program.kt'


def test_shell_return_codes_and_missing_executable(tmp_path):
    _, _, compilation = components(tmp_path)
    tool = tmp_path / 'tool'
    tool.write_text('#!' + sys.executable + '\nimport sys\nprint(sys.argv[1])\nsys.exit(7)\n')
    tool.chmod(0o755)
    result = compilation.run_process([str(tool), 'a-space-λ-hyphen'])
    assert result.returncode == 7
    assert result.output.strip() == 'a-space-λ-hyphen'
    assert not result.status
    missing = compilation.run_process([str(tmp_path / 'missing')])
    assert missing.returncode is not None and missing.returncode != 0
    assert missing.launch_failure is None
    assert 'missing' in missing.output.lower()


def test_shell_runner_expands_kotlin_home_executable(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    executable = home / 'kotlin/kotlin-native/dist/bin/kotlinc-native'
    executable.parent.mkdir(parents=True)
    executable.write_text(
        '#!' + sys.executable + '\nprint("HOME_EXECUTABLE_RAN")\n')
    executable.chmod(0o755)
    monkeypatch.setenv('HOME', str(home))
    source = tmp_path / 'source.kt'
    source.write_text('val value = 1')
    _, _, compilation = components(tmp_path)
    compiler = KotlinCompiler(str(source), settings=KotlinSettings('native'))

    result = compilation.run_process(compiler.get_compiler_cmd())

    assert result.status
    assert 'HOME_EXECUTABLE_RAN' in result.output


def test_runner_classifies_missing_dependency_artifact_and_runs_remaining_variants(tmp_path):
    _, _, compilation = components(tmp_path)
    source = tmp_path / 'program.kt'
    source.write_text('val x = 1')
    requests = []
    for name in ('native', 'js', 'wasm'):
        destination = tmp_path / name
        destination.mkdir()
        compiler = KotlinCompiler(str(source), module_name='d1',
                                  output_directory=str(destination),
                                  klib_output=True,
                                  settings=KotlinSettings(name, executable='/usr/bin/true'))
        requests.append(CompilationRequest(name, compiler, str(destination),
                                           str(destination / 'd1.klib')))
    results = compilation.run_variants(requests)
    assert len(results) == 3
    assert all(result.command.returncode == 0 for result in results)
    assert all(not result.dependency_artifact_valid and result.incomplete_reason
               for result in results)
