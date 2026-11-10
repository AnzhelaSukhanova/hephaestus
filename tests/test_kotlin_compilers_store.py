from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import shutil
import urllib.error

import pytest

from tests.kotlin_compilers_fakes import install_fake_web
from src.tools.kotlin_compilers import download
from src.tools.kotlin_compilers.download import DownloadError
from src.tools.kotlin_compilers.store import Store, file_digests, is_shareable


URL = 'https://repo1.maven.org/maven2/org/jetbrains/annotations/13.0/annotations-13.0.jar'
DATA = b'annotations 13.0'


@pytest.fixture
def web(monkeypatch):
    return install_fake_web(monkeypatch)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'root' / 'store')


def adopt(store, tmp_path, name='annotations-13.0.jar', data=DATA, version='1.0'):
    staged = tmp_path / 'root' / 'state' / 'tmp' / f'{version}.stage' / 'kotlinc' / 'lib' / name
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)
    final = tmp_path / 'root' / version / 'kotlinc' / 'lib' / name
    return store.adopt(staged, final), staged, final


@pytest.mark.parametrize('name, shareable', [
    ('annotations-13.0.jar', True), ('annotations.jar', True), ('kotlin-reflect.jar', True),
    ('kotlinx-coroutines-core-jvm.jar', True), ('kotlin-compiler.jar', False),
    ('kotlin-stdlib.jar', False), ('kotlin-preloader.jar', False),
    ('kotlin-stdlib-js.klib', False)])
def test_shareable_names(name, shareable):
    assert is_shareable(name) is shareable


def test_adopt_indexes_all_three_digests_and_links_relative_to_the_final_place(store, tmp_path):
    entry, staged, final = adopt(store, tmp_path)
    digests = file_digests(entry)
    assert entry == store.root / digests['sha256'] / 'annotations-13.0.jar'
    target = os.readlink(staged)
    assert not os.path.isabs(target)
    assert os.path.normpath(final.parent / target) == str(entry)
    index = json.loads((store.root / 'index.json').read_text())
    assert index['entries'][digests['sha256']]['md5'] == hashlib.md5(DATA).hexdigest()
    for algorithm in ('md5', 'sha1', 'sha256'):
        assert index['by_digest'][f'{algorithm}:{digests[algorithm]}'] == digests['sha256']
    for algorithm in ('sha1', 'sha256'):
        assert store.lookup({algorithm: digests[algorithm]}) == entry


def test_identical_content_shares_one_entry(store, tmp_path):
    first, _, _ = adopt(store, tmp_path, version='2.3.0')
    second, _, _ = adopt(store, tmp_path, version='2.4.21')
    assert first == second
    assert len([p for p in store.root.iterdir() if p.is_dir()]) == 1


def test_different_content_gets_separate_entries(store, tmp_path):
    first, _, _ = adopt(store, tmp_path, 'kotlin-reflect.jar', b'reflect 2.3.0', '2.3.0')
    second, _, _ = adopt(store, tmp_path, 'kotlin-reflect.jar', b'reflect 2.4.21', '2.4.21')
    assert first != second and first.name == second.name == 'kotlin-reflect.jar'


def test_md5_alone_never_links(store, tmp_path):
    entry, _, _ = adopt(store, tmp_path)
    md5 = hashlib.md5(DATA).hexdigest()
    assert store.lookup({'md5': md5}) is None
    assert store.lookup({'md5': md5, 'sha1': hashlib.sha1(DATA).hexdigest()}) == entry
    assert store.lookup({'md5': md5, 'sha1': '0' * 40}) is None


def test_fetch_on_a_hit_requests_only_the_sidecars(web, store, tmp_path):
    entry, _, _ = adopt(store, tmp_path)
    web.add(URL, DATA, 'maven')
    dest = tmp_path / 'root' / 'dev' / 'kotlinc' / 'lib' / 'annotations.jar'
    assert store.fetch_or_link(URL, dest) is True
    assert ('GET', URL) not in web.requests
    assert {url for _, url in web.requests} == {URL + '.sha256', URL + '.sha1', URL + '.md5'}
    assert dest.is_symlink() and dest.resolve() == entry.resolve()


def test_md5_hit_with_a_disagreeing_sha1_downloads(web, store, tmp_path):
    adopt(store, tmp_path)
    other = b'something else'
    web.files[URL] = other
    web.files[URL + '.md5'] = hashlib.md5(DATA).hexdigest().encode()
    web.files[URL + '.sha1'] = hashlib.sha1(other).hexdigest().encode()
    dest = tmp_path / 'root' / 'dev' / 'kotlinc' / 'lib' / 'annotations.jar'
    assert store.fetch_or_link(URL, dest) is False
    assert ('GET', URL) in web.requests
    assert dest.resolve().read_bytes() == other
    assert len([p for p in store.root.iterdir() if p.is_dir()]) == 2


@pytest.mark.parametrize('name, in_store', [('annotations.jar', True), ('kotlin-compiler.jar', False)])
def test_sidecarless_url_downloads_unverified(web, store, tmp_path, capsys, name, in_store):
    web.add(URL, DATA)
    dest = tmp_path / 'root' / 'dev' / 'kotlinc' / 'lib' / name
    assert store.fetch_or_link(URL, dest) is False
    assert 'no checksum published' in capsys.readouterr().err
    assert dest.is_symlink() is in_store and dest.resolve().read_bytes() == DATA


