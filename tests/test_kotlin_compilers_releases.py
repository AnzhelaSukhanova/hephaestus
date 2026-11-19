from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

from tests.kotlin_compilers_fakes import (
    install_fake_web, make_compiler_zip, make_native_tar, publish_release,
)
from src.tools.kotlin_compilers import download
from src.tools.kotlin_compilers.descriptor import SHIM_MARKER
from src.tools.kotlin_compilers.layout import Layout, install_version
from src.tools.kotlin_compilers.manage import main
from src.tools.kotlin_compilers.releases import (
    GITHUB, MAVEN_CENTRAL, InstallError, PrereleaseError, compiler_zip_name, extract_compiler_zip,
    extract_native, github_asset_url, maven_url,
    native_asset_name, prerelease_probe, resolve_prerelease, set_airplane_mode,
)
from src.tools.kotlin_compilers.sync import list_lines, sync
from src.tools.kotlin_compilers.versions import parse_version_list


HOST = 'macos-aarch64'


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


def test_native_only_selector_installs_trimmed_native(web, layout):
    publish_release(web, '2.4.21')
    assert sync(layout, declared('2.4.21-native'), HOST) == 0
    directory = layout.compiler_dir('2.4.21')
    assert sorted(p.name for p in directory.iterdir()) == ['compiler.json', 'native']
    native = directory / 'native'
    assert sorted(str(p.relative_to(native)) for p in native.rglob('*') if not p.is_dir()) == [
        'bin/kotlinc-native', 'bin/run_konan',
        'klib/common/stdlib/default/linkdata/module', 'klib/common/stdlib/default/manifest',
        'konan/konan.properties', 'konan/lib/kotlin-native-compiler-embeddable.jar',
        'konan/nativelib/libcallbacks.dylib', 'tools/env_blacklist']
    assert not (native / 'bin/konanc').is_symlink()
    assert os.access(native / 'bin/run_konan', os.X_OK)
    properties = (native / 'konan/konan.properties').read_text()
    assert 'airplaneMode = true' in properties and 'airplaneMode = false' not in properties
    data = descriptor(layout, '2.4.21')
    assert list(data['backends']) == ['native']
    assert data['backends']['native'] == {
        'launcher': 'native/bin/kotlinc-native', 'args': [],
        'stdlib': 'native/klib/common/stdlib', 'first_stage_only': True}
    assert (data['origin'], data['host'], data['abi_version'], data['inliner_flag']) == (
        'release', HOST, '2.4.0', 'mode')
    assert not any('kotlin-compiler-' in url or 'kotlin-stdlib' in url for url in web.downloads())
    assert not any('api.github.com' in url for _, url in web.requests)
    assert list(layout.tmp.iterdir()) == []


def test_adding_a_backend_later_keeps_native_untouched(web, layout):
    publish_release(web, '2.4.21')
    assert sync(layout, declared('2.4.21-native'), HOST) == 0
    native = layout.compiler_dir('2.4.21') / 'native'
    jar = native / 'konan/lib/kotlin-native-compiler-embeddable.jar'
    before = (native.stat().st_ino, jar.stat().st_ino, jar.stat().st_mtime_ns)
    assert sync(layout, declared('2.4.21-native', '2.4.21-js'), HOST) == 0
    directory = layout.compiler_dir('2.4.21')
    assert (native.stat().st_ino, jar.stat().st_ino, jar.stat().st_mtime_ns) == before
    assert sorted(p.name for p in directory.iterdir()) == [
        'compiler.json', 'kotlinc', 'native', 'stdlib']
    # The zip ships the JS klib; stdlib/ holds only what it lacks.
    assert sorted(p.name for p in (directory / 'stdlib').iterdir()) == [
        'kotlin-stdlib-wasm-js.klib', 'kotlin-stdlib-wasm-wasi.klib']
    assert list(descriptor(layout, '2.4.21')['backends']) == ['js', 'native']
    assert descriptor(layout, '2.4.21')['backends']['js']['stdlib'] == (
        'kotlinc/lib/kotlin-stdlib-js.klib')
    assert os.access(directory / 'kotlinc/bin/kotlinc-js', os.X_OK)


