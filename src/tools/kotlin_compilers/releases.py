"""Published builds: GitHub release assets plus Maven Central stdlib klibs.

The compiler zip is filtered down to the stock launchers Hephaestus starts
(plus ``kotlinc`` and ``kotlin-preloader.jar`` underneath), the jars named in
``kotlin-compiler.jar``'s ``Class-Path``, the stdlib klibs it ships and
``build.txt``. Versions without ``kotlinc-wasm`` get a generated shim.
Maven stdlib klibs are fetched only when the zip lacks them.

Native is installed for the 1st stage (klib production) only: the prebuilt
tarball is streamed through a member filter that keeps the two launchers, the
compiler jar, ``konan.properties``, the JVM callbacks library, ``env_blacklist``
and the common stdlib, and the staged ``konan.properties`` gets
``airplaneMode = true`` so any attempt to fetch LLVM or a sysroot fails
instead of downloading.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import zipfile
import zlib

from . import download
from .descriptor import SHIM_MARKER, STDLIB_KLIBS, WASM_VIA_JS_ARGS
from .download import Checksum, ChecksumError, DownloadError, log
from .host import require_native_host
from .layout import Layout, Stager, install_version
from .store import Store, is_shareable
from .versions import version_key


GITHUB = 'https://github.com/JetBrains/kotlin'
MAVEN_CENTRAL = 'https://repo1.maven.org/maven2'
KOTLIN_GROUP = 'org/jetbrains/kotlin'

# The kept part of a release zip's kotlinc/ (besides the Class-Path jars).
ZIP_REQUIRED = (
    'bin/kotlinc',
    'bin/kotlinc-jvm',
    'bin/kotlinc-js',
    'lib/kotlin-compiler.jar',
    'lib/kotlin-preloader.jar',
    'lib/kotlin-stdlib.jar',
)
ZIP_OPTIONAL = ('bin/kotlinc-wasm', 'build.txt') + tuple(f'lib/{name}'
                                                         for name in STDLIB_KLIBS.values())
COMPILER_JAR = 'lib/kotlin-compiler.jar'
WASM_SHIM = f'''#!/usr/bin/env bash
{SHIM_MARKER}: this release ships no kotlinc-wasm.
source="${{BASH_SOURCE[0]}}"
while [ -h "$source" ]; do
    linked="$(readlink "$source")"
    case "$linked" in /*) source="$linked" ;; *) source="$(dirname "$source")/$linked" ;; esac
done
exec "$(cd -P "$(dirname "$source")" && pwd)/kotlinc-js" {" ".join(WASM_VIA_JS_ARGS)} "$@"
'''

# The 1st-stage subset of a Native distribution (paths below its top directory).
# Directory prefixes end with '/'; anything else must match exactly. Only the
# launchers Hephaestus runs are kept (konanc, cinterop, klib, konan-lldb and
# generate-platform are not). The compiler loads the JVM callbacks library from
# konan/nativelib even for klibs (2.3.x fails without it); the env/files stubs
# and the LLVM/Clang stubs there (~75 MB) are not needed for klibs.
NATIVE_MEMBERS = (
    'bin/run_konan',
    'bin/kotlinc-native',
    'konan/lib/',
    'konan/konan.properties',
    'tools/env_blacklist',
    'klib/common/stdlib/',
)
NATIVE_EXCLUDED = (
    'klib/common/stdlib/default/resources/',
)
NATIVE_LIBRARIES = 'konan/nativelib/'
NATIVE_CALLBACKS = re.compile(r'lib[A-Za-z0-9_]*callbacks\.(?:dylib|so)')
NATIVE_REQUIRED = (
    'bin/run_konan',
    'bin/kotlinc-native',
    'konan/lib/kotlin-native-compiler-embeddable.jar',
    'konan/konan.properties',
    'tools/env_blacklist',
    'klib/common/stdlib/default/manifest',
)
AIRPLANE_MODE = re.compile(r'^[ \t]*airplaneMode[ \t]*[=:].*$', re.MULTILINE)


class InstallError(RuntimeError):
    pass


class PrereleaseError(RuntimeError):
    pass


def github_asset_url(version: str, asset: str) -> str:
    return f'{GITHUB}/releases/download/v{version}/{asset}'


def compiler_zip_name(version: str) -> str:
    return f'kotlin-compiler-{version}.zip'


def native_asset_name(version: str, host: str) -> str:
    return f'kotlin-native-prebuilt-{host}-{version}.tar.gz'


def artifact_url(repository: str, group: str, artifact: str, version: str,
                 extension: str = 'jar', classifier: str | None = None) -> str:
    suffix = f'-{classifier}' if classifier else ''
    return (f'{repository}/{group.replace(".", "/")}/{artifact}/{version}/'
            f'{artifact}-{version}{suffix}.{extension}')


def maven_url(repository: str, artifact: str, version: str, extension: str = 'jar',
              classifier: str | None = None) -> str:
    return artifact_url(repository, KOTLIN_GROUP, artifact, version, extension, classifier)


def feed_tags(feed: str) -> list[str]:
    """Release tags (``v<maj>.<min>.<patch>[-Beta<n>|-RC<n>]``) linked from an Atom feed."""
    tags = re.findall(r'/releases/tag/(v[0-9][^"<&\s]*)', feed)
    return sorted({tag for tag in tags if version_key(tag)}, key=version_key)


def ls_remote_tags() -> list[str]:
    output = subprocess.run(['git', 'ls-remote', '--tags', '--refs', GITHUB, 'v*'],
                            capture_output=True, text=True, check=True, timeout=120).stdout
    return [line.rpartition('/')[2] for line in output.splitlines() if '/' in line]


def resolve_prerelease(probe: Callable[[str], str]) -> tuple[str, str]:
    """The highest published version and its source; ``probe(v)`` names an asset to HEAD.

    The feed lists only published releases, so its assets exist. A tag from
    ``git ls-remote`` may predate its upload, so it is accepted only once its
    asset answers.
    """
    try:
        feed = download.read_text(f'{GITHUB}/releases.atom')
        tags = feed_tags(feed or '')
        if tags:
            return tags[-1][1:], 'releases.atom'
        log('releases.atom has no release tags; falling back to git ls-remote')
    except DownloadError as exc:
        log(f'releases.atom failed ({exc}); falling back to git ls-remote')
    try:
        tags = ls_remote_tags()
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrereleaseError(f'cannot resolve prerelease: releases.atom unusable and '
                              f'git ls-remote failed: {exc}') from exc
    for tag in sorted({t for t in tags if version_key(t)}, key=version_key, reverse=True):
        url = probe(tag[1:])
        if download.exists(url):
            return tag[1:], 'git ls-remote'
        log(f'  {tag}: {url.rpartition("/")[2]} not uploaded yet; trying the next tag')
    raise PrereleaseError('cannot resolve prerelease: no release tag with uploaded assets')


def prerelease_probe(native: bool, host: str) -> Callable[[str], str]:
    if native:
        return lambda version: github_asset_url(version, native_asset_name(version, host))
    return lambda version: github_asset_url(version, compiler_zip_name(version))


def _safe_relative(name: str) -> PurePosixPath | None:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        return None
    return path


def manifest_attributes(text: str) -> dict[str, str]:
    """Main attributes of a jar manifest, with 72-byte continuation lines joined."""
    lines: list[str] = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if line.startswith(' ') and lines:
            lines[-1] += line[1:]
        elif not line:
            if lines:
                break
        else:
            lines.append(line)
    attributes = {}
    for line in lines:
        key, separator, value = line.partition(':')
        if separator:
            attributes[key.strip()] = value.strip()
    return attributes


def class_path(jar: Path) -> list[str]:
    """``lib/<name>`` for every ``Class-Path`` entry of ``jar`` (which lives in ``lib/``)."""
    try:
        with zipfile.ZipFile(jar) as archive:
            text = archive.read('META-INF/MANIFEST.MF').decode('utf-8', 'replace')
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise InstallError(f'cannot read the manifest of {jar.name}: {exc}') from exc
    entries = []
    for entry in manifest_attributes(text).get('Class-Path', '').split():
        relative = _safe_relative(f'lib/{entry}')
        if relative is None or len(relative.parts) != 2:
            raise InstallError(f'{jar.name}: unsupported Class-Path entry {entry!r}')
        entries.append(str(relative))
    return entries


def _extract_member(bundle: zipfile.ZipFile, info: zipfile.ZipInfo, destination: Path,
                    executable: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with bundle.open(info) as source, open(destination, 'wb') as output:
        while chunk := source.read(download.CHUNK):
            output.write(chunk)
    mode = (info.external_attr >> 16) & 0o777
    if executable:
        mode |= 0o755
    destination.chmod(mode or 0o644)


def write_wasm_shim(path: Path) -> None:
    path.write_text(WASM_SHIM)
    path.chmod(0o755)


def extract_compiler_zip(archive: Path, target: Path) -> None:
    """Unpack the minimal ``kotlinc/`` of a release zip into ``target``.

    Keeps :data:`ZIP_REQUIRED`, whatever of :data:`ZIP_OPTIONAL` the zip ships,
    and the jars in ``kotlin-compiler.jar``'s ``Class-Path``; writes the
    ``kotlinc-wasm`` shim when the zip has no ``kotlinc-wasm``.
    """
    try:
        _extract_compiler_zip(archive, target)
    except (zipfile.BadZipFile, zlib.error) as exc:
        raise InstallError(f'{archive.name}: {exc}') from exc
    if not (target / 'bin/kotlinc-wasm').exists():
        write_wasm_shim(target / 'bin/kotlinc-wasm')


def _extract_compiler_zip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        members = {}
        for info in bundle.infolist():
            relative = _safe_relative(info.filename)
            if (relative is None or len(relative.parts) < 2 or relative.parts[0] != 'kotlinc'
                    or info.is_dir()):
                continue
            members['/'.join(relative.parts[1:])] = info

        def extract(inner: str) -> None:
            _extract_member(bundle, members[inner], target / inner, inner.startswith('bin/'))

        missing = [inner for inner in ZIP_REQUIRED if inner not in members]
        if missing:
            raise InstallError(f'{archive.name} has no ' + ', '.join(f'kotlinc/{m}' for m in missing))
        for inner in ZIP_REQUIRED + ZIP_OPTIONAL:
            if inner in members:
                extract(inner)
        for inner in class_path(target / COMPILER_JAR):
            if inner not in members:
                raise InstallError(f'{archive.name}: kotlinc/{inner} is in the Class-Path of '
                                   f'kotlin-compiler.jar but not in the zip')
            if not (target / inner).exists():
                extract(inner)


def _native_member(name: str) -> str | None:
    """The path below the distribution's top directory if it belongs to the 1st stage."""
    relative = _safe_relative(name)
    if relative is None or len(relative.parts) < 2:
        return None
    inner = '/'.join(relative.parts[1:])
    if inner.startswith(NATIVE_EXCLUDED) or inner + '/' in NATIVE_EXCLUDED:
        return None
    if inner.startswith(NATIVE_LIBRARIES):
        name = inner[len(NATIVE_LIBRARIES):]
        return inner if NATIVE_CALLBACKS.fullmatch(name) else None
    for member in NATIVE_MEMBERS:
        if member.endswith('/'):
            if inner.startswith(member) or inner == member.rstrip('/'):
                return inner
        elif inner == member:
            return inner
    return None


