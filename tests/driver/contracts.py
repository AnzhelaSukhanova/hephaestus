from collections.abc import Mapping, Sequence
import math
from typing import TypeGuard

from src.ir import ast
from src.ir.context import Context


def is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, (list, tuple))


def record(value: object) -> Mapping[object, object]:
    assert is_mapping(value), repr(value)
    return value


def duration(value: object) -> None:
    assert type(value) in (int, float), repr(value)
    assert isinstance(value, (int, float))
    assert math.isfinite(value) and value >= 0, repr(value)


def program(value: object) -> ast.Program:
    assert isinstance(value, ast.Program)
    assert isinstance(value.context, Context)
    return value


def json_value(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if isinstance(value, float):
        assert math.isfinite(value)
    elif is_mapping(value):
        for key, item in value.items():
            assert type(key) is str
            json_value(item)
    else:
        assert isinstance(value, list)
        assert is_sequence(value)
        for item in value:
            json_value(item)


def program_result(value: object) -> None:
    assert is_sequence(value) and len(value) == 4
    failed, stats, dependency, closure = value
    assert type(failed) is bool
    fields = record(stats)
    assert {'time', 'error', 'transformations'} <= fields.keys()
    duration(fields['time'])
    assert fields['error'] is None or isinstance(fields['error'], str)
    assert is_sequence(fields['transformations'])
    assert all(isinstance(name, str) for name in fields['transformations'])
    if failed:
        assert fields['program'] is None
        assert isinstance(fields['error'], str)
    else:
        for name, expected in record(fields['programs']).items():
            assert isinstance(name, str)
            assert type(expected) is bool
    assert dependency is None or type(dependency) is int
    assert closure is None or (is_sequence(closure) and
                               all(isinstance(path, str) for path in closure))


def oracle_result(value: object) -> None:
    assert is_sequence(value) and len(value) == 4
    faults, elapsed, metrics, publications = value
    duration(elapsed)
    for pid, stats in record(faults).items():
        assert type(pid) is int
        assert isinstance(record(stats)['error'], str)
    for pid, timings in record(metrics).items():
        assert type(pid) is int
        for name, timing in record(timings).items():
            assert isinstance(name, str)
            duration(timing)
    for pid, publication in record(publications).items():
        assert type(pid) is int
        ledger_record(publication)


def ledger_record(value: object) -> None:
    fields = record(value)
    assert {'direct', 'closure'} <= fields.keys()
    assert fields['direct'] is None or type(fields['direct']) is int
    assert is_sequence(fields['closure'])
    assert all(type(pid) is int for pid in fields['closure'])
    if 'manifest_diff' in fields:
        diff = record(fields['manifest_diff'])
        assert {'manifest_depends', 'closure', 'unexpected'} <= diff.keys()
        for paths in diff.values():
            assert is_sequence(paths) and all(type(path) is str for path in paths)


def dependency(value: object) -> None:
    fields = record(value)
    assert {'pid', 'context', 'closure', 'klibs'} <= fields.keys()
    assert type(fields['pid']) is int
    assert isinstance(fields['context'], Context)
    assert is_sequence(fields['closure'])
    assert all(type(pid) is int for pid in fields['closure'])
    assert is_sequence(fields['klibs'])
    assert all(type(path) is str for path in fields['klibs'])