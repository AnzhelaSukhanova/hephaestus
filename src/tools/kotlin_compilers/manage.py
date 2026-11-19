#!/usr/bin/env python3
"""Kotlin compiler manager.

  manage.py [--versions FILE] [--host HOST] sync [ID ...]
  manage.py [--versions FILE] list [ID ...]
  manage.py [--versions FILE] check [ID ...]

IDs are selectors: ``<base>[-jvm|-js|-wasm|-native]`` with base ``prerelease``,
``master`` or an exact version. See README.md for the directory contract.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.tools.kotlin_compilers.check import check  # noqa: E402
from src.tools.kotlin_compilers.dev import MASTER_ENV, DevError, master_version  # noqa: E402
from src.tools.kotlin_compilers.host import NATIVE_HOSTS, HostError, resolve_host  # noqa: E402
from src.tools.kotlin_compilers.layout import Layout  # noqa: E402
from src.tools.kotlin_compilers.sync import list_lines, sync  # noqa: E402
from src.tools.kotlin_compilers.versions import VersionsError, load_versions, parse_selector  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='manage.py', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--versions', metavar='FILE',
                        help='version list (default: $KOTLIN_COMPILERS_VERSIONS or '
                             'versions.json in the repository root)')
    parser.add_argument('--host', choices=NATIVE_HOSTS, help='override host detection')
    parser.add_argument('--home', metavar='DIR',
                        help='install root (default: $KOTLIN_COMPILERS_HOME or ~/kotlin-compilers)')
    commands = parser.add_subparsers(dest='command', required=True)
    sync_parser = commands.add_parser('sync', help='install declared (or given) compilers')
    sync_parser.add_argument('ids', nargs='*', metavar='ID')
    list_parser = commands.add_parser('list', help='show declared and installed compilers (offline)')
    list_parser.add_argument('ids', nargs='*', metavar='ID')
    check_parser = commands.add_parser(
        'check', help='compile a fixture with each installed compiler x backend')
    check_parser.add_argument('ids', nargs='*', metavar='ID')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        version_list = load_versions(args.versions)
        for text in args.ids:
            parse_selector(text)
        host = resolve_host(args.host)
        master = master_version(os.environ.get(MASTER_ENV))
    except (VersionsError, HostError, DevError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    layout = Layout(args.home)
    if args.command == 'sync':
        return sync(layout, version_list, host, args.ids, master)
    if args.command == 'check':
        code, lines = check(layout, version_list, args.ids)
        print('\n'.join(lines))
        return code
    print('\n'.join(list_lines(layout, version_list, host, args.ids)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