def test_mismatching_download_is_rejected(web, store, tmp_path):
    web.add(URL, DATA, 'maven')
    web.files[URL + '.sha1'] = b'1' * 40
    dest = tmp_path / 'root' / 'dev' / 'kotlinc' / 'lib' / 'annotations.jar'
    with pytest.raises(Exception, match='sha1 mismatch'):
        store.fetch_or_link(URL, dest)
    assert not dest.exists() and not dest.is_symlink()


def test_deleted_entry_falls_back_to_downloading_and_rebuilds_the_index(web, store, tmp_path):
    entry, _, _ = adopt(store, tmp_path)
    shutil.rmtree(entry.parent)
    web.add(URL, DATA, 'maven')
    dest = tmp_path / 'root' / 'dev' / 'kotlinc' / 'lib' / 'annotations.jar'
    assert store.fetch_or_link(URL, dest) is False
    assert ('GET', URL) in web.requests
    assert dest.resolve().read_bytes() == DATA
    index = json.loads((store.root / 'index.json').read_text())
    assert list(index['entries']) == [hashlib.sha256(DATA).hexdigest()]


def test_missing_index_is_rebuilt_from_the_entries(store, tmp_path):
    entry, _, _ = adopt(store, tmp_path)
    (store.root / 'index.json').unlink()
    assert store.lookup({'sha1': hashlib.sha1(DATA).hexdigest()}) == entry
    assert (store.root / 'index.json').is_file()
    (store.root / 'index.json').write_text('{')
    assert store.lookup({'sha1': hashlib.sha1(DATA).hexdigest()}) == entry


def test_stray_dotfile_in_an_entry_is_ignored(store, tmp_path, monkeypatch):
    entry, _, _ = adopt(store, tmp_path, version='2.3.0')
    (entry.parent / '.DS_Store').write_bytes(b'junk')
    rebuilds = []
    monkeypatch.setattr(store, 'rebuild', lambda: rebuilds.append(1))
    second, staged, final = adopt(store, tmp_path, version='2.4.21')
    assert second == entry and entry.read_bytes() == DATA
    assert os.path.normpath(final.parent / os.readlink(staged)) == str(entry)
    assert store.lookup({'sha1': hashlib.sha1(DATA).hexdigest()}) == entry
    assert rebuilds == []


def test_adopt_replaces_an_entry_that_does_not_match_its_name(store, tmp_path, capsys):
    entry, _, _ = adopt(store, tmp_path, version='2.3.0')
    entry.write_bytes(b'corrupted')
    second, staged, final = adopt(store, tmp_path, version='2.4.21')
    assert second == entry and entry.read_bytes() == DATA
    assert os.path.normpath(final.parent / os.readlink(staged)) == str(entry)
    assert 'does not match its name' in capsys.readouterr().err


def flaky_sidecars(web, monkeypatch, code, failures):
    """Fail the first ``failures`` requests of every sidecar with HTTP ``code``."""
    seen = {}

    def open_url(url, method='GET'):
        if url.endswith(('.sha256', '.sha1', '.md5')):
            seen[url] = seen.get(url, 0) + 1
            if seen[url] <= failures:
                raise urllib.error.HTTPError(url, code, 'flaky', {}, None)
        return web.open(url, method)

    monkeypatch.setattr(download, 'open_url', open_url)
    return seen


@pytest.mark.parametrize('code', [408, 429, 503])
def test_transient_sidecar_errors_are_retried(web, store, tmp_path, monkeypatch, capsys, code):
    web.add(URL, DATA, 'maven')
    seen = flaky_sidecars(web, monkeypatch, code, failures=1)
    dest = tmp_path / 'root' / 'dev' / 'kotlinc' / 'lib' / 'annotations.jar'
    assert store.fetch_or_link(URL, dest) is False
    assert seen[URL + '.sha1'] == 2
    assert 'no checksum published' not in capsys.readouterr().err
    assert dest.resolve().read_bytes() == DATA


def test_failing_sidecar_fails_instead_of_installing_unverified(web, store, tmp_path, monkeypatch):
    web.add(URL, DATA, 'maven')
    flaky_sidecars(web, monkeypatch, 429, failures=download.RETRIES)
    dest = tmp_path / 'root' / 'dev' / 'kotlinc' / 'lib' / 'annotations.jar'
    with pytest.raises(DownloadError, match='HTTP 429'):
        store.fetch_or_link(URL, dest)
    assert ('GET', URL) not in web.requests
    assert not dest.exists() and not dest.is_symlink()
    assert not store.root.exists()


class TruncatedBody(io.BytesIO):
    def read(self, size=-1):
        if self.tell():
            raise http.client.IncompleteRead(b'', 10)
        return super().read(size)


def test_truncated_body_is_a_download_error_and_leaves_no_part_file(monkeypatch, tmp_path):
    monkeypatch.setattr(download, 'open_url', lambda url, method='GET': TruncatedBody(b'abc'))
    dest = tmp_path / 'x.jar'
    with pytest.raises(DownloadError, match='IncompleteRead'):
        download.fetch('https://example.org/x.jar', dest, None)
    assert list(tmp_path.iterdir()) == []