def test_rerun_skips_installed_versions(web, layout):
    publish_release(web, '2.3.21')
    assert sync(layout, declared('2.3.21-native'), HOST) == 0
    web.requests.clear()
    assert sync(layout, declared('2.3.21-native'), HOST) == 0
    assert web.requests == []


def test_all_backends_of_a_release(web, layout):
    publish_release(web, '2.3.0')
    assert sync(layout, declared('2.3.0'), HOST) == 0
    data = descriptor(layout, '2.3.0')
    assert list(data['backends']) == ['jvm', 'js', 'wasm', 'native']
    assert data['backends']['wasm'] == {'launcher': 'kotlinc/bin/kotlinc-wasm', 'args': [],
                                        'shim': True,
                                        'stdlib': 'stdlib/kotlin-stdlib-wasm-js.klib'}


def test_checksum_mismatch_publishes_nothing(web, layout):
    publish_release(web, '2.4.21')
    url = github_asset_url('2.4.21', native_asset_name('2.4.21', HOST))
    web.files[url + '.sha256'] = b'0' * 64
    assert sync(layout, declared('2.4.21-native'), HOST) == 1
    assert not layout.compiler_dir('2.4.21').exists()
    assert list(layout.tmp.iterdir()) == []
    central = maven_url(MAVEN_CENTRAL, 'kotlin-native-prebuilt', '2.4.21', 'tar.gz', HOST)
    assert central not in web.downloads()


def test_release_sha256_listing_is_used_without_a_sidecar(web, layout, capsys):
    publish_release(web, '2.4.21', native_sidecar=False)
    url = github_asset_url('2.4.21', native_asset_name('2.4.21', HOST))
    listing = github_asset_url('2.4.21', 'maven-2.4.21-sha256.txt')
    web.files[listing] = (f'org/x/{native_asset_name("2.4.21", HOST)} '
                          f'{hashlib.sha256(web.files[url]).hexdigest()}\n').encode()
    assert sync(layout, declared('2.4.21-native'), HOST) == 0
    assert 'warning' not in capsys.readouterr().err
    web.files[listing] = (f'org/x/{native_asset_name("2.4.21", HOST)} {"1" * 64}\n').encode()
    other = Layout(layout.root.parent / 'other')
    assert sync(other, declared('2.4.21-native'), HOST) == 1


def test_missing_checksums_warn_and_install(web, layout, capsys):
    publish_release(web, '2.4.21', native_sidecar=False)
    assert sync(layout, declared('2.4.21-native'), HOST) == 0
    assert 'no checksum published' in capsys.readouterr().err
    assert (layout.compiler_dir('2.4.21') / 'native/bin/run_konan').exists()


def test_native_falls_back_to_maven_central(web, layout):
    publish_release(web, '2.4.21', native=False)
    central = maven_url(MAVEN_CENTRAL, 'kotlin-native-prebuilt', '2.4.21', 'tar.gz', HOST)
    web.add(central, make_native_tar('2.4.21', HOST, '2.4.0'), 'sha1')
    assert sync(layout, declared('2.4.21-native'), HOST) == 0
    assert central in web.downloads()
    assert list(descriptor(layout, '2.4.21')['backends']) == ['native']


def test_failed_download_leaves_nothing(web, layout):
    assert sync(layout, declared('2.9.0-native'), HOST) == 1
    assert not layout.compiler_dir('2.9.0').exists()
    assert list(layout.tmp.iterdir()) == []


def test_unsupported_host_installs_everything_but_native(web, layout, capsys):
    publish_release(web, '2.3.0')
    assert sync(layout, declared('2.3.0'), 'linux-aarch64') == 1
    assert 'no prebuilt' in capsys.readouterr().err
    assert list(descriptor(layout, '2.3.0')['backends']) == ['jvm', 'js', 'wasm']


