"""Canned network and tiny archives for the Kotlin compiler manager tests."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import tarfile
import urllib.error
import zipfile

from src.tools.kotlin_compilers import download, layout as store_layout, releases
from src.tools.kotlin_compilers.descriptor import build_descriptor, write_descriptor
from src.tools.kotlin_compilers.dev import DEV_REPO
from src.tools.kotlin_compilers.layout import HOME_ENV, Layout
from src.tools.kotlin_compilers.releases import (
    GITHUB, MAVEN_CENTRAL, artifact_url, compiler_zip_name, github_asset_url, maven_url,
    native_asset_name,
)


class FakeWeb:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.tags: list[str] | None = []  # git ls-remote output; None = git fails

    def add(self, url: str, data: bytes, sidecar: str | None = None) -> None:
        self.files[url] = data
        if sidecar == 'sha256':
            self.files[url + '.sha256'] = hashlib.sha256(data).hexdigest().encode() + b'\n'
        elif sidecar == 'sha1':
            self.files[url + '.sha1'] = hashlib.sha1(data).hexdigest().encode()
        elif sidecar == 'maven':  # what Maven Central always serves
            self.files[url + '.sha1'] = hashlib.sha1(data).hexdigest().encode()
            self.files[url + '.md5'] = hashlib.md5(data).hexdigest().encode()

    def open(self, url: str, method: str = 'GET'):
        self.requests.append((method, url))
        if url not in self.files:
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        return io.BytesIO(b'' if method == 'HEAD' else self.files[url])

    def ls_remote(self) -> list[str]:
        self.requests.append(('GIT', GITHUB))
        if self.tags is None:
            raise OSError('git: network unreachable')
        return list(self.tags)

    def feed(self, *tags: str) -> None:
        entries = ''.join(
            f'<entry><link rel="alternate" type="text/html" '
            f'href="{GITHUB}/releases/tag/{tag}"/><title>{tag}</title></entry>' for tag in tags)
        self.files[f'{GITHUB}/releases.atom'] = (
            f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
        ).encode()

    def downloads(self) -> list[str]:
        """GET requests for payloads (checksum sidecars excluded)."""
        return [url for method, url in self.requests
                if method == 'GET' and not url.endswith(('.sha256', '.sha1', '.md5', '-sha256.txt'))]


def install_fake_web(monkeypatch) -> FakeWeb:
    web = FakeWeb()
    monkeypatch.setattr(download, 'open_url', web.open)
    monkeypatch.setattr(download, 'RETRY_DELAY_SECONDS', 0)
    monkeypatch.setattr(releases, 'ls_remote_tags', web.ls_remote)
    return web


def manifest_text(abi: str) -> str:
    return f'abi_version={abi}\nmetadata_version={abi}\nunique_name=stdlib\n'


def make_klib(abi: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('default/manifest', manifest_text(abi))
        archive.writestr('default/linkdata/module', 'x')
    return buffer.getvalue()


def _zip_entry(archive: zipfile.ZipFile, name: str, data: str, mode: int) -> None:
    info = zipfile.ZipInfo(name)
    info.external_attr = (0o100000 | mode) << 16
    archive.writestr(info, data)


CLASS_PATH = ('annotations-13.0.jar', 'kotlin-stdlib.jar', 'kotlin-reflect.jar',
              'kotlinx-coroutines-core-jvm.jar')
STDLIB_KLIB_NAMES = ('kotlin-stdlib-js.klib', 'kotlin-stdlib-wasm-js.klib',
                     'kotlin-stdlib-wasm-wasi.klib')
JUNK = ('license/LICENSE.txt', 'bin/kapt', 'bin/kotlinc.bat', 'bin/kotlin',
        'lib/kotlin-compiler-sources.jar', 'lib/allopen-compiler-plugin.jar',
        'lib/kotlin-scripting-compiler.jar', 'lib/kotlin-test.jar', 'lib/kotlin-runner.jar')
SHARED_JAR_CONTENT = {  # byte-identical across versions, as in the real zips
    'annotations-13.0.jar': 'annotations 13.0',
    'kotlinx-coroutines-core-jvm.jar': 'coroutines 1.8.0',
}
# Maven artifacts byte-identical to a zip's jar (checked live for 2.5.0-Beta2).
SHARED_MAVEN_JARS = {
    ('annotations', '13.0'): 'annotations-13.0.jar',
    ('kotlinx-coroutines-core-jvm', '1.8.0'): 'kotlinx-coroutines-core-jvm.jar',
}


def make_manifest_jar(class_path=()) -> bytes:
    """A jar whose manifest wraps ``Class-Path`` at 72 bytes like the real one."""
    line = 'Class-Path: ' + ' '.join(class_path)
    wrapped = [line[:72]] + [' ' + line[i:i + 71] for i in range(72, len(line), 71)]
    manifest = 'Manifest-Version: 1.0\r\n' + (''.join(f'{part}\r\n' for part in wrapped)
                                            if class_path else '') + 'Main-Class: X\r\n\r\n'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('META-INF/MANIFEST.MF', manifest)
    return buffer.getvalue()


def make_compiler_zip(wasm_launcher: bool = False, *, version: str = 'x',
                      klibs=('kotlin-stdlib-js.klib',), abi: str = '2.3.0',
                      class_path=CLASS_PATH, omit=()) -> bytes:
    """A release zip: stock launchers, Class-Path jars, the shipped klibs and junk."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        def entry(name, data, mode=0o644):
            if name not in omit:
                _zip_entry(archive, f'kotlinc/{name}', data, mode)
        entry('bin/kotlinc', '#!/bin/sh\n', 0o755)
        entry('bin/kotlinc-jvm', '#!/bin/sh\n', 0o755)
        entry('bin/kotlinc-js', '#!/bin/sh\n', 0o755)
        if wasm_launcher:
            entry('bin/kotlinc-wasm', '#!/bin/sh\n', 0o755)
        if 'lib/kotlin-compiler.jar' not in omit:
            archive.writestr('kotlinc/lib/kotlin-compiler.jar', make_manifest_jar(class_path))
        entry('lib/kotlin-preloader.jar', 'preloader')
        for name in class_path:
            entry(f'lib/{name}', SHARED_JAR_CONTENT.get(name, f'{name} {version}'))
        for name in klibs:
            if f'lib/{name}' not in omit:
                archive.writestr(f'kotlinc/lib/{name}', make_klib(abi))
        entry('build.txt', version)
        for name in JUNK:
            entry(name, 'junk')
    return buffer.getvalue()


