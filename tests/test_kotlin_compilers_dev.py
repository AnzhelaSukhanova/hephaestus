from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.kotlin_compilers_fakes import install_fake_web, publish_dev, publish_release
from src.tools.kotlin_compilers.dev import (
    DEV_REPO, DevError, master_version, resolve_dependencies,
)
from src.tools.kotlin_compilers.layout import Layout
from src.tools.kotlin_compilers.releases import MAVEN_CENTRAL, artifact_url
from src.tools.kotlin_compilers.sync import list_lines, sync
from src.tools.kotlin_compilers.versions import parse_version_list


HOST = 'macos-aarch64'
DEV = '2.5.20-dev-3466'


@pytest.fixture
def web(monkeypatch):
    return install_fake_web(monkeypatch)


@pytest.fixture
def layout(tmp_path):
    return Layout(tmp_path / 'compilers')


def declared(*selectors, fallbacks=None):
    return parse_version_list({'versions': list(selectors), 'fallbacks': fallbacks or {}})


def descriptor(layout, version):
    return json.loads((layout.compiler_dir(version) / 'compiler.json').read_text())


def sync_state(layout):
    return json.loads(layout.sync_json.read_text())


@pytest.mark.parametrize('value, expected', [
    (None, None), ('', None), ('  ', None), (DEV, DEV), ('2.5.0-RC-294', '2.5.0-RC-294')])
def test_master_version(value, expected):
    assert master_version(value) == expected


@pytest.mark.parametrize('value, message', [
    ('latest', 'latest is unsupported'), ('2.5', 'not an exact'), ('master', 'not an exact')])
def test_master_version_rejects(value, message):
    with pytest.raises(DevError, match=message):
        master_version(value)


def test_pom_resolver_follows_runtime_closure(web):
    publish_dev(web, DEV)
    artifacts = {(a.group, a.name): a for a in resolve_dependencies(
        'org.jetbrains.kotlin', 'kotlin-compiler', DEV)}
    assert {name for _, name in artifacts} == {
        'kotlin-compiler', 'kotlin-stdlib-jdk8', 'kotlin-reflect', 'kotlinx-coroutines-core-jvm',
        'kotlin-build-tools-api', 'kotlin-stdlib', 'kotlin-stdlib-jdk7', 'annotations'}
    # Nearest wins: the dev stdlib (depth 2) beats coroutines' 1.8.20; annotations 23.0.0
    # (depth 2 via coroutines) beats 13.0 (depth 3 via stdlib).
    assert artifacts[('org.jetbrains.kotlin', 'kotlin-stdlib')].version == DEV
    assert artifacts[('org.jetbrains', 'annotations')].version == '23.0.0'
    assert artifacts[('org.jetbrains.kotlin', 'kotlin-compiler')].repository == DEV_REPO
    assert artifacts[('org.jetbrains.kotlin', 'kotlin-reflect')].repository == MAVEN_CENTRAL
    requested = [url for _, url in web.requests]
    for skipped in ('reflect-only-dependency', 'junit', 'optional-thing', 'managed-only'):
        assert not any(skipped in url for url in requested)


def test_pom_resolver_fails_clearly_for_missing_artifacts(web):
    publish_dev(web, DEV)
    del web.files[artifact_url(DEV_REPO, 'org.jetbrains.kotlin', 'kotlin-build-tools-api',
                               DEV, 'pom')]
    with pytest.raises(DevError, match='kotlin-build-tools-api.*not found'):
        resolve_dependencies('org.jetbrains.kotlin', 'kotlin-compiler', DEV)