def test_interrupted_staging_keeps_existing_install(web, layout):
    publish_release(web, '2.4.21')
    assert sync(layout, declared('2.4.21-native'), HOST) == 0
    before = (layout.compiler_dir('2.4.21') / 'compiler.json').read_text()

    def broken(part, target, downloads):
        target.mkdir()
        raise OSError('interrupted')

    with pytest.raises(OSError, match='interrupted'):
        install_version(layout, '2.4.21', origin='release', host=HOST,
                        backends={'jvm'}, stage=broken)
    assert (layout.compiler_dir('2.4.21') / 'compiler.json').read_text() == before
    assert not (layout.compiler_dir('2.4.21') / 'kotlinc').exists()
    assert list(layout.tmp.iterdir()) == []


@pytest.mark.parametrize('original', ['airplaneMode = false\n', 'airplaneMode=false',
                                      'dependenciesUrl = x\n', ''])
def test_airplane_mode_is_forced_on(tmp_path, original):
    properties = tmp_path / 'konan.properties'
    properties.write_text(original)
    set_airplane_mode(properties)
    lines = properties.read_text().splitlines()
    assert [line for line in lines if 'airplaneMode' in line] == ['airplaneMode = true']


def test_native_member_filter_drops_platform_parts(tmp_path):
    archive = tmp_path / 'native.tar.gz'
    archive.write_bytes(make_native_tar('2.3.21', HOST, '2.3.0', properties='x = 1\n'))
    extract_native(archive, tmp_path / 'native')
    native = tmp_path / 'native'
    assert not (native / 'klib/platform').exists()
    assert [p.name for p in (native / 'konan/nativelib').iterdir()] == ['libcallbacks.dylib']
    assert not (native / 'konan/targets').exists()
    assert not (native / 'konan/platformDef').exists()
    assert sorted(p.name for p in (native / 'bin').iterdir()) == ['kotlinc-native', 'run_konan']
    assert [p.name for p in (native / 'tools').iterdir()] == ['env_blacklist']
    assert not (native / 'klib/common/stdlib/default/resources').exists()
    assert 'airplaneMode = true' in (native / 'konan/konan.properties').read_text()


def test_list_reports_statuses(web, layout):
    publish_release(web, '2.3.21')
    publish_release(web, '2.2.0')
    assert sync(layout, declared('2.2.0-jvm'), HOST) == 0
    assert sync(layout, declared('2.3.21-native'), HOST) == 0
    lines = list_lines(layout, declared('2.3.21-native', '2.4.21-js', 'master',
                                        fallbacks={}), HOST)
    text = '\n'.join(lines)
    rows = {line.split()[0]: line for line in lines if line and line[0].isdigit() or
            line.startswith('master')}
    assert 'installed' in rows['2.3.21-native'] and '2.3.0' in rows['2.3.21-native']
    assert 'missing' in rows['2.4.21-js']
    assert 'empty' in rows['master']
    assert 'undeclared' in rows['2.2.0']
    assert 'host: macos-aarch64' in text


def test_manage_cli_sync_and_list(web, tmp_path, capsys):
    publish_release(web, '2.3.21')
    versions = tmp_path / 'versions.json'
    versions.write_text(json.dumps({'versions': ['2.3.21-native']}))
    home = tmp_path / 'home'
    assert main(['--versions', str(versions), '--host', HOST, '--home', str(home), 'sync']) == 0
    assert (home / '2.3.21' / 'native').is_dir()
    capsys.readouterr()
    assert main(['--versions', str(versions), '--host', HOST, '--home', str(home), 'list']) == 0
    assert '2.3.21-native  installed' in capsys.readouterr().out
    assert main(['--versions', str(versions), '--home', str(home), 'sync', '2.4.21-natvie']) == 2


