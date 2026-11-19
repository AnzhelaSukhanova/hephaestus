from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from src.oracles.compilation import OracleFinding, check_compilation


@pytest.mark.parametrize('expected,diagnostics,findings', [
    (True, {}, ()),
    (False, {'Main.kt': ['expected error']}, ()),
    (True, {'Main.kt': ['first', 'second']},
     (OracleFinding('unexpected_rejection', 'Main.kt', ('first', 'second')),)),
    (False, {}, (OracleFinding('unexpected_acceptance', 'Main.kt', ()),)),
    (True, {'unrelated.kt': ['error']}, ()),
])
def test_compilation_expectations(expected: bool, diagnostics: dict,
                                  findings: tuple) -> None:
    assert check_compilation({'Main.kt': expected}, diagnostics, None) == findings


def test_crash_takes_precedence_over_program_findings() -> None:
    assert check_compilation({'Main.kt': False}, {}, 'compiler crash') == (
        OracleFinding('crash', None, ('compiler crash',)),)


def test_finding_order_and_inputs_are_preserved() -> None:
    expectations = {'correct.kt': True, 'incorrect.kt': False}
    diagnostics = {'correct.kt': ['first error', 'second error']}
    before = deepcopy((expectations, diagnostics))
    findings = check_compilation(expectations, diagnostics, None)
    assert findings == (
        OracleFinding('unexpected_rejection', 'correct.kt',
                      ('first error', 'second error')),
        OracleFinding('unexpected_acceptance', 'incorrect.kt', ()),
    )
    assert (expectations, diagnostics) == before
    with pytest.raises(FrozenInstanceError):
        setattr(findings[0], 'program', 'changed.kt')