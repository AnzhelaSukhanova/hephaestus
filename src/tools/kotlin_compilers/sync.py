"""The triggered ``sync`` run and the offline ``list`` report."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from .descriptor import DescriptorError, read_descriptor
from .dev import DevError, install_dev
from .download import DownloadError, log
from .host import NATIVE_HOSTS, HostError
from .layout import Layout
from .releases import (
    InstallError, PrereleaseError, install_release, prerelease_probe, resolve_prerelease,
)
from .versions import (
    BACKENDS, SLOTS, Selector, VersionList, fallbacks_used, is_dev_version, merge,
    parse_selector, resolve_slots, selector_version,
)


INSTALL_ERRORS = (DownloadError, InstallError, HostError, DescriptorError, DevError, OSError)


def select(version_list: VersionList, ids: Sequence[str]) -> list[Selector]:
    """The declared selectors, or the given ids parsed as selectors."""
    if not ids:
        return list(version_list.selectors)
    return [parse_selector(text) for text in dict.fromkeys(ids)]


def install_all(layout: Layout, wanted: Mapping[str, frozenset[str]], host: str,
                errors: list[str]) -> None:
    for version, backends in wanted.items():
        if 'native' in backends and host not in NATIVE_HOSTS:
            errors.append(f'{version}: native: Kotlin/Native has no prebuilt for host {host!r}')
            backends = backends - {'native'}
            if not backends:
                continue
        dev = is_dev_version(version)
        label = f'{version} [{",".join(b for b in BACKENDS if b in backends)}]'
        log(f'{label}{" (dev repo)" if dev else ""}:')
        try:
            install = install_dev if dev else install_release
            descriptor, parts = install(layout, version, backends, host)
        except INSTALL_ERRORS as exc:
            errors.append(f'{version}: {exc}')
            log(f'  failed: {exc}')
            continue
        if parts:
            log(f'  installed {", ".join(parts)} -> {layout.compiler_dir(version)} '
                f'(backends: {", ".join(descriptor["backends"])})')
        else:
            log('  already installed')


def sync(layout: Layout, version_list: VersionList, host: str,
         ids: Sequence[str] = (), master: str | None = None) -> int:
    """Resolve the slots once, install what is missing, then point the symlinks.

    ``master`` is the validated ``KOTLIN_MASTER_VERSION`` (``None`` when unset).
    ``master`` (offline) is resolved on every full run; ``prerelease`` (network)
    whenever a selector needs it, directly or through the ``master`` fallback.
    On any failure the symlinks and the recorded resolution stay as they were.
    """
    selectors = select(version_list, ids)
    kinds = {selector.kind for selector in selectors}
    fallback = version_list.fallbacks.get('master')
    want_master = not ids or 'master' in kinds
    master_falls_back = want_master and master is None and fallback is not None
    want_prerelease = 'prerelease' in kinds or (
        'master' in kinds and master_falls_back and fallback == 'prerelease')
    errors: list[str] = []
    resolved_now: dict[str, str | None] = {}
    source = None
    with layout.locked():
        layout.clean_tmp()
        prerelease = None
        if want_prerelease:
            native = host in NATIVE_HOSTS and any(
                'native' in s.backends for s in selectors
                if s.kind == 'prerelease' or (s.kind == 'master' and master_falls_back))
            try:
                prerelease, source = resolve_prerelease(prerelease_probe(native, host))
                log(f'prerelease: {prerelease} (from {source})')
                resolved_now['prerelease'] = prerelease
            except (PrereleaseError, DownloadError) as exc:
                errors.append(str(exc))
        slots = resolve_slots(prerelease, master, version_list.fallbacks)
        if want_master and not (master_falls_back and fallback == 'prerelease'
                                and prerelease is None):
            resolved_now['master'] = slots['master']
            if master is not None:
                log(f'master: {master} (from KOTLIN_MASTER_VERSION)')
            elif slots['master'] is not None:
                log(f'master: KOTLIN_MASTER_VERSION unset; fallback {fallback} -> {slots["master"]}')
            else:
                log('master: KOTLIN_MASTER_VERSION unset and no fallback; slot is empty')
        skipped = [s.text for s in selectors
                   if s.kind != 'release' and resolved_now.get(s.kind) is None]
        if skipped:
            log(f'skipping unresolved selectors: {", ".join(skipped)}')
        install_all(layout, merge(selectors, {k: resolved_now.get(k) for k in SLOTS}),
                    host, errors)
        if not errors:
            try:
                update_symlinks(layout, resolved_now)
            except OSError as exc:
                errors.append(f'symlinks: {exc}')
        write_sync_state(layout, host, errors, resolved_now, source,
                         fallbacks_used(master, version_list.fallbacks)
                         if 'master' in resolved_now else None)
    for error in errors:
        log(f'error: {error}')
    return 1 if errors else 0


def update_symlinks(layout: Layout, resolved: Mapping[str, str | None]) -> None:
    for slot, version in resolved.items():
        if version is None:
            layout.set_symlink(slot, None)
        elif layout.descriptor(version) is not None:
            layout.set_symlink(slot, version)
            log(f'{slot} -> {version}')
        else:
            log(f'{slot}: {version} is not installed (no selector asks for it); '
                f'symlink left as it was')


def write_sync_state(layout: Layout, host: str, errors: list[str],
                     resolved_now: Mapping[str, str | None], source: str | None,
                     used: Mapping[str, str] | None) -> None:
    """Record this run; the resolution is only updated by a run without errors."""
    previous = read_sync_state(layout)
    resolved = {slot: (previous.get('resolved') or {}).get(slot) for slot in SLOTS}
    data = {'synced_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'host': host, 'resolved': resolved,
            'fallbacks_used': previous.get('fallbacks_used') or {},
            'prerelease_source': previous.get('prerelease_source'),
            'errors': errors}
    if not errors:
        resolved.update(resolved_now)
        if used is not None:
            data['fallbacks_used'] = dict(used)
        if source is not None:
            data['prerelease_source'] = source
    handle, temporary = tempfile.mkstemp(prefix='.sync.', suffix='.json', dir=layout.state)
    with os.fdopen(handle, 'w') as output:
        json.dump(data, output, indent=2)
        output.write('\n')
    os.chmod(temporary, 0o644)
    os.replace(temporary, layout.sync_json)


def read_sync_state(layout: Layout) -> dict:
    try:
        data = json.loads(layout.sync_json.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _descriptor_status(directory: Path) -> tuple[dict | None, str | None]:
    try:
        return read_descriptor(directory), None
    except DescriptorError as exc:
        return None, str(exc)


def list_lines(layout: Layout, version_list: VersionList, host: str,
               ids: Iterable[str] = ()) -> list[str]:
    state = read_sync_state(layout)
    resolved = state.get('resolved') or {}
    used = state.get('fallbacks_used') or {}
    selectors = select(version_list, list(ids))
    lines = [f'host: {host}' + ('' if host in NATIVE_HOSTS else ' (no Kotlin/Native prebuilt)'),
             f'versions: {version_list.source}',
             f'home: {layout.root}',
             f'last sync: {state.get("synced_at", "never")}', '']
    rows = [('id', 'status', 'version', 'backends', 'abi', 'origin')]
    requested: dict[str, set[str]] = {}
    for selector in selectors:
        version = selector_version(selector, resolved)
        if version is None:
            if selector.kind == 'master' and 'master' not in version_list.fallbacks:
                status = 'empty'
            else:
                status = 'unresolved (run sync)'
            rows.append((selector.text, status, '-', '-', '-', '-'))
            continue
        requested.setdefault(version, set()).update(selector.backends)
        descriptor, problem = _descriptor_status(layout.compiler_dir(version))
        installed = set(descriptor['backends']) if descriptor else set()
        missing = selector.backends - installed
        if problem:
            status = 'corrupt'
        elif not missing:
            status = 'installed'
        elif installed:
            status = f'missing {",".join(b for b in BACKENDS if b in missing)}'
        else:
            status = 'missing'
        if selector.kind in used:
            status = f'fallback → {used[selector.kind]}, {status}'
        rows.append((selector.text, status, version,
                     ','.join(b for b in BACKENDS if b in installed) or '-',
                     descriptor['abi_version'] if descriptor else '-',
                     descriptor['origin'] if descriptor else '-'))
    for version in layout.installed_versions():
        descriptor, problem = _descriptor_status(layout.compiler_dir(version))
        if version not in requested:
            rows.append((version, 'corrupt' if problem else 'undeclared', version,
                         ','.join(descriptor['backends']) if descriptor else '-',
                         descriptor['abi_version'] if descriptor else '-',
                         descriptor['origin'] if descriptor else '-'))
            continue
        extra = set(descriptor['backends']) - requested[version] if descriptor else set()
        if extra:
            rows.append((version, f'extra {",".join(b for b in BACKENDS if b in extra)}',
                         version, ','.join(descriptor['backends']) if descriptor else '-',
                         descriptor['abi_version'] if descriptor else '-',
                         descriptor['origin'] if descriptor else '-'))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines += ['  '.join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
              for row in rows]
    lines += ['', 'symlinks:']
    for slot in SLOTS:
        lines.append(f'  {slot} -> {layout.symlink_target(slot) or "(none)"}')
    return lines