@pytest.mark.parametrize('tags, expected', [
    (('v2.4.21-RC', 'v2.5.0-Beta1'), '2.5.0-Beta1'),
    (('v2.5.0-Beta1', 'v2.4.21-RC'), '2.5.0-Beta1'),
    (('v2.5.0-Beta2', 'v2.4.21'), '2.5.0-Beta2'),
    (('v2.5.0', 'v2.5.0-RC1'), '2.5.0'),
    (('build-2.6.0-dev-1', 'v2.6.0-M1', 'v2.6.0-dev-1', 'v2.5.0-RC'), '2.5.0-RC'),
])
def test_prerelease_is_highest_version_in_feed(web, tags, expected):
    web.feed(*tags)
    assert resolve_prerelease(prerelease_probe(True, HOST)) == (expected, 'releases.atom')
    assert not any(method in ('GIT', 'HEAD') for method, _ in web.requests)


def test_feed_without_release_tags_falls_back_to_ls_remote(web):
    web.feed('build-2.5.20-dev-3493', 'build-2.5.20-dev-3492')
    web.tags = ['v2.4.21', 'v2.5.0-Beta2', 'v2.5.0-Beta3', 'build-2.6.0-dev-1', 'v2.6.0-M1']
    publish_release(web, '2.5.0-Beta2', abi='2.5.0')
    assert resolve_prerelease(prerelease_probe(True, HOST)) == ('2.5.0-Beta2', 'git ls-remote')
    heads = [url for method, url in web.requests if method == 'HEAD']
    assert [url.rpartition('/')[2] for url in heads] == [
        native_asset_name('2.5.0-Beta3', HOST), native_asset_name('2.5.0-Beta2', HOST)]


def test_ls_remote_probe_uses_compiler_zip_for_non_native(web):
    web.tags = ['v2.5.0-Beta2']
    publish_release(web, '2.5.0-Beta2', native=False)
    assert resolve_prerelease(prerelease_probe(False, HOST)) == ('2.5.0-Beta2', 'git ls-remote')


def test_prerelease_unresolvable_fails_sync_and_keeps_symlinks(web, layout):
    publish_release(web, '2.4.21')
    web.feed('v2.4.21')
    assert sync(layout, declared('prerelease-native'), HOST) == 0
    assert layout.symlink_target('prerelease') == '2.4.21'
    del web.files[f'{GITHUB}/releases.atom']
    web.tags = None
    with pytest.raises(PrereleaseError, match='git ls-remote failed'):
        resolve_prerelease(prerelease_probe(True, HOST))
    assert sync(layout, declared('prerelease-native'), HOST) == 1
    assert layout.symlink_target('prerelease') == '2.4.21'
    state = json.loads(layout.sync_json.read_text())
    assert state['resolved']['prerelease'] == '2.4.21' and state['errors']


def test_prerelease_matching_a_pinned_version_is_merged(web, layout):
    publish_release(web, '2.4.21')
    web.feed('v2.4.21', 'v2.4.21-RC')
    assert sync(layout, declared('2.4.21-native', 'prerelease-js'), HOST) == 0
    natives = [url for url in web.downloads() if 'kotlin-native-prebuilt' in url]
    assert len(natives) == 1
    assert list(descriptor(layout, '2.4.21')['backends']) == ['js', 'native']
    assert layout.symlink_target('prerelease') == '2.4.21'
    assert layout.installed_versions() == ['2.4.21']
    assert not any('api.github.com' in url for _, url in web.requests)


def test_full_sync_of_the_committed_shape(web, layout):
    publish_release(web, '2.5.0-Beta2', abi='2.5.0', wasm_launcher=True)
    publish_release(web, '2.3.21')
    web.feed('v2.5.0-Beta2', 'v2.4.21')
    # KOTLIN_MASTER_VERSION unset: master falls back to prerelease, which is resolved for it.
    version_list = declared('master', '2.3.21-native', fallbacks={'master': 'prerelease'})
    assert sync(layout, version_list, HOST) == 0
    assert layout.installed_versions() == ['2.3.21', '2.5.0-Beta2']
    assert list(descriptor(layout, '2.5.0-Beta2')['backends']) == ['jvm', 'js', 'wasm', 'native']
    assert (layout.symlink_target('prerelease'), layout.symlink_target('master')) == (
        '2.5.0-Beta2', '2.5.0-Beta2')
    state = json.loads(layout.sync_json.read_text())
    assert state['fallbacks_used'] == {'master': 'prerelease'}
    assert state['prerelease_source'] == 'releases.atom'
    web.requests.clear()
    assert sync(layout, version_list, HOST) == 0
    assert [url for _, url in web.requests] == [f'{GITHUB}/releases.atom']


