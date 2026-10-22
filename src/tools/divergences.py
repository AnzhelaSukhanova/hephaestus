"""Render backend-comparison divergences from a faults report as Markdown.

This is a reading aid over ``faults.json``: it only consumes the serialized
report and never feeds back into the run. Crashes stay in ``faults.json`` only.

Usage: python -m src.tools.divergences <run_dir> [<run_dir> ...]
"""
from collections.abc import Mapping, Sequence
from difflib import unified_diff
import os
import sys
from typing import Any

from src.tools.outcomes import read_report


DIVERGENCE_KINDS = ('status_mismatch', 'diagnostic_mismatch')
DIVERGENCES_FILE = 'divergences.md'


def _divergent_findings(fault: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [finding for finding in fault.get('comparison', {}).get('findings', ())
            if finding['kind'] in DIVERGENCE_KINDS]


def _format(diagnostic: Mapping[str, Any]) -> str:
    location = diagnostic['source'] or '<compiler>'
    for key in ('line', 'column'):
        if diagnostic[key] is not None:
            location += ':' + str(diagnostic[key])
    return f"{location}: {diagnostic['message']}"


def _rejection_lines(finding: Mapping[str, Any]) -> dict[str, list[str]]:
    # Rejection messages are serialized as '<variant>: <location>: <message>'.
    return {variant_id: sorted(message[len(variant_id) + 2:]
                               for message in finding['diagnostics'][1:]
                               if message.startswith(variant_id + ': '))
            for variant_id in finding['variants']}


def _difference_lines(finding: Mapping[str, Any],
                      differences: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    return {variant_id: [line for difference in differences
                         for line in [_format(difference['diagnostic'])]
                         * difference['counts'][variant_id]]
            for variant_id in finding['variants']}


def _diff(lines_by_variant: Mapping[str, list[str]],
          groups: Mapping[str, Sequence[str]]) -> list[str]:
    grouped: list[tuple[list[str], list[str]]] = []
    for variant_id, lines in lines_by_variant.items():
        for members, group_lines in grouped:
            if group_lines == lines:
                members.append(variant_id)
                break
        else:
            grouped.append(([variant_id], lines))
    accepted = groups.get('accepted', ())
    base = next((group for group in grouped if group[0][0] in accepted), grouped[0])

    def label(members: list[str]) -> str:
        status = next((status for status, variants in groups.items()
                       if members[0] in variants), 'unknown')
        return f"{', '.join(members)} ({status})"

    diff = []
    for group in grouped:
        if group is not base:
            diff.extend(unified_diff(base[1], group[1], label(base[0]), label(group[0]),
                                     lineterm=''))
    return diff


def render_divergences(faults: Mapping[Any, Mapping[str, Any]]) -> str:
    divergent = [(pid, fault) for pid, fault in sorted(faults.items(),
                                                       key=lambda item: int(item[0]))
                 if _divergent_findings(fault)]
    out = ['# Backend divergences', '',
           f'{len(divergent)} divergent program(s); each is also in `faults.json` '
           'under the same id. Crashes are listed only in `faults.json`.', '']
    for pid, fault in divergent:
        comparison = fault['comparison']
        groups = comparison['groups']
        out += [f'## {pid}', '', f'Artifacts: `{pid}/`', '']
        out += [f"- {status}: {', '.join(members)}"
                for status, members in groups.items() if members]
        out.append('')
        for finding in _divergent_findings(fault):
            out += [f"### {finding['kind']}", '', '```text', *finding['diagnostics'], '```', '']
            if finding['kind'] == 'status_mismatch':
                lines = _rejection_lines(finding)
            else:
                lines = _difference_lines(finding, comparison['differences'])
            diff = _diff(lines, groups)
            if diff:
                out += ['```diff', *diff, '```', '']
    return '\n'.join(out) + '\n'


def write_divergences(run_dir: str,
                      faults: Mapping[Any, Mapping[str, Any]] | None = None) -> str:
    if faults is None:
        faults = read_report(os.path.join(run_dir, 'faults.json'))
    path = os.path.join(run_dir, DIVERGENCES_FILE)
    with open(path, 'w') as out:
        out.write(render_divergences(faults))
    return path


if __name__ == '__main__':
    for directory in sys.argv[1:]:
        print(write_divergences(directory))
