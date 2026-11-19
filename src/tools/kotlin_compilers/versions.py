"""The declared compiler set: selector parsing, validation and merging.

A selector is ``<base>[-<backend>]``. The base is ``prerelease``, ``master``
or an exact published version; the optional suffix limits the install to one
backend. Selectors are requests, not directory names: every selector that
resolves to the same exact version merges into one directory.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Literal


BACKENDS = ('jvm', 'js', 'wasm', 'native')
SLOTS = ('prerelease', 'master')
FALLBACK_SLOTS = ('master',)
VERSIONS_ENV = 'KOTLIN_COMPILERS_VERSIONS'
DEFAULT_VERSIONS_FILE = Path(__file__).resolve().parents[3] / 'versions.json'

EXACT_VERSION = re.compile(
    r'(\d+)\.(\d+)\.(\d+)(?:-(?:Beta\d*|RC\d*|dev-\d+|Beta-\d+|RC-\d+|RC\d+-\d+))?')
# Builds that exist only on the JetBrains dev Maven repo (no GitHub release).
DEV_VERSION = re.compile(r'\d+\.\d+\.\d+-(?:dev|Beta|RC\d*)-\d+')
TAG = re.compile(r'v?(\d+)\.(\d+)\.(\d+)(?:-(Beta|RC)(\d*))?')

SelectorKind = Literal['release', 'prerelease', 'master']


class VersionsError(ValueError):
    pass


@dataclass(frozen=True)
class Selector:
    text: str
    kind: SelectorKind
    base: str
    backends: frozenset[str]


@dataclass(frozen=True)
class VersionList:
    selectors: tuple[Selector, ...]
    fallbacks: Mapping[str, str]
    source: Path | None = None


def is_exact_version(text: str) -> bool:
    return EXACT_VERSION.fullmatch(text) is not None


def is_dev_version(text: str) -> bool:
    return DEV_VERSION.fullmatch(text) is not None


def version_numbers(version: str) -> tuple[int, int, int]:
    match = EXACT_VERSION.fullmatch(version)
    if match is None:
        raise VersionsError(f'not an exact Kotlin version: {version!r}')
    return int(match[1]), int(match[2]), int(match[3])


def version_key(tag: str) -> tuple[int, int, int, int, int] | None:
    """Order release tags by version: Beta < RC < final; ``None`` for odd tags."""
    match = TAG.fullmatch(tag)
    if match is None:
        return None
    major, minor, patch, stage, number = match.groups()
    if stage is None:
        return int(major), int(minor), int(patch), 2, 0
    return int(major), int(minor), int(patch), {'Beta': 0, 'RC': 1}[stage], int(number or 1)


def parse_selector(text: str) -> Selector:
    if not isinstance(text, str) or not text:
        raise VersionsError(f'selector must be a non-empty string: {text!r}')
    base, _, last = text.rpartition('-')
    if base and last in BACKENDS:
        backends = frozenset({last})
    else:
        base, backends = text, frozenset(BACKENDS)
    if base in SLOTS:
        return Selector(text, base, base, backends)
    if is_exact_version(base):
        return Selector(text, 'release', base, backends)
    slot = next((slot for slot in SLOTS if base.startswith(slot + '-')), None)
    if slot is not None:
        raise VersionsError(f'unknown backend suffix in {text!r}; '
                            f'expected {slot} or {slot}-<{"|".join(BACKENDS)}>')
    raise VersionsError(
        f'invalid selector {text!r}: expected prerelease, master or '
        f'<maj>.<min>.<patch>[-Beta<n>|-RC<n>|-dev-<n>], optionally '
        f'followed by -<{"|".join(BACKENDS)}>')


def parse_version_list(data: object, source: Path | None = None) -> VersionList:
    where = f' in {source}' if source else ''
    if not isinstance(data, dict):
        raise VersionsError(f'expected a JSON object{where}')
    unknown = set(data) - {'versions', 'fallbacks'}
    if unknown:
        raise VersionsError(f'unknown keys{where}: {", ".join(sorted(unknown))}')
    entries = data.get('versions')
    if not isinstance(entries, list):
        raise VersionsError(f'"versions" must be a list of selectors{where}')
    selectors = []
    seen = set()
    for entry in entries:
        selector = parse_selector(entry)
        if selector.text in seen:
            raise VersionsError(f'duplicate selector {selector.text!r}{where}')
        seen.add(selector.text)
        selectors.append(selector)
    fallbacks = data.get('fallbacks', {})
    if not isinstance(fallbacks, dict):
        raise VersionsError(f'"fallbacks" must be an object{where}')
    for slot, target in fallbacks.items():
        if slot not in FALLBACK_SLOTS:
            raise VersionsError(f'fallback key {slot!r}{where}: only '
                                f'{", ".join(FALLBACK_SLOTS)} can be empty')
        if target == slot:
            raise VersionsError(f'fallback for {slot!r} points at itself{where}')
        if not isinstance(target, str) or not (target == 'prerelease' or is_exact_version(target)):
            raise VersionsError(f'fallback for {slot!r}{where} must be prerelease or an '
                                f'exact version, not {target!r}')
    return VersionList(tuple(selectors), dict(fallbacks), source)


def versions_file(override: str | os.PathLike[str] | None = None) -> Path:
    return Path(override or os.environ.get(VERSIONS_ENV) or DEFAULT_VERSIONS_FILE).expanduser()


def load_versions(override: str | os.PathLike[str] | None = None) -> VersionList:
    path = versions_file(override)
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise VersionsError(f'cannot read {path}: {exc}') from exc
    except ValueError as exc:
        raise VersionsError(f'{path} is not valid JSON: {exc}') from exc
    return parse_version_list(data, path)


def resolve_slots(prerelease: str | None, master: str | None,
                  fallbacks: Mapping[str, str]) -> dict[str, str | None]:
    """Exact version per slot; an empty ``master`` takes its fallback's version."""
    slots: dict[str, str | None] = {'prerelease': prerelease, 'master': master}
    for slot, target in fallbacks.items():
        if slots.get(slot) is None:
            slots[slot] = slots.get(target) if target in SLOTS else target
    return slots


def fallbacks_used(master: str | None, fallbacks: Mapping[str, str]) -> dict[str, str]:
    return {slot: target for slot, target in fallbacks.items()
            if slot == 'master' and master is None}


def selector_version(selector: Selector, slots: Mapping[str, str | None]) -> str | None:
    return selector.base if selector.kind == 'release' else slots.get(selector.kind)


def merge(selectors: Iterable[Selector],
          slots: Mapping[str, str | None]) -> dict[str, frozenset[str]]:
    """Exact version -> union of requested backends; unresolved slots are skipped."""
    merged: dict[str, frozenset[str]] = {}
    for selector in selectors:
        version = selector_version(selector, slots)
        if version is not None:
            merged[version] = merged.get(version, frozenset()) | selector.backends
    return merged