def make_native_tar(version: str, host: str, abi: str,
                    properties: str = 'dependenciesUrl = x\nairplaneMode = false\n') -> bytes:
    top = f'kotlin-native-prebuilt-{host}-{version}'
    files = {
        'bin/run_konan': ('#!/bin/sh\n', 0o755),
        'bin/kotlinc-native': ('#!/bin/sh\n', 0o755),
        'konan/lib/kotlin-native-compiler-embeddable.jar': ('jar', 0o644),
        'konan/konan.properties': (properties, 0o644),
        'tools/env_blacklist': ('DYLD_X\n', 0o644),
        'klib/common/stdlib/default/manifest': (manifest_text(abi), 0o644),
        'klib/platform/macos_arm64/posix/default/manifest': ('x', 0o644),
        'konan/nativelib/libcallbacks.dylib': ('x', 0o755),
        'konan/nativelib/liborgjetbrainskotlinbackendkonanenvstubs.dylib': ('x', 0o755),
        'klib/common/stdlib/default/resources/x.txt': ('x', 0o644),
        'klib/common/stdlib/default/linkdata/module': ('x', 0o644),
        'bin/cinterop': ('#!/bin/sh\n', 0o755),
        'bin/klib': ('#!/bin/sh\n', 0o755),
        'tools/konan_lldb.py': ('x', 0o644),
        'konan/nativelib/libllvmstubs.dylib': ('x', 0o755),
        'konan/nativelib/libclangstubs.dylib': ('x', 0o755),
        'konan/platformDef/x.def': ('x', 0o644),
        'konan/targets/macos_arm64/x': ('x', 0o644),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, (text, mode) in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f'{top}/{name}')
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(f'{top}/bin/konanc')
        link.type = tarfile.SYMTYPE
        link.linkname = 'run_konan'
        archive.addfile(link)
    return buffer.getvalue()


def make_jar(*classes: str, title: str = '') -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('META-INF/MANIFEST.MF',
                         f'Manifest-Version: 1.0\nImplementation-Title: {title}\n')
        for name in classes:
            archive.writestr(name.replace('.', '/') + '.class', b'\xca\xfe\xba\xbe')
    return buffer.getvalue()


