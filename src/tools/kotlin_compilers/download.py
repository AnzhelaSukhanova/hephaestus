"""Unauthenticated downloads with sidecar checksum verification.

Every request goes through :func:`open_url`, so tests can replace the network
with canned responses. No credentials are ever sent.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
from pathlib import Path
import sys
import time
from typing import BinaryIO
import urllib.error
import urllib.request


USER_AGENT = 'hephaestus-kotlin-compilers'
TIMEOUT_SECONDS = 60
RETRIES = 3
RETRY_DELAY_SECONDS = 2.0
CHUNK = 1 << 20
# Client errors worth another attempt: request timeout and rate limiting.
RETRIED_CLIENT_ERRORS = (408, 429)


class DownloadError(RuntimeError):
    pass


class NotFound(DownloadError):
    pass


class ChecksumError(DownloadError):
    pass


@dataclass(frozen=True)
class Checksum:
    algorithm: str
    digest: str
    source: str


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def open_url(url: str, method: str = 'GET') -> BinaryIO:
    request = urllib.request.Request(url, method=method, headers={'User-Agent': USER_AGENT})
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)


def _with_retries(url: str, method: str) -> BinaryIO:
    for attempt in range(1, RETRIES + 1):
        try:
            return open_url(url, method)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise NotFound(f'{url}: HTTP {exc.code}') from exc
            if (exc.code < 500 and exc.code not in RETRIED_CLIENT_ERRORS) or attempt == RETRIES:
                raise DownloadError(f'{url}: HTTP {exc.code}') from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            if attempt == RETRIES:
                raise DownloadError(f'{url}: {exc}') from exc
        time.sleep(RETRY_DELAY_SECONDS * attempt)
    raise DownloadError(f'{url}: no attempts made')


def exists(url: str) -> bool:
    try:
        _with_retries(url, 'HEAD').close()
    except NotFound:
        return False
    return True


def read_text(url: str) -> str | None:
    """The body as text, or ``None`` if the server says it doesn't exist."""
    try:
        with _with_retries(url, 'GET') as response:
            return response.read().decode('utf-8', 'replace')
    except NotFound:
        return None
    except (OSError, http.client.HTTPException) as exc:
        raise DownloadError(f'{url}: {exc}') from exc


def _first_digest(text: str | None, length: int) -> str | None:
    if text is None:
        return None
    token = text.strip().split()[0].lower() if text.strip() else ''
    return token if len(token) == length and all(c in '0123456789abcdef' for c in token) else None


def github_checksum(asset_url: str, version: str) -> Checksum | None:
    """``<asset>.sha256`` next to the asset, else the release's ``maven-<v>-sha256.txt``."""
    digest = _first_digest(read_text(asset_url + '.sha256'), 64)
    if digest:
        return Checksum('sha256', digest, asset_url + '.sha256')
    base, _, name = asset_url.rpartition('/')
    listing_url = f'{base}/maven-{version}-sha256.txt'
    listing = read_text(listing_url)
    for line in (listing or '').splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].rpartition('/')[2] == name:
            return Checksum('sha256', parts[1].lower(), listing_url)
    return None


# Maven sidecars, strongest first. Maven Central always serves .sha1 and .md5,
# .sha256 only for some artifacts; the dev repo serves all three.
SIDECARS = (('sha256', 64), ('sha1', 40), ('md5', 32))


def published_digests(url: str) -> list[Checksum]:
    """Every sidecar digest published for ``url``, strongest first; the file isn't fetched.

    Only a missing sidecar counts as unpublished; any other failure raises
    :class:`DownloadError` rather than downgrading to an unverified download.
    """
    found = []
    for algorithm, length in SIDECARS:
        digest = _first_digest(read_text(f'{url}.{algorithm}'), length)
        if digest:
            found.append(Checksum(algorithm, digest, f'{url}.{algorithm}'))
    return found


def maven_checksum(url: str) -> Checksum | None:
    """A Maven file's strongest sidecar: ``.sha256``, else ``.sha1``, else ``.md5``."""
    digests = published_digests(url)
    return digests[0] if digests else None


def fetch(url: str, destination: Path, checksum: Checksum | None) -> Path:
    """Stream ``url`` into ``destination``; on a checksum mismatch nothing is kept."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + '.part')
    digest = hashlib.new(checksum.algorithm if checksum else 'sha256')
    log(f'  downloading {url}')
    try:
        with _with_retries(url, 'GET') as response, open(partial, 'wb') as output:
            while chunk := response.read(CHUNK):
                digest.update(chunk)
                output.write(chunk)
    except (OSError, http.client.HTTPException, DownloadError) as exc:
        partial.unlink(missing_ok=True)
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError(f'{url}: {exc}') from exc
    if checksum is None:
        log(f'  warning: no checksum published for {url}; installing unverified')
    elif digest.hexdigest() != checksum.digest:
        partial.unlink()
        raise ChecksumError(f'{checksum.algorithm} mismatch for {url}: expected '
                            f'{checksum.digest} ({checksum.source}), got {digest.hexdigest()}')
    partial.rename(destination)
    return destination