def test_zip_filter_keeps_only_the_minimal_kotlinc(tmp_path):
    archive = tmp_path / 'k.zip'
    archive.write_bytes(make_compiler_zip(version='2.3.0'))
    target = tmp_path / 'kotlinc'
    extract_compiler_zip(archive, target)
    assert sorted(str(p.relative_to(target)) for p in target.rglob('*') if not p.is_dir()) == [
        'bin/kotlinc', 'bin/kotlinc-js', 'bin/kotlinc-jvm', 'bin/kotlinc-wasm', 'build.txt',
        'lib/annotations-13.0.jar', 'lib/kotlin-compiler.jar', 'lib/kotlin-preloader.jar',
        'lib/kotlin-reflect.jar', 'lib/kotlin-stdlib-js.klib', 'lib/kotlin-stdlib.jar',
        'lib/kotlinx-coroutines-core-jvm.jar']
    assert all(os.access(p, os.X_OK) for p in (target / 'bin').iterdir())


def test_class_path_jar_missing_from_the_zip_is_an_install_error(tmp_path):
    archive = tmp_path / 'k.zip'
    archive.write_bytes(make_compiler_zip(omit=('lib/kotlin-reflect.jar',)))
    with pytest.raises(InstallError, match='kotlin-reflect.jar is in the Class-Path'):
        extract_compiler_zip(archive, tmp_path / 'kotlinc')


def test_missing_stock_launcher_is_an_install_error(tmp_path):
    archive = tmp_path / 'k.zip'
    archive.write_bytes(make_compiler_zip(omit=('lib/kotlin-preloader.jar',)))
    with pytest.raises(InstallError, match='kotlin-preloader.jar'):
        extract_compiler_zip(archive, tmp_path / 'kotlinc')


def test_wasm_shim_runs_kotlinc_js_even_through_a_symlink(tmp_path):
    archive = tmp_path / 'k.zip'
    archive.write_bytes(make_compiler_zip())
    target = tmp_path / 'kotlinc'
    extract_compiler_zip(archive, target)
    (target / 'bin/kotlinc-js').write_text('#!/bin/sh\necho "$0"\nfor a in "$@"; do echo "$a"; done\n')
    shim = target / 'bin/kotlinc-wasm'
    assert os.access(shim, os.X_OK) and SHIM_MARKER in shim.read_text()
    (tmp_path / 'elsewhere').mkdir()
    os.symlink(shim, tmp_path / 'elsewhere/kotlinc-wasm')
    for start in (shim, tmp_path / 'elsewhere/kotlinc-wasm'):
        output = subprocess.run([str(start), '-version', 'A.kt'], capture_output=True, text=True,
                                check=True).stdout.splitlines()
        assert Path(output[0]).resolve() == (target / 'bin/kotlinc-js').resolve()
        assert output[1:] == ['-Xwasm', '-Xwasm-target=wasm-js', '-version', 'A.kt']