def make_pom(group: str, name: str, version: str, dependencies=(), extra: str = '') -> bytes:
    """``dependencies``: ``(group, name, version, scope, extra_xml)`` tuples."""
    body = ''.join(
        f'<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>'
        f'<version>{v}</version>{f"<scope>{s}</scope>" if s else ""}{x}</dependency>'
        for g, a, v, s, x in dependencies)
    return (f'<?xml version="1.0"?><project xmlns="http://maven.apache.org/POM/4.0.0">'
            f'<modelVersion>4.0.0</modelVersion><groupId>{group}</groupId>'
            f'<artifactId>{name}</artifactId><version>{version}</version>{extra}'
            f'<dependencies>{body}</dependencies></project>').encode()


def publish_artifact(web: FakeWeb, repository: str, group: str, name: str, version: str,
                     dependencies=(), classes=(), extra: str = '') -> None:
    web.add(artifact_url(repository, group, name, version, 'pom'),
            make_pom(group, name, version, dependencies, extra), 'maven')
    shared = SHARED_MAVEN_JARS.get((name, version))
    data = (SHARED_JAR_CONTENT[shared].encode() if shared
            else make_jar(*classes, title=f'{group}:{name}:{version}'))
    web.add(artifact_url(repository, group, name, version), data, 'maven')


EXCLUDE_ALL = '<exclusions><exclusion><groupId>*</groupId><artifactId>*</artifactId></exclusion></exclusions>'


def publish_dev(web: FakeWeb, version: str, host: str = 'macos-aarch64', *,
                wasm_main: str | None = 'org.jetbrains.kotlin.cli.js.KotlinWasmCompiler',
                abi: str | None = None) -> None:
    """A dev build shaped like today's ``kotlin-compiler`` POM graph."""
    dev, central = DEV_REPO, MAVEN_CENTRAL
    kotlin, kotlinx = 'org.jetbrains.kotlin', 'org.jetbrains.kotlinx'
    abi = abi or '.'.join(version.split('-')[0].split('.')[:2]) + '.0'
    classes = ['org.jetbrains.kotlin.cli.jvm.K2JVMCompiler',
               'org.jetbrains.kotlin.cli.js.K2JSCompiler'] + ([wasm_main] if wasm_main else [])
    publish_artifact(web, dev, kotlin, 'kotlin-compiler', version, [
        (kotlin, 'kotlin-stdlib-jdk8', version, 'compile', ''),
        (kotlin, 'kotlin-reflect', '1.6.10', 'compile', EXCLUDE_ALL),
        (kotlinx, 'kotlinx-coroutines-core-jvm', '1.8.0', 'compile', ''),
        (kotlin, 'kotlin-build-tools-api', version, 'compile', ''),
        ('junit', 'junit', '4.13', 'test', ''),
        ('org.example', 'optional-thing', '1.0', 'compile', '<optional>true</optional>'),
    ], classes)
    publish_artifact(web, dev, kotlin, 'kotlin-stdlib-jdk8', version, [
        (kotlin, 'kotlin-stdlib', version, 'compile', ''),
        (kotlin, 'kotlin-stdlib-jdk7', version, 'compile', '')])
    publish_artifact(web, dev, kotlin, 'kotlin-stdlib-jdk7', version, [
        (kotlin, 'kotlin-stdlib', version, 'compile', '')])
    publish_artifact(web, dev, kotlin, 'kotlin-stdlib', version, [
        ('org.jetbrains', 'annotations', '13.0', 'compile', '')])
    publish_artifact(web, dev, kotlin, 'kotlin-build-tools-api', version, extra=(
        '<dependencyManagement><dependencies><dependency><groupId>org.example</groupId>'
        '<artifactId>managed-only</artifactId><version>9</version></dependency>'
        '</dependencies></dependencyManagement>'))
    publish_artifact(web, central, kotlin, 'kotlin-reflect', '1.6.10', [
        ('org.example', 'reflect-only-dependency', '1.0', 'compile', '')])
    publish_artifact(web, central, kotlinx, 'kotlinx-coroutines-core-jvm', '1.8.0', [
        (kotlin, 'kotlin-stdlib', '1.8.20', 'compile', ''),
        ('org.jetbrains', 'annotations', '23.0.0', 'compile', '')])
    publish_artifact(web, central, 'org.jetbrains', 'annotations', '13.0')
    publish_artifact(web, central, 'org.jetbrains', 'annotations', '23.0.0')
    for artifact in ('kotlin-stdlib-js', 'kotlin-stdlib-wasm-js', 'kotlin-stdlib-wasm-wasi'):
        web.add(maven_url(dev, artifact, version, 'klib'), make_klib(abi), 'sha1')
    web.add(maven_url(dev, 'kotlin-native-prebuilt', version, 'tar.gz', host),
            make_native_tar(version, host, abi), 'sha1')


