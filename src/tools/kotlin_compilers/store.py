"""A checksum-addressed store for jars that recur across versions.

``store/<sha256>/<name>`` holds one copy of each shareable jar; version
directories hold relative symlinks into it. ``store/index.json`` maps
``md5:``, ``sha1:`` and ``sha256:`` digests to entries, so a Maven artifact
whose published ``.sha1``/``.md5`` is already indexed is linked without being
downloaded. md5 is only a lookup key: a hit also needs a published sha1 or
sha256 that agrees, and downloads are always verified against the strongest
published sidecar.

Links are written relative to where the file ends up after publishing
(``final``), not to the staging path, so they resolve once the part is renamed
into place and keep working when the whole tree is moved.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from . import download
from .download import log


SCHEMA = 1
INDEX = 'index.json'
DIGESTS = ('md5', 'sha1', 'sha256')
STRONG = ('sha256', 'sha1')
# Jars that recur across versions; everything else is version-specific and stays a real file.
SHAREABLE = re.compile(r'(?:annotations(?:-[0-9][0-9A-Za-z.]*)?|kotlinx-coroutines-core-jvm|'
                       r'kotlin-reflect)\.jar')


def is_shareable(name: str) -> bool:
    return SHAREABLE.fullmatch(name) is not None


def file_digests(path: Path) -> dict[str, str]:
    """md5, sha1 and sha256 of ``path`` in one pass."""
    hashes = {algorithm: hashlib.new(algorithm) for algorithm in DIGESTS}
    with open(path, 'rb') as source:
        while chunk := source.read(download.CHUNK):
            for digest in hashes.values():
                digest.update(chunk)
    return {algorithm: digest.hexdigest() for algorithm, digest in hashes.items()}


def link(entry: Path, dest: Path, final: Path | None = None) -> None:
    """A relative symlink at ``dest`` to ``entry``, computed from ``final`` (default ``dest``)."""
    target = os.path.relpath(entry, (final or dest).parent)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    os.symlink(target, dest)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.index_path = root / INDEX

    # -- index ---------------------------------------------------------------

    def _entry_file(self, sha256: str) -> Path | None:
        directory = self.root / sha256
        if not directory.is_dir():
            return None
        # Dotfiles (a Finder .DS_Store, an editor's swap file) are never entries.
        files = sorted(p for p in directory.iterdir()
                       if p.is_file() and not p.is_symlink() and not p.name.startswith('.'))
        return files[0] if files else None

    def _on_disk(self) -> set[str]:
        if not self.root.is_dir():
            return set()
        return {entry.name for entry in self.root.iterdir()
                if entry.is_dir() and self._entry_file(entry.name) is not None}

    def _empty(self) -> dict:
        return {'schema': SCHEMA, 'entries': {}, 'by_digest': {}}

    def _record(self, index: dict, path: Path, digests: dict[str, str]) -> None:
        sha256 = digests['sha256']
        index['entries'][sha256] = {'name': path.name, 'size': path.stat().st_size, **digests}
        for algorithm, digest in digests.items():
            index['by_digest'][f'{algorithm}:{digest}'] = sha256

    def rebuild(self) -> dict:
        """Re-hash every store entry and rewrite the index."""
        index = self._empty()
        for sha256 in sorted(self._on_disk()):
            path = self._entry_file(sha256)
            digests = file_digests(path)
            if digests['sha256'] != sha256:
                log(f'  warning: store entry {path} does not match its name; ignored')
                continue
            self._record(index, path, digests)
        self._save(index)
        return index

    def index(self) -> dict:
        """The index, rebuilt when missing, unreadable or out of step with ``store/``."""
        try:
            index = json.loads(self.index_path.read_text())
            if (index.get('schema') != SCHEMA or not isinstance(index.get('entries'), dict)
                    or not isinstance(index.get('by_digest'), dict)):
                raise ValueError('unexpected index shape')
        except (OSError, ValueError):
            return self.rebuild() if self.root.is_dir() else self._empty()
        if set(index['entries']) != self._on_disk():
            return self.rebuild()
        return index

    def _save(self, index: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix='.index.', suffix='.json', dir=self.root)
        try:
            with os.fdopen(handle, 'w') as output:
                json.dump(index, output, indent=2, sort_keys=True)
                output.write('\n')
            os.chmod(temporary, 0o644)
            os.replace(temporary, self.index_path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    # -- API -----------------------------------------------------------------

    def lookup(self, digests: dict[str, str]) -> Path | None:
        """The store file matching published ``digests`` (``{"sha1": ..., "md5": ...}``).

        Every given digest must agree with the entry, and at least one of them
        must be sha1 or sha256, so a lone md5 match never links a file.
        """
        digests = {algorithm: digest.lower() for algorithm, digest in digests.items()
                   if algorithm in DIGESTS and digest}
        if not any(algorithm in STRONG for algorithm in digests):
            return None
        index = self.index()
        candidates = {index['by_digest'].get(f'{algorithm}:{digest}')
                      for algorithm, digest in digests.items()} - {None}
        for sha256 in sorted(candidates):
            entry = index['entries'].get(sha256, {})
            if any(entry.get(algorithm) != digest for algorithm, digest in digests.items()):
                continue
            path = self._entry_file(sha256)
            if path is not None:
                return path
            self.rebuild()
        return None

    def adopt(self, staged: Path, final: Path | None = None) -> Path:
        """Move ``staged`` into the store (or drop it for an identical entry); leave a symlink."""
        digests = file_digests(staged)
        sha256 = digests['sha256']
        index = self.index()
        entry = self._entry_file(sha256)
        if entry is not None and file_digests(entry)['sha256'] != sha256:
            log(f'  warning: store entry {entry} does not match its name; replaced')
            entry = None
        if entry is None:
            directory = self.root / sha256
            directory.mkdir(parents=True, exist_ok=True)
            entry = directory / staged.name
            os.replace(staged, entry)
            entry.chmod(0o644)
        else:
            staged.unlink()
        self._record(index, entry, digests)
        self._save(index)
        link(entry, staged, final)
        return entry

    def fetch_or_link(self, url: str, dest: Path, final: Path | None = None) -> bool:
        """Link ``dest`` to a store entry if the published digests match; else download it.

        Returns ``True`` when linked. Downloaded files with a shareable name
        enter the store.
        """
        published = download.published_digests(url)
        entry = self.lookup({checksum.algorithm: checksum.digest for checksum in published})
        if entry is not None:
            link(entry, dest, final)
            log(f'  linked {dest.name} from store/{entry.parent.name[:12]}… '
                f'(published {", ".join(c.algorithm for c in published)} match)')
            return True
        download.fetch(url, dest, published[0] if published else None)
        if is_shareable(dest.name):
            self.adopt(dest, final)
        return False