def set_airplane_mode(properties: Path) -> None:
    text = properties.read_text()
    text, count = AIRPLANE_MODE.subn('airplaneMode = true', text)
    if count == 0:
        text = text + ('' if text.endswith('\n') or not text else '\n') + 'airplaneMode = true\n'
    properties.write_text(text)


def extract_native(archive: Path, target: Path) -> None:
    """Stream the prebuilt tarball, keeping only :data:`NATIVE_MEMBERS`, then guard LLVM."""
    target.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, 'r|gz') as bundle:
            for member in bundle:
                inner = _native_member(member.name)
                if inner is None:
                    continue
                member.name = inner
                if member.islnk():
                    linked = _native_member(member.linkname)
                    if linked is None:
                        continue
                    member.linkname = linked
                bundle.extract(member, target, filter='data')
    except tarfile.TarError as exc:
        # Includes the 'data' filter's refusals (links or paths escaping the target).
        raise InstallError(f'{archive.name}: {exc}') from exc
    missing = [path for path in NATIVE_REQUIRED if not (target / path).exists()]
    if missing:
        raise InstallError(f'{archive.name} lacks 1st-stage files: {", ".join(missing)}')
    for launcher in (target / 'bin').iterdir():
        if launcher.is_file():
            launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    set_airplane_mode(target / 'konan/konan.properties')