FAKE_MASTER_VERSION = '2.5.20-dev-1'


def fake_build(layout: Layout, version: str = FAKE_MASTER_VERSION, *,
               launcher_script: str = '#!/bin/sh\n', wasm_launcher: bool = True,
               abi: str = '2.5.0') -> Path:
    """An installed dev build with every backend; without ``wasm_launcher`` the
    descriptor drives wasm through ``kotlinc-js`` and its ``-Xwasm`` arguments."""
    directory = layout.compiler_dir(version)
    launchers = ['kotlinc/bin/kotlinc-jvm', 'kotlinc/bin/kotlinc-js', 'native/bin/kotlinc-native']
    if wasm_launcher:
        launchers.append('kotlinc/bin/kotlinc-wasm')
    for launcher in launchers:
        path = directory / launcher
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launcher_script)
        path.chmod(0o755)
    (directory / 'kotlinc/lib').mkdir()
    (directory / 'kotlinc/lib/kotlin-stdlib.jar').write_bytes(make_jar(title='kotlin-stdlib'))
    (directory / 'stdlib').mkdir()
    for name in STDLIB_KLIB_NAMES:
        (directory / 'stdlib' / name).write_bytes(make_klib(abi))
    manifest = directory / 'native/klib/common/stdlib/default/manifest'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(manifest_text(abi))
    write_descriptor(directory, build_descriptor(
        directory, version=version, origin='dev', host='macos-aarch64',
        backends={'jvm', 'js', 'wasm', 'native'}))
    return directory


def fake_master(root: str | os.PathLike[str], monkeypatch=None, **build) -> Path:
    """A store under ``root`` whose ``master`` links to a fresh :func:`fake_build`.

    Points ``KOTLIN_COMPILERS_HOME`` at the store (through ``monkeypatch`` when
    given, else in ``os.environ`` for child processes) and forgets resolved
    installs; returns the resolved build directory.
    """
    layout = Layout(root)
    layout.ensure()
    directory = fake_build(layout, **build)
    layout.set_symlink('master', directory.name)
    if monkeypatch is None:
        os.environ[HOME_ENV] = str(layout.root)
    else:
        monkeypatch.setenv(HOME_ENV, str(layout.root))
    store_layout._resolved_install.cache_clear()
    return directory.resolve()


def publish_release(web: FakeWeb, version: str, host: str = 'macos-aarch64', *,
                    abi: str | None = None, compiler: bool = True, native: bool = True,
                    stdlib: bool = True, native_sidecar: bool = True,
                    wasm_launcher: bool = False, zip_klibs=None) -> None:
    """``zip_klibs`` defaults to the real shape: all three with kotlinc-wasm, else JS only."""
    abi = abi or '.'.join(version.split('-')[0].split('.')[:2]) + '.0'
    if zip_klibs is None:
        zip_klibs = STDLIB_KLIB_NAMES if wasm_launcher else ('kotlin-stdlib-js.klib',)
    if compiler:
        url = github_asset_url(version, compiler_zip_name(version))
        web.add(url, make_compiler_zip(wasm_launcher, version=version, klibs=zip_klibs, abi=abi),
                'sha256')
    if native:
        url = github_asset_url(version, native_asset_name(version, host))
        web.add(url, make_native_tar(version, host, abi), 'sha256' if native_sidecar else None)
    if stdlib:
        for artifact in ('kotlin-stdlib-js', 'kotlin-stdlib-wasm-js', 'kotlin-stdlib-wasm-wasi'):
            web.add(maven_url(MAVEN_CENTRAL, artifact, version, 'klib'), make_klib(abi), 'maven')
