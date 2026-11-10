from dataclasses import asdict

from src.compilers.kotlin import BACKENDS
from src.compilers.results import Diagnostic
from src.oracles.backend_comparison import check_backend_compilation
from src.tools.divergences import render_divergences, write_divergences
from tests.oracles.test_backend_comparison import ERROR, observation


def fault(results):
    return {'comparison': asdict(check_backend_compilation(results, BACKENDS))}


def test_status_mismatch_is_rendered_with_rejection_diff():
    results = {variant_id: observation(variant_id, True, backend=variant_id)
               for variant_id in BACKENDS}
    clash = Diagnostic('program.kt', 2, 416, 'inline cycle')
    results['jvm'] = observation('jvm', errors=(clash,), backend='jvm')
    text = render_divergences({90: fault(results)})
    assert '## 90' in text and '### status_mismatch' in text
    assert '--- js, native, wasm (accepted)\n+++ jvm (rejected)' in text
    assert '\n+program.kt:2:416: inline cycle\n' in text


def test_diagnostic_mismatch_diff_and_crashes_are_excluded(tmp_path):
    results = {variant_id: observation(variant_id, backend=variant_id)
               for variant_id in BACKENDS}
    extra = Diagnostic('program.kt', 3, 4, 'extra error')
    results['wasm'] = observation('wasm', errors=(ERROR, extra), backend='wasm')
    crashed = {variant_id: observation(variant_id, backend=variant_id)
               for variant_id in BACKENDS}
    crashed['js'] = observation('js', crash_message='boom', backend='js')
    path = write_divergences(str(tmp_path), {'7': fault(results), '2': fault(crashed)})
    text = open(path).read()
    assert '1 divergent program(s)' in text
    assert '## 7' in text and '## 2' not in text and 'boom' not in text
    assert '--- js, native, jvm (rejected)\n+++ wasm (rejected)' in text
    assert '\n+program.kt:3:4: extra error\n' in text
