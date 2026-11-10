import pytest

from src.modules.models import OracleResult
from tests.driver import contracts


@pytest.mark.parametrize('value', [True, -1, float('inf'), float('nan'), '0', None])
def test_invalid_duration(value: object) -> None:
    with pytest.raises(AssertionError):
        contracts.duration(value)


@pytest.mark.parametrize('value', [0, 0.0, 1.25])
def test_valid_duration(value: object) -> None:
    contracts.duration(value)


@pytest.mark.parametrize('value', [None, {}, (0, {}, None, None),
                                  (False, {'time': 0}, None, None)])
def test_invalid_program_result(value: object) -> None:
    with pytest.raises(AssertionError):
        contracts.program_result(value)


@pytest.mark.parametrize('value', [({}, 0, {}, {}),
                                  OracleResult({True: {'error': 'bad'}}),
                                  OracleResult({1: {'error': None}}),
                                  OracleResult(compilation_time=-1),
                                  OracleResult(time_metrics={'1': {}}),
                                  OracleResult(inconclusive={'1': {}})])
def test_invalid_oracle_result(value: object) -> None:
    with pytest.raises(AssertionError):
        contracts.oracle_result(value)


@pytest.mark.parametrize('value', [{1: 'bad'}, {'time': float('nan')}, {'tuple': (1, 2)}])
def test_invalid_json(value: object) -> None:
    with pytest.raises(AssertionError):
        contracts.json_value(value)


def test_valid_payloads() -> None:
    contracts.program_result((False, {'programs': {'p.kt': True}, 'time': 0,
                                     'error': None, 'transformations': []}, None, ()))
    contracts.oracle_result(OracleResult({1: {'error': 'rejected'}}, 0.5,
                                         {1: {'compile': 0}}, {},
                                         {2: {'reason': 'unavailable'}}))
    contracts.json_value({'1': [None, True, 'text', 0.5]})


@pytest.mark.parametrize('value', [{}, {'direct': True, 'closure': []},
                                  {'direct': 1, 'closure': ['1']},
                                  {'direct': None, 'closure': [], 'manifest_diff': {}}])
def test_invalid_publication(value: object) -> None:
    with pytest.raises(AssertionError):
        contracts.oracle_result(OracleResult(publications={1: value}))


@pytest.mark.parametrize('value', [{}, {'pid': True, 'context': None, 'closure': []},
                                  {'pid': 1, 'context': 'not a Context', 'closure': []}])
def test_invalid_dependency(value: object) -> None:
    with pytest.raises(AssertionError):
        contracts.dependency(value)