def test_exact_master_installs_dev_build(web, layout):
    publish_dev(web, DEV)
    assert sync(layout, declared('master'), HOST, master=DEV) == 0
    directory = layout.compiler_dir(DEV)
    assert sorted(p.name for p in directory.iterdir()) == [
        'compiler.json', 'kotlinc', 'native', 'stdlib']
    assert sorted(p.name for p in (directory / 'kotlinc/lib').iterdir()) == [
        'annotations.jar', 'kotlin-build-tools-api.jar', 'kotlin-compiler.jar',
        'kotlin-reflect.jar', 'kotlin-stdlib-jdk7.jar', 'kotlin-stdlib-jdk8.jar',
        'kotlin-stdlib.jar', 'kotlinx-coroutines-core-jvm.jar']
    assert sorted(p.name for p in (directory / 'kotlinc/bin').iterdir()) == [
        'kotlinc-js', 'kotlinc-jvm', 'kotlinc-wasm']
    data = descriptor(layout, DEV)
    assert (data['origin'], data['abi_version'], data['inliner_flag']) == ('dev', '2.5.0', 'mode')
    assert data['backends']['wasm'] == {'launcher': 'kotlinc/bin/kotlinc-wasm', 'args': [],
                                        'stdlib': 'stdlib/kotlin-stdlib-wasm-js.klib'}
    assert data['backends']['jvm']['stdlib'] == 'kotlinc/lib/kotlin-stdlib.jar'
    assert 'airplaneMode = true' in (directory / 'native/konan/konan.properties').read_text()
    assert layout.symlink_target('master') == DEV
    state = sync_state(layout)
    assert state['resolved']['master'] == DEV and state['fallbacks_used'] == {}
    assert state['errors'] == []
    assert not any('github.com' in url for _, url in web.requests)


def test_wrapper_contents_and_behaviour(web, layout, tmp_path):
    publish_dev(web, DEV)
    assert sync(layout, declared('master-jvm'), HOST, master=DEV) == 0
    wrapper = layout.compiler_dir(DEV) / 'kotlinc/bin/kotlinc-jvm'
    text = wrapper.read_text()
    assert 'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler' in text
    assert '-cp "$LIB/*"' in text and 'JAVA_OPTS="-Xmx512M -Xms128M"' in text
    assert os.access(wrapper, os.X_OK)
    (tmp_path / 'jdk').mkdir()
    fake_java = tmp_path / 'jdk' / 'java'
    fake_java.write_text('#!/bin/sh\n'
                         'if [ "$1" = -version ]; then echo \'openjdk version "26"\' >&2; exit 0; fi\n'
                         'for a in "$@"; do echo "$a"; done\n')
    fake_java.chmod(0o755)
    output = subprocess.run([str(wrapper), '-Dfoo=1', '-J-Xss4m', '-d', 'out', 'A.kt'],
                            env={**os.environ, 'JAVACMD': str(fake_java), 'JAVA_OPTS': '-Xmx1g'},
                            capture_output=True, text=True, check=True).stdout.splitlines()
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('JAVACMD', 'JAVA_HOME', 'JAVA_OPTS')}
    environment['PATH'] = f'{tmp_path / "jdk"}{os.pathsep}{environment["PATH"]}'
    from_path = subprocess.run([str(wrapper), 'A.kt'], env=environment, capture_output=True,
                               text=True, check=True).stdout.splitlines()
    assert from_path[:2] == ['-Xmx512M', '-Xms128M'] and from_path[-1] == 'A.kt'
    lib = output[output.index('-cp') + 1]
    assert lib.endswith('/kotlinc/bin/../lib/*')
    assert output[:5] == ['-Xmx1g', '--enable-native-access=ALL-UNNAMED',
                          '--sun-misc-unsafe-memory-access=allow', '-Dfoo=1', '-Xss4m']
    home = (layout.compiler_dir(DEV) / 'kotlinc').resolve()
    assert output[5] == f'-Dkotlin.home={home}'
    assert output[-3:] == ['-d', 'out', 'A.kt']
    assert output[output.index('-cp') + 2] == 'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler'


def test_without_a_wasm_entry_point_kotlinc_wasm_runs_the_js_compiler(web, layout):
    publish_dev(web, DEV, wasm_main=None)
    assert sync(layout, declared('master-wasm'), HOST, master=DEV) == 0
    wrapper = layout.compiler_dir(DEV) / 'kotlinc/bin/kotlinc-wasm'
    text = wrapper.read_text()
    assert 'org.jetbrains.kotlin.cli.js.K2JSCompiler -Xwasm -Xwasm-target=wasm-js "$' in text
    assert descriptor(layout, DEV)['backends']['wasm'] == {
        'launcher': 'kotlinc/bin/kotlinc-wasm', 'args': [],
        'stdlib': 'stdlib/kotlin-stdlib-wasm-js.klib'}


