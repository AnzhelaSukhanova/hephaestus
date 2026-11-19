"""Host detection for the Kotlin/Native prebuilt."""

from __future__ import annotations

import platform


NATIVE_HOSTS = ('macos-aarch64', 'macos-x86_64', 'linux-x86_64')
_HOSTS = {
    ('darwin', 'arm64'): 'macos-aarch64',
    ('darwin', 'aarch64'): 'macos-aarch64',
    ('darwin', 'x86_64'): 'macos-x86_64',
    ('linux', 'x86_64'): 'linux-x86_64',
    ('linux', 'amd64'): 'linux-x86_64',
}


class HostError(ValueError):
    pass


def detect_host(system: str | None = None, machine: str | None = None) -> str:
    """The Native host name, or ``<system>-<machine>`` for hosts without one."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    return _HOSTS.get((system, machine), f'{system}-{machine}')


def resolve_host(override: str | None = None) -> str:
    if override is None:
        return detect_host()
    if override not in NATIVE_HOSTS:
        raise HostError(f'unknown host {override!r}; expected one of {", ".join(NATIVE_HOSTS)}')
    return override


def require_native_host(host: str) -> str:
    if host not in NATIVE_HOSTS:
        raise HostError(f'Kotlin/Native has no prebuilt for host {host!r} '
                        f'(supported: {", ".join(NATIVE_HOSTS)}); jvm/js/wasm still install')
    return host