def adopt_shared_jars(lib: Path, final_lib: Path, store: Store) -> None:
    """Move the shareable jars of a staged ``lib/`` into the store, leaving symlinks."""
    for jar in sorted(lib.glob('*.jar')):
        if is_shareable(jar.name) and not jar.is_symlink():
            store.adopt(jar, final_lib / jar.name)


def stage_compiler_zip(version: str, target: Path, downloads: Path, final: Path,
                       store: Store) -> None:
    url = github_asset_url(version, compiler_zip_name(version))
    archive = download.fetch(url, downloads / compiler_zip_name(version),
                             download.github_checksum(url, version))
    target.mkdir()
    extract_compiler_zip(archive, target)
    archive.unlink()
    adopt_shared_jars(target / 'lib', final / 'lib', store)


def stage_stdlib_klibs(repository: str, version: str, target: Path, final: Path,
                       store: Store, shipped: list[Path]) -> None:
    """Fetch into ``target`` only the stdlib klibs none of the ``shipped`` dirs has.

    ``target`` is not created when nothing is missing.
    """
    for artifact, name in STDLIB_KLIBS.items():
        if any((directory / name).is_file() for directory in shipped):
            continue
        url = maven_url(repository, artifact, version, 'klib')
        target.mkdir(exist_ok=True)
        store.fetch_or_link(url, target / name, final / name)