def test_dev_build_links_shared_jars_from_a_release_zip(web, layout):
    publish_release(web, '2.5.0-Beta2', abi='2.5.0', wasm_launcher=True, native=False)
    assert sync(layout, declared('2.5.0-Beta2-jvm'), HOST) == 0
    publish_dev(web, DEV)
    web.requests.clear()
    assert sync(layout, declared('master-jvm'), HOST, master=DEV) == 0
    lib = layout.compiler_dir(DEV) / 'kotlinc/lib'
    downloaded = {url.rpartition('/')[2] for url in web.downloads() if url.endswith('.jar')}
    assert 'kotlinx-coroutines-core-jvm-1.8.0.jar' not in downloaded
    assert 'kotlin-compiler-2.5.20-dev-3466.jar' in downloaded
    coroutines = lib / 'kotlinx-coroutines-core-jvm.jar'
    assert coroutines.is_symlink() and not os.path.isabs(os.readlink(coroutines))
    release = layout.compiler_dir('2.5.0-Beta2') / 'kotlinc/lib/kotlinx-coroutines-core-jvm.jar'
    assert coroutines.resolve() == release.resolve()
    assert not (lib / 'kotlin-compiler.jar').is_symlink()
    # Only the sidecars of the linked jar were requested, never the jar itself.
    url = artifact_url(MAVEN_CENTRAL, 'org.jetbrains.kotlinx', 'kotlinx-coroutines-core-jvm', '1.8.0')
    assert ('GET', url) not in web.requests and ('GET', url + '.sha1') in web.requests


def test_master_native_downloads_only_the_tarball(web, layout):
    publish_dev(web, DEV)
    assert sync(layout, declared('master-native'), HOST, master=DEV) == 0
    assert [url.rpartition('/')[2] for url in web.downloads()] == [
        f'kotlin-native-prebuilt-{DEV}-{HOST}.tar.gz']
    assert not any(url.endswith('.pom') for _, url in web.requests)
    assert sorted(p.name for p in layout.compiler_dir(DEV).iterdir()) == ['compiler.json', 'native']


def test_unset_master_falls_back_to_prerelease(web, layout):
    publish_release(web, '2.5.0-Beta2', abi='2.5.0')
    web.feed('v2.5.0-Beta2', 'v2.4.21')
    version_list = declared('prerelease-native', 'master-native',
                            fallbacks={'master': 'prerelease'})
    assert sync(layout, version_list, HOST, master=None) == 0
    assert [url.rpartition('/')[2] for url in web.downloads()] == [
        'releases.atom', 'kotlin-native-prebuilt-macos-aarch64-2.5.0-Beta2.tar.gz']
    assert layout.symlink_target('prerelease') == '2.5.0-Beta2'
    assert layout.symlink_target('master') == '2.5.0-Beta2'
    state = sync_state(layout)
    assert state['resolved'] == {'prerelease': '2.5.0-Beta2', 'master': '2.5.0-Beta2'}
    assert state['fallbacks_used'] == {'master': 'prerelease'}
    assert state['prerelease_source'] == 'releases.atom'
    rows = '\n'.join(list_lines(layout, version_list, HOST))
    assert 'fallback → prerelease, installed' in rows


def test_unset_master_without_fallback_is_empty(web, layout):
    layout.ensure()
    os.symlink('2.0.0', layout.root / 'master')
    assert sync(layout, declared('master'), HOST, master=None) == 0
    assert web.requests == []
    assert not (layout.root / 'master').is_symlink()
    assert sync_state(layout)['resolved']['master'] is None
    rows = [line for line in list_lines(layout, declared('master'), HOST)
            if line.startswith('master ')]
    assert 'empty' in rows[0]


def test_expired_dev_build_keeps_previous_master(web, layout, capsys):
    publish_dev(web, DEV)
    assert sync(layout, declared('master-native'), HOST, master=DEV) == 0
    assert sync(layout, declared('master-native'), HOST, master='2.5.20-dev-1') == 1
    assert 'dev builds expire' in capsys.readouterr().err
    assert layout.symlink_target('master') == DEV
    state = sync_state(layout)
    assert state['resolved']['master'] == DEV and state['errors']


def test_dev_version_pinned_in_the_list_uses_the_dev_repo(web, layout):
    publish_dev(web, DEV)
    assert sync(layout, declared(f'{DEV}-native'), HOST) == 0
    assert descriptor(layout, DEV)['origin'] == 'dev'
