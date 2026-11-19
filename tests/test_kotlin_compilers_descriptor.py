from __future__ import annotations

import io
import json
import zipfile

import pytest

from tests.kotlin_compilers_fakes import make_compiler_zip, make_klib, manifest_text
from src.tools.kotlin_compilers.descriptor import (
    DescriptorError, abi_version, build_descriptor, inliner_flag_style, read_descriptor,
    write_descriptor,
)
from src.tools.kotlin_compilers.layout import Layout
from src.tools.kotlin_compilers.releases import extract_compiler_zip


def make_tree(tmp_path, version, abi, wasm_launcher=False):
    directory = tmp_path / version
    archive = tmp_path / f'{version}.zip'
    archive.write_bytes(make_compiler_zip(wasm_launcher))
    extract_compiler_zip(archive, directory / 'kotlinc')
    (directory / 'stdlib').mkdir()
    for name in ('kotlin-stdlib-js.klib', 'kotlin-stdlib-wasm-js.klib',
                 'kotlin-stdlib-wasm-wasi.klib'):
        (directory / 'stdlib' / name).write_bytes(make_klib(abi))
    stdlib = directory / 'native/klib/common/stdlib/default'
    stdlib.mkdir(parents=True)
    (stdlib / 'manifest').write_text(manifest_text(abi))
    (directory / 'native/bin').mkdir()
    (directory / 'native/bin/kotlinc-native').write_text('#!/bin/sh\n')
    return directory


def test_2_2_0_quirks(tmp_path):
    directory = make_tree(tmp_path, '2.2.0', '2.2.0')
    data = build_descriptor(directory, version='2.2.0', origin='release', host='macos-aarch64',
                            backends={'jvm', 'js', 'wasm', 'native'})
    assert data['inliner_flag'] == 'boolean'
    assert data['abi_version'] == '2.2.0'
    assert data['backends']['wasm'] == {
        'launcher': 'kotlinc/bin/kotlinc-wasm', 'args': [], 'shim': True,
        'stdlib': 'stdlib/kotlin-stdlib-wasm-js.klib'}
    # The zip ships the JS klib, so js uses it; the Wasm klib only exists in stdlib/.
    assert data['backends']['js']['stdlib'] == 'kotlinc/lib/kotlin-stdlib-js.klib'
    assert data['backends']['jvm']['stdlib'] == 'kotlinc/lib/kotlin-stdlib.jar'
    assert list(data['backends']) == ['jvm', 'js', 'wasm', 'native']


def test_2_3_0_quirks(tmp_path):
    directory = make_tree(tmp_path, '2.3.0', '2.3.0')
    data = build_descriptor(directory, version='2.3.0', origin='release', host='linux-x86_64',
                            backends={'wasm'})
    assert data['inliner_flag'] == 'mode'
    assert list(data['backends']) == ['wasm']
    assert data['backends']['wasm']['launcher'] == 'kotlinc/bin/kotlinc-wasm'
    assert (data['backends']['wasm']['args'], data['backends']['wasm']['shim']) == ([], True)


def test_dedicated_wasm_launcher_detected_from_files(tmp_path):
    directory = make_tree(tmp_path, '2.5.0-Beta2', '2.5.0', wasm_launcher=True)
    data = build_descriptor(directory, version='2.5.0-Beta2', origin='release',
                            host='macos-aarch64', backends={'wasm'})
    assert data['backends']['wasm'] == {'launcher': 'kotlinc/bin/kotlinc-wasm', 'args': [],
                                        'stdlib': 'stdlib/kotlin-stdlib-wasm-js.klib'}
    assert data['abi_version'] == '2.5.0'


def test_stdlib_prefers_the_zip_copy(tmp_path):
    directory = tmp_path / '2.5.0-Beta2'
    archive = tmp_path / 'z.zip'
    archive.write_bytes(make_compiler_zip(True, abi='2.5.0', klibs=(
        'kotlin-stdlib-js.klib', 'kotlin-stdlib-wasm-js.klib', 'kotlin-stdlib-wasm-wasi.klib')))
    extract_compiler_zip(archive, directory / 'kotlinc')
    data = build_descriptor(directory, version='2.5.0-Beta2', origin='release',
                            host='macos-aarch64', backends={'js', 'wasm'})
    assert data['backends']['js']['stdlib'] == 'kotlinc/lib/kotlin-stdlib-js.klib'
    assert data['backends']['wasm']['stdlib'] == 'kotlinc/lib/kotlin-stdlib-wasm-js.klib'
    assert data['abi_version'] == '2.5.0'
    write_descriptor(directory, data)
    assert read_descriptor(directory) == data


def test_install_without_kotlinc_wasm_still_goes_through_kotlinc_js(tmp_path):
    directory = make_tree(tmp_path, '2.3.0', '2.3.0')
    (directory / 'kotlinc/bin/kotlinc-wasm').unlink()
    data = build_descriptor(directory, version='2.3.0', origin='release', host='macos-aarch64',
                            backends={'wasm'})
    assert data['backends']['wasm'] == {
        'launcher': 'kotlinc/bin/kotlinc-js', 'args': ['-Xwasm', '-Xwasm-target=wasm-js'],
        'stdlib': 'stdlib/kotlin-stdlib-wasm-js.klib'}


@pytest.mark.parametrize('version, style', [
    ('2.1.20', 'boolean'), ('2.2.0', 'boolean'), ('2.3.0', 'mode'),
    ('2.5.0-Beta2', 'mode'), ('2.5.20-dev-3321', 'mode'), ('2.5.0-RC-294', 'mode')])
def test_inliner_flag_style(version, style):
    assert inliner_flag_style(version) == style


def test_abi_falls_back_to_version_without_stdlib(tmp_path):
    directory = tmp_path / 'x'
    archive = tmp_path / 'x.zip'
    archive.write_bytes(make_compiler_zip(klibs=()))
    extract_compiler_zip(archive, directory / 'kotlinc')
    data = build_descriptor(directory, version='2.4.21', origin='release', host='macos-aarch64',
                            backends={'jvm'})
    assert data['abi_version'] == '2.4.0'


def test_round_trip_and_validation(tmp_path):
    directory = make_tree(tmp_path, '2.3.0', '2.3.0')
    data = build_descriptor(directory, version='2.3.0', origin='release', host='macos-aarch64',
                            backends={'js', 'native'})
    write_descriptor(directory, data)
    assert read_descriptor(directory) == data
    (directory / 'kotlinc/lib/kotlin-stdlib-js.klib').unlink()
    with pytest.raises(DescriptorError, match='stdlib'):
        read_descriptor(directory)


def test_corrupt_descriptor_counts_as_not_installed(tmp_path):
    layout = Layout(tmp_path)
    directory = make_tree(tmp_path, '2.3.0', '2.3.0')
    (directory / 'compiler.json').write_text('{')
    with pytest.raises(DescriptorError, match='invalid JSON'):
        read_descriptor(directory)
    assert layout.descriptor('2.3.0') is None
    (directory / 'compiler.json').write_text(json.dumps({'schema': 99}))
    assert layout.descriptor('2.3.0') is None
    assert read_descriptor(tmp_path / 'absent') is None


def test_packed_klib_manifest_with_dot_prefix(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('./default/manifest', manifest_text('2.3.0'))
    directory = tmp_path / 'v'
    (directory / 'stdlib').mkdir(parents=True)
    (directory / 'stdlib/kotlin-stdlib-js.klib').write_bytes(buffer.getvalue())
    assert abi_version(directory, '2.3.21') == '2.3.0'
