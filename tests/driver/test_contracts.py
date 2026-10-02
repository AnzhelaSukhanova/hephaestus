import pytest

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


@pytest.mark.parametrize('value', [({True: {'error': 'bad'}}, 0, {}, {}),
                                  ({1: {'error': None}}, 0, {}, {}),
                                  ({}, -1, {}, {}), ({}, 0, {'1': {}}, {})])
def test_invalid_oracle_result(value: object) -> None:
    with pytest.raises(AssertionError):
        contracts.oracle_result(value)


@pytest.mark.parametrize('value', [{1: 'bad'}, {'time': float('nan')}, {'tuple': (1, 2)}])
def test_invalid_json(value: object) -> None:
    with pytest.raises(AssertionError):
        contracts.json_value(value)


def test_valid_payloads() -> None:
    contracts.program_result((False, {'programs': {'p.kt': True}, 'time': 0,
                                     'error': None, 'transformations': []}, None, None))
    contracts.oracle_result(({1: {'error': 'rejected'}}, 0.5,
                             {1: {'compile': 0}}, {}))
    contracts.json_value({'1': [None, True, 'text', 0.5]})


@pytest.mark.parametrize('value', [{}, {'direct': True, 'closure': []},
                                  {'direct': 1, 'closure': ['1']},
                                  {'direct': None, 'closure': [], 'manifest_diff': {}}])
def test_invalid_publication(value: object) -> None:
    with pytest.raises(AssertionError):
        contracts.oracle_result(({}, 0, {}, {1: value}))


@pytest.mark.parametrize('value', [{}, {'pid': True, 'context': None, 'closure': [], 'klibs': []},
                                  {'pid': 1, 'context': 'not a Context', 'closure': [], 'klibs': []}])
def test_invalid_dependency(value: object) -> None:
    with pytest.raises(AssertionError):
        contracts.dependency(value)