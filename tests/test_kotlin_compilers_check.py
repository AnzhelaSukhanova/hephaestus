from __future__ import annotations

import json
import os
import sys

import pytest

from tests.kotlin_compilers_fakes import make_compiler_zip, make_klib, manifest_text
from src.tools.kotlin_compilers.check import FULL_PATH_RENDERER, check
from src.tools.kotlin_compilers.descriptor import build_descriptor, write_descriptor
from src.tools.kotlin_compilers.layout import Layout
from src.tools.kotlin_compilers.versions import parse_version_list


# A stand-in compiler: logs its arguments, answers -version, reports Broken.kt's
# error with its full path (as the FullPath renderer does) and writes the artifact
# check expects.
FAKE_LAUNCHER_LINES = [
    'import os, sys, zipfile',
    'args = sys.argv[1:]',
    'def value(flag):',
    '    return args[args.index(flag) + 1]',
    'def klib(path):',
    "    with zipfile.ZipFile(path, 'w') as archive:",
    "        abi = os.environ.get('FAKE_ABI', ABI)",
    "        archive.writestr('default/manifest', 'abi_version=' + abi + chr(10))",
    "with open(os.environ['FAKE_LOG'], 'a') as log:",
    "    log.write(os.path.basename(sys.argv[0]) + ' ' + ' '.join(args) + chr(10))",
    "if '-version' in args:",
    "    name = os.path.basename(sys.argv[0])",
    "    print('info: ' + name + ' ' + os.environ.get('FAKE_VERSION', VERSION) + ' (JRE 26)')",
    '    sys.exit(0)',
    "broken = [a for a in args if a.endswith('Broken.kt')]",
    'if broken:',
    "    shown = os.path.basename(broken[0]) if os.environ.get('FAKE_RELATIVE') else broken[0]",
    "    print(shown + ':3:19: error: initializer type mismatch')",
    '    sys.exit(1)',
    "if '-d' in args:",
    "    open(value('-d'), 'w').close()",
    "elif '-ir-output-dir' in args:",
    "    klib(os.path.join(value('-ir-output-dir'), value('-ir-output-name') + '.klib'))",
    'else:',
    "    data = [a for a in args if a.startswith('-Xkonan-data-dir=')][0].split('=', 1)[1]",
    "    if os.environ.get('FAKE_FETCH'):",
    "        os.makedirs(os.path.join(data, 'dependencies', 'llvm-19-aarch64-macos'))",
    "    if os.environ.get('FAKE_AIRPLANE'):",
    "        print('error: Cannot find a dependency locally: llvm-19')",
    '        sys.exit(1)',
    "    klib(value('-o') + '.klib')",
    "if os.environ.get('FAKE_WARN'):",
    "    print('warning: the standard library version differs')",
]


def fake_install(layout, version, abi, backends):
    directory = layout.compiler_dir(version)
    script = '\n'.join([f'#!{sys.executable}', f'ABI = {abi!r}', f'VERSION = {version!r}',
                        *FAKE_LAUNCHER_LINES]) + '\n'
    for launcher in ('kotlinc/bin/kotlinc-jvm', 'kotlinc/bin/kotlinc-js',
                     'kotlinc/bin/kotlinc-wasm', 'native/bin/kotlinc-native'):
        path = directory / launcher
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(script)
        path.chmod(0o755)
    (directory / 'kotlinc/lib').mkdir()
    (directory / 'kotlinc/lib/kotlin-stdlib.jar').write_bytes(make_compiler_zip())
    (directory / 'stdlib').mkdir()
    for name in ('kotlin-stdlib-js.klib', 'kotlin-stdlib-wasm-js.klib',
                 'kotlin-stdlib-wasm-wasi.klib'):
        (directory / 'stdlib' / name).write_bytes(make_klib(abi))
    manifest = directory / 'native/klib/common/stdlib/default/manifest'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(manifest_text(abi))
    write_descriptor(directory, build_descriptor(directory, version=version, origin='release',
                                                 host='macos-aarch64', backends=backends))


@pytest.fixture
def fake_compilers(tmp_path, monkeypatch):
    layout = Layout(tmp_path / 'compilers')
    layout.ensure()
    fake_install(layout, '2.4.21', '2.4.0', {'jvm', 'js', 'wasm', 'native'})
    fake_install(layout, '2.3.21', '2.3.0', {'native'})
    os.symlink('2.4.21', layout.root / 'prerelease')
    layout.sync_json.write_text(json.dumps(
        {'resolved': {'prerelease': '2.4.21', 'master': '2.4.21'}}))
    log = tmp_path / 'calls.log'
    log.touch()
    monkeypatch.setenv('FAKE_LOG', str(log))
    for name in ('FAKE_ABI', 'FAKE_FETCH', 'FAKE_AIRPLANE', 'FAKE_WARN', 'FAKE_VERSION',
                 'FAKE_RELATIVE'):
        monkeypatch.delenv(name, raising=False)
    return layout, log


def declared(*selectors):
    return parse_version_list({'versions': list(selectors),
                               'fallbacks': {'master': 'prerelease'}})