def test_zip_with_all_klibs_needs_no_maven_request(web, layout):
    publish_release(web, '2.5.0-Beta2', abi='2.5.0', wasm_launcher=True, native=False)
    assert sync(layout, declared('2.5.0-Beta2-js', '2.5.0-Beta2-wasm'), HOST) == 0
    directory = layout.compiler_dir('2.5.0-Beta2')
    assert not (directory / 'stdlib').exists()
    assert not any(MAVEN_CENTRAL in url for _, url in web.requests)
    data = descriptor(layout, '2.5.0-Beta2')
    assert data['backends']['wasm'] == {'launcher': 'kotlinc/bin/kotlinc-wasm', 'args': [],
                                        'stdlib': 'kotlinc/lib/kotlin-stdlib-wasm-js.klib'}
    assert data['backends']['js']['stdlib'] == 'kotlinc/lib/kotlin-stdlib-js.klib'
    # Adding a backend later stages stdlib/ again and still finds nothing to fetch.
    publish_release(web, '2.5.0-Beta2', abi='2.5.0', compiler=False)
    assert sync(layout, declared('2.5.0-Beta2-js', '2.5.0-Beta2-native'), HOST) == 0
    assert not (directory / 'stdlib').exists()
    assert not any('kotlin-stdlib' in url for url in web.downloads())


def test_zip_with_only_the_js_klib_fetches_just_the_wasm_klibs(web, layout):
    publish_release(web, '2.3.0', native=False)
    assert sync(layout, declared('2.3.0-wasm'), HOST) == 0
    klibs = [url.rpartition('/')[2] for url in web.downloads() if url.endswith('.klib')]
    assert sorted(klibs) == ['kotlin-stdlib-wasm-js-2.3.0.klib', 'kotlin-stdlib-wasm-wasi-2.3.0.klib']
    directory = layout.compiler_dir('2.3.0')
    assert all(not p.is_symlink() for p in (directory / 'stdlib').iterdir())
    data = descriptor(layout, '2.3.0')
    assert data['backends']['wasm']['stdlib'] == 'stdlib/kotlin-stdlib-wasm-js.klib'
    assert data['backends']['wasm']['shim'] is True


def test_shared_jars_are_stored_once_across_versions(web, layout):
    publish_release(web, '2.3.0', native=False)
    publish_release(web, '2.4.21', native=False)
    assert sync(layout, declared('2.3.0-jvm', '2.4.21-jvm'), HOST) == 0
    libs = [layout.compiler_dir(v) / 'kotlinc/lib' for v in ('2.3.0', '2.4.21')]
    for name in ('annotations-13.0.jar', 'kotlinx-coroutines-core-jvm.jar', 'kotlin-reflect.jar'):
        links = [lib / name for lib in libs]
        assert all(link.is_symlink() and not os.path.isabs(os.readlink(link)) for link in links)
        assert all(link.resolve().is_relative_to(layout.store.resolve()) for link in links)
    same = {(lib / 'annotations-13.0.jar').resolve() for lib in libs}
    different = {(lib / 'kotlin-reflect.jar').resolve() for lib in libs}
    assert len(same) == 1 and len(different) == 2
    for name in ('kotlin-compiler.jar', 'kotlin-stdlib.jar', 'kotlin-preloader.jar'):
        assert all(not (lib / name).is_symlink() and (lib / name).is_file() for lib in libs)
    index = json.loads((layout.store / 'index.json').read_text())
    entry = next(e for e in index['entries'].values() if e['name'] == 'annotations-13.0.jar')
    for algorithm in ('md5', 'sha1', 'sha256'):
        assert index['by_digest'][f'{algorithm}:{entry[algorithm]}'] == entry['sha256']
    assert layout.installed_versions() == ['2.3.0', '2.4.21']


def test_interrupted_install_leaves_no_link_in_published_dirs(web, layout):
    publish_release(web, '2.3.0', native=False)
    del web.files[maven_url(MAVEN_CENTRAL, 'kotlin-stdlib-wasm-wasi', '2.3.0', 'klib')]
    assert sync(layout, declared('2.3.0-wasm'), HOST) == 1
    assert not layout.compiler_dir('2.3.0').exists()
    assert list(layout.tmp.iterdir()) == []
    assert all(not p.is_symlink() for p in layout.root.rglob('*') if 'store' not in p.parts)


