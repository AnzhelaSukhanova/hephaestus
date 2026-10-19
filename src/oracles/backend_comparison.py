from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

from src.compilers.results import Diagnostic, VariantObservation
from src.oracles.models import ComparisonResult, DiagnosticDifference, OracleFinding


def _usability_reason(variant_id: str,
                      variant: VariantObservation | None) -> str | None:
    if variant is None:
        return 'missing observation'
    command = variant.command
    reason = variant.incomplete_reason
    if variant.variant_id != variant_id:
        return 'Mismatched or duplicate variant identity: ' + variant.variant_id
    if command is None:
        return reason or 'Compilation was blocked'
    if command.crash_message:
        return reason
    if command.launch_failure:
        return 'Compiler launch failed: ' + command.launch_failure
    if command.returncode is None:
        return 'No process return code'
    if not command.parsing_complete:
        return 'Unparsed compiler output'
    if command.status != (command.returncode == 0):
        return 'Inconsistent process status'
    if command.succeeded() and not variant.dependency_artifact_valid:
        return 'Missing or unusable output KLIB'
    if not command.status and not command.errors:
        return 'Failure without recognizable error diagnostics'
    return reason


def _group(
        observations: Mapping[str, VariantObservation], variant_ids: Sequence[str]
) -> tuple[list[OracleFinding], list[str], dict[str, tuple[str, ...]],
           dict[str, Counter[Diagnostic]]]:
    findings = []
    reasons = []
    groups = defaultdict(list)
    diagnostics: dict[str, Counter[Diagnostic]] = {}
    extra = sorted(observations.keys() - set(variant_ids))
    if extra:
        reasons.append('Unexpected variant observations: ' + ', '.join(extra))
    for variant_id in variant_ids:
        variant = observations.get(variant_id)
        command = variant.command if variant is not None else None
        if command and command.crash_message:
            findings.append(OracleFinding('crash', None, (command.crash_message,), (variant_id,)))
            groups['crashed'].append(variant_id)
        reason = _usability_reason(variant_id, variant)
        if command and command.crash_message:
            if reason:
                reasons.append(variant_id + ': ' + reason)
            continue
        if reason:
            reasons.append(variant_id + ': ' + reason)
            groups['unavailable'].append(variant_id)
            continue
        groups['accepted' if command.status else 'rejected'].append(variant_id)
        diagnostics[variant_id] = Counter(command.filtered_errors)
    _ = groups['crashed']
    result_groups = {key: tuple(values) for key, values in sorted(groups.items())}
    return findings, reasons, result_groups, diagnostics


def _diagnostic_differences(
        diagnostics: Mapping[str, Counter[Diagnostic]],
        groups: Mapping[str, Sequence[str]], variant_ids: Sequence[str]
) -> tuple[tuple[tuple[str, ...], ...], tuple[DiagnosticDifference, ...]]:
    diagnostic_groups: list[list[str]] = []
    for variant_id in variant_ids:
        for group in diagnostic_groups:
            if diagnostics[group[0]] == diagnostics[variant_id]:
                group.append(variant_id)
                break
        else:
            diagnostic_groups.append([variant_id])
    differences = []
    all_errors = set().union(*(set(counter) for counter in diagnostics.values()))
    for diagnostic in sorted(all_errors, key=repr):
        counts = {variant_id: diagnostics[variant_id][diagnostic]
                  for variant_id in variant_ids}
        differs_within_status = any(
            len({diagnostics[variant_id][diagnostic]
                 for variant_id in status_group}) > 1
            for status in ('accepted', 'rejected')
            if (status_group := groups.get(status, ()))
        )
        if differs_within_status:
            differences.append(DiagnosticDifference(diagnostic, counts))
    return tuple(tuple(group) for group in diagnostic_groups), tuple(differences)


def _diagnostic_message(difference: DiagnosticDifference) -> str:
    diagnostic = difference.diagnostic
    location = diagnostic.source or '<compiler>'
    if diagnostic.line is not None:
        location += ':' + str(diagnostic.line)
    if diagnostic.column is not None:
        location += ':' + str(diagnostic.column)
    counts = ', '.join(f'{variant_id}={count}'
                       for variant_id, count in sorted(difference.counts.items()))
    return f'{location}: {diagnostic.message} ({counts})'


def check_backend_compilation(
        observations: Mapping[str, VariantObservation],
        variant_ids: Sequence[str]) -> ComparisonResult:
    findings, reasons, result_groups, diagnostics = _group(observations, variant_ids)
    if reasons or result_groups['crashed']:
        return ComparisonResult(tuple(findings), result_groups,
                                incomplete_reasons=tuple(reasons))
    if result_groups.get('accepted') and result_groups.get('rejected'):
        findings.append(OracleFinding('status_mismatch', None,
            ('Backend acceptance differs: ' + repr(result_groups),), tuple(variant_ids)))
    diagnostic_groups, differences = _diagnostic_differences(
        diagnostics, result_groups, variant_ids)
    if differences:
        findings.append(OracleFinding('diagnostic_mismatch', None,
            tuple(_diagnostic_message(difference) for difference in differences),
            tuple(variant_ids)))
    return ComparisonResult(tuple(findings), result_groups,
                            diagnostic_groups, differences)