def kotlinc_libs(target: Path, final: Path) -> list[Path]:
    """Where a freshly staged or an installed ``kotlinc/lib`` sits next to a ``stdlib`` part."""
    return [target.parent / 'kotlinc/lib', final.parent / 'kotlinc/lib']


ChecksumLookup = Callable[[str], Checksum | None]


def stage_native_archive(candidates: list[tuple[str, ChecksumLookup]], target: Path,
                         downloads: Path) -> None:
    """Try ``(url, checksum lookup)`` sources in order; a checksum mismatch is final."""
    archive = downloads / 'kotlin-native-prebuilt.tar.gz'
    errors: list[DownloadError] = []
    for url, lookup in candidates:
        try:
            download.fetch(url, archive, lookup(url))
            break
        except ChecksumError:
            raise
        except DownloadError as exc:
            errors.append(exc)
            log(f'  {exc}; trying the next source')
    else:
        message = '; '.join(str(error) for error in errors)
        if all(isinstance(error, download.NotFound) for error in errors):
            raise download.NotFound(message)
        raise DownloadError(message)
    log(f'  extracting the 1st-stage subset of {url.rpartition("/")[2]}')
    extract_native(archive, target)
    archive.unlink()


def release_native_candidates(version: str, host: str) -> list[tuple[str, ChecksumLookup]]:
    github = github_asset_url(version, native_asset_name(version, host))
    central = maven_url(MAVEN_CENTRAL, 'kotlin-native-prebuilt', version, 'tar.gz', host)
    return [(github, lambda url: download.github_checksum(url, version)),
            (central, download.maven_checksum)]


def release_stager(version: str, host: str, directory: Path, store: Store) -> Stager:
    def stage(part: str, target: Path, downloads: Path) -> None:
        final = directory / part
        if part == 'kotlinc':
            stage_compiler_zip(version, target, downloads, final, store)
        elif part == 'stdlib':
            stage_stdlib_klibs(MAVEN_CENTRAL, version, target, final, store,
                               kotlinc_libs(target, final))
        elif part == 'native':
            stage_native_archive(release_native_candidates(version, require_native_host(host)),
                                 target, downloads)
        else:
            raise InstallError(f'unknown part {part!r}')
    return stage


def install_release(layout: Layout, version: str, backends, host: str) -> tuple[dict, list[str]]:
    if 'native' in backends:
        require_native_host(host)
    stage = release_stager(version, host, layout.compiler_dir(version), Store(layout.store))
    return install_version(layout, version, origin='release', host=host, backends=backends,
                           stage=stage)