def test_open_url_sends_no_credentials(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen['headers'] = dict(request.header_items())
        raise download.urllib.error.HTTPError(request.full_url, 404, 'nf', {}, None)

    monkeypatch.setattr(download.urllib.request, 'urlopen', fake_urlopen)
    assert download.read_text('https://github.com/x') is None
    assert set(seen['headers']) == {'User-agent'}


def test_corrupt_zip_fails_only_its_version(web, layout):
    publish_release(web, '2.3.0', native=False)
    publish_release(web, '2.4.21', native=False)
    web.add(github_asset_url('2.3.0', compiler_zip_name('2.3.0')), b'PK\x03\x04 not a zip', 'sha256')
    assert sync(layout, declared('2.3.0-jvm', '2.4.21-jvm'), HOST) == 1
    assert not layout.compiler_dir('2.3.0').exists()
    assert list(descriptor(layout, '2.4.21')['backends']) == ['jvm']
    errors = json.loads(layout.sync_json.read_text())['errors']
    assert len(errors) == 1 and errors[0].startswith('2.3.0: kotlin-compiler-2.3.0.zip: ')


def with_extra_members(tar: bytes, *members: tarfile.TarInfo) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(tar), mode='r:gz') as source, \
            tarfile.open(fileobj=buffer, mode='w:gz') as output:
        for member in source:
            output.addfile(member, source.extractfile(member) if member.isfile() else None)
        for member in members:
            output.addfile(member)
    return buffer.getvalue()


def test_escaping_link_in_native_tarball_is_an_install_error(tmp_path):
    link = tarfile.TarInfo(f'kotlin-native-prebuilt-{HOST}-2.4.21/konan/lib/evil.jar')
    link.type, link.linkname = tarfile.SYMTYPE, '../../../../outside.jar'
    archive = tmp_path / 'n.tar.gz'
    archive.write_bytes(with_extra_members(make_native_tar('2.4.21', HOST, '2.4.0'), link))
    target = tmp_path / 'out' / 'native'
    with pytest.raises(InstallError, match='n.tar.gz: '):
        extract_native(archive, target)
    assert not (target / 'konan/lib/evil.jar').is_symlink()


def test_truncated_native_tarball_is_an_install_error(tmp_path):
    tar = make_native_tar('2.4.21', HOST, '2.4.0')
    archive = tmp_path / 'n.tar.gz'
    archive.write_bytes(tar[:len(tar) // 10])
    with pytest.raises(InstallError):
        extract_native(archive, tmp_path / 'native')


@pytest.mark.parametrize('damage', ['corrupt', 'missing'])
def test_lost_descriptor_is_rebuilt_from_the_parts_on_disk(web, layout, capsys, damage):
    publish_release(web, '2.3.0')
    assert sync(layout, declared('2.3.0-jvm', '2.3.0-native'), HOST) == 0
    path = layout.compiler_dir('2.3.0') / 'compiler.json'
    if damage == 'corrupt':
        path.write_text('{ not json')
    else:
        path.unlink()
    web.requests.clear()
    capsys.readouterr()
    assert sync(layout, declared('2.3.0-jvm'), HOST) == 0
    # The zip ships the JS klib, so js is found too; wasm lacks its stdlib/ klib.
    assert list(descriptor(layout, '2.3.0')['backends']) == ['jvm', 'js', 'native']
    assert web.downloads() == []
    assert 'missing or corrupt; rebuilding it from the parts present' in capsys.readouterr().err


def test_real_directory_at_a_slot_name_is_reported_not_raised(web, layout):
    publish_release(web, '2.5.0-Beta2', abi='2.5.0')
    web.feed('v2.5.0-Beta2')
    layout.ensure()
    (layout.root / 'prerelease').mkdir()
    assert sync(layout, declared('prerelease-native'), HOST) == 1
    state = json.loads(layout.sync_json.read_text())
    assert state['errors'] == [f'symlinks: {layout.root / "prerelease"} exists and is not a symlink']
    assert state['resolved']['prerelease'] is None
