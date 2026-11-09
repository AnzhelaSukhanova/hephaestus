from __future__ import annotations

import json

import pytest

from src.tools.kotlin_compilers import versions
from src.tools.kotlin_compilers.host import HostError, detect_host, require_native_host, resolve_host
from src.tools.kotlin_compilers.versions import (
    BACKENDS, VersionsError, fallbacks_used, load_versions, merge, parse_selector,
    parse_version_list, resolve_slots, version_key,
)


ALL = frozenset(BACKENDS)


@pytest.mark.parametrize('text, kind, base, backends', [
    ('2.3.0', 'release', '2.3.0', ALL),
    ('2.4.21-native', 'release', '2.4.21', {'native'}),
    ('2.5.0-Beta2', 'release', '2.5.0-Beta2', ALL),
    ('2.5.0-Beta2-wasm', 'release', '2.5.0-Beta2', {'wasm'}),
    ('2.4.21-RC', 'release', '2.4.21-RC', ALL),
    ('2.5.20-dev-3321', 'release', '2.5.20-dev-3321', ALL),
    ('2.5.20-dev-3321-js', 'release', '2.5.20-dev-3321', {'js'}),
    ('prerelease', 'prerelease', 'prerelease', ALL),
    ('prerelease-native', 'prerelease', 'prerelease', {'native'}),
    ('master', 'master', 'master', ALL),
    ('master-jvm', 'master', 'master', {'jvm'}),
])
def test_parse_selector(text, kind, base, backends):
    selector = parse_selector(text)
    assert (selector.text, selector.kind, selector.base, selector.backends) == (
        text, kind, base, frozenset(backends))


@pytest.mark.parametrize('text, message', [
    ('prerelease-natvie', 'unknown backend suffix'),
    ('master-', 'unknown backend suffix'),
    ('2.4.21-natvie', 'invalid selector'),
    ('2.4', 'invalid selector'),
    ('native', 'invalid selector'),
    ('2.5.0-M1', 'invalid selector'),
    ('', 'non-empty'),
])
def test_parse_selector_rejects(text, message):
    with pytest.raises(VersionsError, match=message):
        parse_selector(text)


def test_committed_versions_file_loads():
    loaded = load_versions(versions.DEFAULT_VERSIONS_FILE)
    assert [s.text for s in loaded.selectors] == ['master', '2.3.21-native']
    assert dict(loaded.fallbacks) == {'master': 'prerelease'}


def test_duplicate_selectors_rejected():
    with pytest.raises(VersionsError, match='duplicate selector'):
        parse_version_list({'versions': ['2.3.0', '2.3.0']})


@pytest.mark.parametrize('fallbacks, message', [
    ({'prerelease': '2.3.0'}, 'only master'),
    ({'master': '2.4'}, 'must be prerelease or an exact version'),
    ({'master': 'natvie'}, 'must be prerelease or an exact version'),
    ({'master': 'master'}, 'points at itself'),
])
def test_bad_fallbacks_rejected(fallbacks, message):
    with pytest.raises(VersionsError, match=message):
        parse_version_list({'versions': [], 'fallbacks': fallbacks})


def test_missing_fallbacks_accepted():
    assert dict(parse_version_list({'versions': ['2.3.0']}).fallbacks) == {}


def test_unknown_keys_and_bad_shapes_rejected():
    with pytest.raises(VersionsError, match='unknown keys'):
        parse_version_list({'versions': [], 'compilers': []})
    with pytest.raises(VersionsError, match='list of selectors'):
        parse_version_list({'versions': {'2.3.0': 1}})


def test_merge_unions_backends_per_version():
    selectors = [parse_selector(t) for t in ('2.4.21-js', '2.4.21-wasm', '2.3.21-native')]
    assert merge(selectors, {}) == {'2.4.21': {'js', 'wasm'}, '2.3.21': {'native'}}


def test_prerelease_resolved_to_pinned_version_merges():
    selectors = [parse_selector(t) for t in ('2.4.21-native', 'prerelease-js')]
    slots = resolve_slots('2.4.21', None, {})
    assert merge(selectors, slots) == {'2.4.21': {'native', 'js'}}


def test_unresolved_slot_selectors_are_skipped():
    selectors = [parse_selector(t) for t in ('master-native', '2.3.0-jvm')]
    assert merge(selectors, resolve_slots('2.5.0-Beta2', None, {})) == {'2.3.0': {'jvm'}}


def test_resolve_slots_applies_fallbacks():
    assert resolve_slots('2.5.0-Beta2', None, {'master': 'prerelease'}) == {
        'prerelease': '2.5.0-Beta2', 'master': '2.5.0-Beta2'}
    assert resolve_slots('2.5.0-Beta2', None, {'master': '2.4.21'})['master'] == '2.4.21'
    assert resolve_slots('2.5.0-Beta2', '2.5.20-dev-1', {'master': 'prerelease'})['master'] == \
        '2.5.20-dev-1'
    assert fallbacks_used(None, {'master': 'prerelease'}) == {'master': 'prerelease'}
    assert fallbacks_used('2.5.20-dev-1', {'master': 'prerelease'}) == {}


def test_version_key_orders_beta_rc_final():
    tags = ['v2.4.21-RC', 'v2.5.0-Beta1', 'v2.5.0-Beta2', 'v2.5.0-RC1', 'v2.5.0', 'v2.4.21']
    assert sorted(tags, key=version_key) == [
        'v2.4.21-RC', 'v2.4.21', 'v2.5.0-Beta1', 'v2.5.0-Beta2', 'v2.5.0-RC1', 'v2.5.0']
    assert version_key('v2.5.0-Beta') == version_key('v2.5.0-Beta1')
    for odd in ('build-2.5.0', 'v2.5.0-M1', 'v2.5.20-dev-3321'):
        assert version_key(odd) is None


def test_versions_override(tmp_path, monkeypatch):
    path = tmp_path / 'mine.json'
    path.write_text(json.dumps({'versions': ['2.2.0']}))
    assert [s.text for s in load_versions(path).selectors] == ['2.2.0']
    monkeypatch.setenv(versions.VERSIONS_ENV, str(path))
    assert [s.text for s in load_versions().selectors] == ['2.2.0']
    other = tmp_path / 'other.json'
    other.write_text(json.dumps({'versions': ['2.3.0']}))
    assert [s.text for s in load_versions(other).selectors] == ['2.3.0']


def test_invalid_json_reported(tmp_path):
    path = tmp_path / 'broken.json'
    path.write_text('{')
    with pytest.raises(VersionsError, match='not valid JSON'):
        load_versions(path)


@pytest.mark.parametrize('system, machine, host', [
    ('Darwin', 'arm64', 'macos-aarch64'),
    ('Darwin', 'x86_64', 'macos-x86_64'),
    ('Linux', 'x86_64', 'linux-x86_64'),
    ('Linux', 'AMD64', 'linux-x86_64'),
])
def test_host_mapping(system, machine, host):
    assert detect_host(system, machine) == host
    assert require_native_host(host) == host


def test_unsupported_host_fails_for_native_only():
    host = detect_host('Linux', 'aarch64')
    assert host == 'linux-aarch64'
    with pytest.raises(HostError, match='no prebuilt'):
        require_native_host(host)
    assert detect_host('Windows', 'AMD64') == 'windows-amd64'


def test_host_override():
    assert resolve_host('linux-x86_64') == 'linux-x86_64'
    with pytest.raises(HostError, match='unknown host'):
        resolve_host('linux-aarch64')
