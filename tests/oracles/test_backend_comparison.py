from itertools import product

import pytest

from src.compilers.results import (
    CommandObservation, Diagnostic, ParsedDiagnostics, ProcessResult,
    VariantObservation,
)
from src.compilers.kotlin import BACKENDS, KotlinBackend, KotlinSettings
from src.oracles.backend_comparison import check_backend_compilation


ERROR = Diagnostic('program.kt', 1, 2, 'wrong type', 'TYPE_MISMATCH')


def observation(variant_id, accepted=False, errors=(ERROR,),
                backend: KotlinBackend = 'native', **kwargs):
    returncode = kwargs.pop('returncode', 0 if accepted else 1)
    launch_failure = kwargs.pop('launch_failure', None)
    parsed_errors = kwargs.pop('errors', () if accepted else errors)
    parsed = ParsedDiagnostics(
        parsed_errors, kwargs.pop('parsing_complete', True),
        kwargs.pop('unparsed', ()))
    filtered_errors = kwargs.pop('filtered_errors', parsed_errors)
    crash_message = kwargs.pop('crash_message', None)
    command = CommandObservation(
        ProcessResult(returncode, 'raw evidence', launch_failure), {}, parsed,
        filtered_errors, crash_message, ('compiler',), '/work', 1)
    return VariantObservation(variant_id, command,
                              settings=KotlinSettings(backend=backend))


@pytest.mark.parametrize('statuses', list(product((False, True), repeat=3)))
def test_symmetric_acceptance(statuses):
    results = {variant_id: observation(variant_id, accepted, backend=variant_id)
               for variant_id, accepted in zip(BACKENDS, statuses)}
    verdict = check_backend_compilation(results, BACKENDS)
    assert verdict.complete
    assert verdict.groups['crashed'] == ()
    assert bool(verdict.findings) == (len(set(statuses)) > 1)
    assert verdict.all_success == all(statuses)
    if len(set(statuses)) > 1:
        assert [finding.kind for finding in verdict.findings] == ['status_mismatch']


def test_diagnostic_order_multiplicity_and_meaning():
    other = Diagnostic('program.kt', 3, 4, 'another error')
    results = {variant_id: observation(variant_id, errors=(ERROR, other, ERROR),
                                       backend=variant_id)
               for variant_id in BACKENDS}
    results['js'] = observation('js', errors=(other, ERROR, ERROR), backend='js')
    assert not check_backend_compilation(results, BACKENDS).findings
    results['wasm'] = observation('wasm', errors=(ERROR, other), backend='wasm')
    verdict = check_backend_compilation(results, BACKENDS)
    assert verdict.differences[0].counts == {'js': 2, 'native': 2, 'wasm': 1}
    for changed in (Diagnostic('program.kt', 2, 2, 'wrong type', 'TYPE_MISMATCH'),
                    Diagnostic('program.kt', 1, 2, 'different type', 'TYPE_MISMATCH'),
                    Diagnostic('different.kt', 1, 2, 'wrong type', 'TYPE_MISMATCH')):
        results['wasm'] = observation('wasm', errors=(changed, other, ERROR), backend='wasm')
        assert check_backend_compilation(results, BACKENDS).findings


def test_status_mismatch_keeps_distinct_diagnostic_mismatches():
    results = {variant_id: observation(variant_id, accepted=variant_id == 'js',
                                       backend=variant_id)
               for variant_id in BACKENDS}
    results['wasm'] = observation('wasm', errors=(Diagnostic('program.kt', 3, 4,
                                                              'different error'),),
                                  backend='wasm')
    verdict = check_backend_compilation(results, BACKENDS)
    assert [finding.kind for finding in verdict.findings] == [
        'status_mismatch', 'diagnostic_mismatch']
    diagnostic_finding = verdict.findings[-1]
    assert any('different error' in message and 'program.kt:3:4' in message
               for message in diagnostic_finding.diagnostics)


@pytest.mark.parametrize('crashed', [('js',), BACKENDS])
def test_crashes_are_findings_not_rejection(crashed):
    results = {variant_id: observation(variant_id, backend=variant_id)
               for variant_id in BACKENDS}
    for variant_id in crashed:
        results[variant_id] = observation(variant_id, crash_message='recognized crash',
                                          parsing_complete=False, backend=variant_id)
    verdict = check_backend_compilation(results, BACKENDS)
    assert verdict.complete
    assert [finding.variants for finding in verdict.findings] == [(backend,) for backend in crashed]
    assert all(finding.kind == 'crash' for finding in verdict.findings)


@pytest.mark.parametrize('failure', ['missing', 'identity', 'launch', 'opaque',
                                   'partial', 'dependency_artifact', 'blocked'])
def test_incomplete_inputs_never_create_disagreements(failure):
    results = {variant_id: observation(variant_id, backend=variant_id)
               for variant_id in BACKENDS}
    if failure == 'missing':
        del results['wasm']
    elif failure == 'identity':
        results['wasm'] = observation('js')
    elif failure == 'launch':
        results['wasm'] = observation('wasm', launch_failure='not found', returncode=None,
                                      backend='wasm')
    elif failure == 'opaque':
        results['wasm'] = observation('wasm', errors=(), backend='wasm')
    elif failure == 'partial':
        results['wasm'] = observation('wasm', parsing_complete=False, backend='wasm')
    elif failure == 'dependency_artifact':
        results['wasm'] = VariantObservation('wasm', observation('wasm', True).command,
                                             dependency_artifact_valid=False,
                                             settings=KotlinSettings(backend='wasm'))
    else:
        results['wasm'] = VariantObservation(
            'wasm', None, incomplete_reason='dependency missing',
            settings=KotlinSettings(backend='wasm'))
    results['js'] = observation('js', crash_message='crash', backend='js')
    verdict = check_backend_compilation(results, BACKENDS)
    assert not verdict.complete
    assert [finding.kind for finding in verdict.findings] == ['crash']


def test_filters_cannot_change_acceptance_or_hide_unparsed_failures():
    results = {variant_id: observation(variant_id, filtered_errors=(), backend=variant_id)
               for variant_id in BACKENDS}
    assert not check_backend_compilation(results, BACKENDS).findings
    results['js'] = observation('js', True, backend='js')
    assert [finding.kind for finding in
            check_backend_compilation(results, BACKENDS).findings] == ['status_mismatch']
    results['wasm'] = observation('wasm', filtered_errors=(), parsing_complete=False,
                                  backend='wasm')
    assert not check_backend_compilation(results, BACKENDS).complete