def test_check_runs_each_real_directory_once(fake_compilers):
    layout, log = fake_compilers
    code, lines = check(layout, declared('prerelease', '2.4.21-native', '2.3.21-native'))
    assert code == 0, lines
    calls = log.read_text().splitlines()
    assert len(calls) == 5 * 3  # five compiler x backend pairs, three probes each
    compiles = [call for call in calls if '-version' not in call]
    assert all(FULL_PATH_RENDERER in call for call in compiles)
    native = [call for call in compiles if call.startswith('kotlinc-native')]
    assert len(native) == 4
    assert all('-produce library' in call and '-Xkonan-data-dir=' in call
               and '-Xklib-ir-inliner=full' in call for call in native)
    wasm = [call for call in compiles if call.startswith('kotlinc-wasm')]
    assert len(wasm) == 2 and all('kotlin-stdlib-wasm-js.klib' in call for call in wasm)
    jvm = [call for call in compiles if call.startswith('kotlinc-jvm')]
    assert all('-Xklib-ir-inliner' not in call and '.jar' in call for call in jvm)
    assert any('prerelease,2.4.21-native' in line and 'abi 2.4.0' in line
               and 'full-path error' in line and '-version 2.4.21' in line for line in lines)


def test_js_and_wasm_version_probes_get_output_arguments(fake_compilers):
    layout, log = fake_compilers
    assert check(layout, declared('prerelease'), ['2.4.21'])[0] == 0
    versions = [call.split() for call in log.read_text().splitlines() if '-version' in call]
    by_launcher = {call[0]: call[1:] for call in versions}
    for launcher in ('kotlinc-js', 'kotlinc-wasm'):
        assert by_launcher[launcher][:2] == ['-version', '-ir-output-name']
        assert '-ir-output-dir' in by_launcher[launcher]
    for launcher in ('kotlinc-jvm', 'kotlinc-native'):
        assert by_launcher[launcher] == ['-version']


def test_wrong_version_fails_the_version_probe(fake_compilers, monkeypatch):
    layout, _ = fake_compilers
    monkeypatch.setenv('FAKE_VERSION', '2.4.20')
    code, lines = check(layout, declared('prerelease'), ['2.4.21-js'])
    assert code == 1
    row = next(line for line in lines if line.startswith('2.4.21-js'))
    assert 'FAIL version ' in row and 'version: printed 2.4.20 != compiler.json 2.4.21' in row
    assert 'abi 2.4.0' in row  # the other probes still ran and passed


def test_error_without_the_full_path_fails_the_fullpath_probe(fake_compilers, monkeypatch):
    layout, _ = fake_compilers
    monkeypatch.setenv('FAKE_RELATIVE', '1')
    code, lines = check(layout, declared('prerelease'), ['2.4.21-jvm'])
    assert code == 1
    row = next(line for line in lines if line.startswith('2.4.21-jvm'))
    assert 'FAIL fullpath ' in row and 'fullpath: no error line starting with' in row


def test_boolean_inliner_style_passes_a_bare_flag(fake_compilers):
    layout, log = fake_compilers
    fake_install(layout, '2.2.0', '2.2.0', {'js'})
    assert check(layout, declared('2.2.0'), ['2.2.0-js'])[0] == 0
    compiles = [call for call in log.read_text().splitlines() if '-version' not in call]
    assert compiles and all(call.split()[-1] == '-Xklib-ir-inliner' for call in compiles)


def test_check_limits_to_the_suffix_backend(fake_compilers):
    layout, log = fake_compilers
    code, lines = check(layout, declared('prerelease'), ['master-native'])
    assert code == 0, lines
    assert [call.split()[0] for call in log.read_text().splitlines()] == ['kotlinc-native'] * 3


def test_check_detects_abi_mismatch(fake_compilers, monkeypatch):
    layout, _ = fake_compilers
    monkeypatch.setenv('FAKE_ABI', '9.9.0')
    code, lines = check(layout, declared('prerelease'), ['2.4.21-js'])
    assert code == 1
    assert 'abi_version 9.9.0 != compiler.json 2.4.0' in '\n'.join(lines)


def test_check_fails_when_native_fetches_dependencies(fake_compilers, monkeypatch):
    layout, _ = fake_compilers
    monkeypatch.setenv('FAKE_FETCH', '1')
    code, lines = check(layout, declared('prerelease'), ['2.3.21-native'])
    assert code == 1
    assert 'Native downloaded into dependencies/: llvm-19-aarch64-macos' in '\n'.join(lines)


def test_check_reports_airplane_mode_block(fake_compilers, monkeypatch):
    layout, _ = fake_compilers
    monkeypatch.setenv('FAKE_AIRPLANE', '1')
    code, lines = check(layout, declared('prerelease'), ['2.3.21-native'])
    assert code == 1
    assert 'Native tried to fetch LLVM' in '\n'.join(lines)


def test_check_reports_stdlib_warnings_and_missing_installs(fake_compilers, monkeypatch):
    layout, _ = fake_compilers
    monkeypatch.setenv('FAKE_WARN', '1')
    code, lines = check(layout, declared('prerelease'), ['2.4.21-jvm', '2.2.0', '2.3.21-js'])
    text = '\n'.join(lines)
    assert code == 1
    assert 'stdlib warning: warning: the standard library version differs' in text
    assert 'problem: 2.2.0: 2.2.0 is not installed' in text
    assert 'problem: 2.3.21-js: backend not installed in 2.3.21' in text
