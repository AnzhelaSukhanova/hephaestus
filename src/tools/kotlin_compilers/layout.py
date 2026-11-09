"""The ``~/kotlin-compilers`` directory contract: paths, publishing, symlinks.

A compiler directory is assembled in ``state/tmp`` and renamed into place
once complete. A backend added later is staged as its own part, renamed into
the existing directory, and only then recorded by atomically replacing
``compiler.json``. Installed parts are never modified or deleted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import tempfile

from .descriptor import (
    DESCRIPTOR, DescriptorError, backends_on_disk, build_descriptor, parts_for,
    read_descriptor, write_descriptor,
)
from .download import log
from .versions import SLOTS


HOME_ENV = 'KOTLIN_COMPILERS_HOME'
DEFAULT_HOME = '~/kotlin-compilers'

# stage(part, target, downloads): fill ``target`` (a fresh directory path) with one part.
Stager = Callable[[str, Path, Path], None]


class Layout:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root or os.environ.get(HOME_ENV) or DEFAULT_HOME).expanduser()
        self.state = self.root / 'state'
        self.tmp = self.state / 'tmp'
        self.sync_json = self.state / 'sync.json'
        self.lock = self.state / 'sync.lock'
        self.store = self.root / 'store'

    def compiler_dir(self, version: str) -> Path:
        return self.root / version

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold ``state/sync.lock``; a concurrent sync waits here."""
        self.ensure()
        with open(self.lock, 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def ensure(self) -> None:
        self.tmp.mkdir(parents=True, exist_ok=True)

    def clean_tmp(self) -> None:
        """Remove debris of interrupted runs; call only while holding the sync lock."""
        if self.tmp.is_dir():
            for entry in self.tmp.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)

    def descriptor(self, version: str) -> dict | None:
        """The installed descriptor, or ``None`` when missing or corrupt."""
        try:
            return read_descriptor(self.compiler_dir(version))
        except DescriptorError:
            return None

    def installed_versions(self) -> list[str]:
        """Exact-version directories (symlinks and ``state`` excluded), sorted by name."""
        if not self.root.is_dir():
            return []
        return sorted(entry.name for entry in self.root.iterdir()
                      if entry.is_dir() and not entry.is_symlink()
                      and entry.name not in ('state', 'store') and entry.name not in SLOTS
                      and (entry / DESCRIPTOR).exists())

    def symlink_target(self, name: str) -> str | None:
        path = self.root / name
        return os.readlink(path) if path.is_symlink() else None

    def set_symlink(self, name: str, version: str | None) -> None:
        """Point ``name`` at ``version`` atomically, or remove it for ``None``."""
        path = self.root / name
        if version is None:
            if path.is_symlink():
                path.unlink()
            return
        if path.exists() and not path.is_symlink():
            raise OSError(f'{path} exists and is not a symlink')
        if self.symlink_target(name) == version:
            return
        self.ensure()
        temporary = self.tmp / f'.{name}.symlink'
        temporary.unlink(missing_ok=True)
        os.symlink(version, temporary)
        os.replace(temporary, path)


def install_version(layout: Layout, version: str, *, origin: str, host: str,
                    backends: Iterable[str], stage: Stager) -> tuple[dict, list[str]]:
    """Make ``version`` hold at least ``backends``; return its descriptor and the new parts."""
    directory = layout.compiler_dir(version)
    current = layout.descriptor(version) if directory.is_dir() else None
    if current is not None:
        have = set(current['backends'])
    elif directory.is_dir():
        # Keep what is already installed instead of recording only this request.
        have = backends_on_disk(directory)
        log(f'  {DESCRIPTOR} missing or corrupt; rebuilding it from the parts present'
            f'{" (" + ", ".join(sorted(have)) + ")" if have else ""}')
    else:
        have = set()
    wanted = have | set(backends)
    if current is not None and wanted == have:
        return current, []
    # Backends found on disk are recorded as they are; only the others get parts staged.
    needed = wanted if current is not None else set(backends)
    missing = [part for part in parts_for(needed) if not (directory / part).exists()]
    layout.ensure()
    staging = Path(tempfile.mkdtemp(prefix=f'{version}.', dir=layout.tmp))
    downloads = staging / '.downloads'
    try:
        downloads.mkdir()
        for part in missing:
            stage(part, staging / part, downloads)
        shutil.rmtree(downloads)
        # An optional part (stdlib/) may be left out when nothing belongs in it.
        missing = [part for part in missing if (staging / part).exists()]
        installed_at = current['installed_at'] if current else None
        if not directory.exists():
            descriptor = build_descriptor(staging, version=version, origin=origin, host=host,
                                          backends=wanted, installed_at=installed_at)
            write_descriptor(staging, descriptor)
            staging.chmod(0o755)
            os.rename(staging, directory)
        else:
            for part in missing:
                os.rename(staging / part, directory / part)
            descriptor = build_descriptor(directory, version=version, origin=origin, host=host,
                                          backends=wanted, installed_at=installed_at)
            write_descriptor(directory, descriptor)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return descriptor, missing
