"""Fresh-process oracle checks with the real Kotlin diagnostic parser."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import traceback
from typing import assert_type

from tests.driver import contracts
from tests.driver.oracle_fixtures import (
    Batch, Commands, OracleDefect, defect_scenario, diagnostic, main_command,
)
from tests.driver.support import load_driver


def _truth(expected: bool, accepted: bool) -> None:
    driver = load_driver()
    batch = Batch()
    source = batch.add(1, expected)
    commands = Commands([(accepted, '' if accepted else diagnostic(source, 'rejected'))])
    driver.run_command = commands
    result = batch.check()
    faults, _, metrics, _ = result
    assert metrics == {}
    assert set(faults) == ({1} if expected != accepted else set())
    if faults:
        error = ('1:1: rejected' if expected
                 else 'SHOULD NOT BE COMPILED: injected type mismatch')
        assert faults[1]['error'] == error
        assert faults[1] is batch.oracles[1].stats
        assert faults[1]['programs'] == {source: expected}
        batch.preserved(1, expected)
    else:
        assert not (Path(driver.cli_args.test_directory) / '1').exists()
    assert not batch.directory.exists()
    assert not (Path(driver.cli_args.test_directory) / 'tmp' / '1').exists()
    commands.finished([main_command(batch)])


def correct_accepted() -> None:
    _truth(True, True)


def correct_rejected() -> None:
    _truth(True, False)


def incorrect_accepted() -> None:
    _truth(False, True)


def incorrect_rejected() -> None:
    _truth(False, False)


def mixed_batch() -> None:
    driver = load_driver()
    batch = Batch()
    batch.add(1, True)
    bad1 = batch.add(1, False)
    good2 = batch.add(2, True)
    batch.add(3, False)
    batch.add(4, True)
    good5 = batch.add(5, True)
    bad5 = batch.add(5, False)
    output = '\n'.join([
        diagnostic(bad1, 'expected rejection'),
        diagnostic(good2, 'first diagnostic', 2, 3),
        diagnostic(good5, 'another program', 9, 2),
        diagnostic(good2, 'second diagnostic', 7, 5),
        diagnostic(bad5, 'expected rejection too'),
    ])
    commands = Commands([(False, output)])
    driver.run_command = commands
    faults, _, metrics, publications = batch.check()
    assert set(faults) == {2, 3, 5}
    assert faults[2]['error'] == '2:3: first diagnostic\n7:5: second diagnostic'
    assert faults[3]['error'] == 'SHOULD NOT BE COMPILED: injected type mismatch'
    assert faults[5]['error'] == '9:2: another program'
    assert metrics == publications == {}
    for pid, expected in ((2, True), (3, False), (5, True), (5, False)):
        batch.preserved(pid, expected)
    for pid in batch.oracles:
        assert not (Path(driver.cli_args.test_directory) / 'tmp' / str(pid)).exists()
        assert (Path(driver.cli_args.test_directory) / str(pid)).exists() == (pid in faults)
    assert not batch.directory.exists()
    commands.finished([main_command(batch)])


def filtered_diagnostics() -> None:
    driver = load_driver()
    batch = Batch()
    good = batch.add(1, True)
    bad = batch.add(2, False)
    retained = batch.add(3, True)
    patterns = Path('filters.txt')
    patterns.write_text(r'(?m)^.*error: ignored diagnostic$' + '\n')
    driver.cli_args.error_filter_patterns = str(patterns)
    output = '\n'.join([
        diagnostic(good, 'ignored diagnostic'),
        diagnostic(bad, 'ignored diagnostic'),
        diagnostic(retained, 'retained diagnostic', 3, 4),
    ])
    commands = Commands([(False, output)])
    driver.run_command = commands
    faults, _, _, _ = batch.check()
    assert set(faults) == {2, 3}
    assert faults[2]['error'] == 'SHOULD NOT BE COMPILED: injected type mismatch'
    assert faults[3]['error'] == '3:4: retained diagnostic'
    assert batch.oracles[1].stats['error'] == 'injected type mismatch'
    assert not batch.directory.exists()
    commands.finished([main_command(batch)])


def compiler_crash() -> None:
    driver = load_driver()
    batch = Batch()
    batch.add(1, True)
    batch.add(2, False)
    output = 'org.jetbrains.kotlin.CompilerException\ncontrolled compiler crash'
    commands = Commands([(False, output)])
    driver.run_command = commands
    captured = StringIO()
    with redirect_stdout(captured):
        faults, _, metrics, _ = batch.check()
    assert set(faults) == {1, 2}
    for pid, expected in ((1, True), (2, False)):
        assert faults[pid]['error'] == output
        batch.preserved(pid, expected)
    assert metrics == {}
    assert ('We found compiler crash' in captured.getvalue()) == driver.cli_args.debug
    assert not batch.directory.exists()
    commands.finished([main_command(batch)])


def debug_exit() -> None:
    driver = load_driver()
    assert driver.cli_args.debug
    batch = Batch()
    source = batch.add(1, True)
    commands = Commands([(False, diagnostic(source, 'debug rejection'))])
    driver.run_command = commands
    captured = StringIO()
    with redirect_stdout(captured):
        try:
            batch.check()
        except SystemExit as error:
            assert error.code == 1
        else:
            raise AssertionError('Correct-program mismatch must exit in debug mode')
    assert 'Mismatch found in program 1. Expected to compile' in captured.getvalue()
    assert '1:1: debug rejection' in captured.getvalue()
    batch.preserved(1, True)
    commands.finished([main_command(batch)])


def debug_incorrect_acceptance() -> None:
    driver = load_driver()
    assert driver.cli_args.debug
    captured = StringIO()
    with redirect_stdout(captured):
        _truth(False, True)
    assert 'Mismatch found in program 1. Expected to fail' in captured.getvalue()


def _enrichment(profile: str, phase: str) -> None:
    from hephaestus import TimeMetrics
    from src.compilers.kotlin import KotlinCompiler

    driver = load_driver()
    batch = Batch()
    source = batch.add(1, True)
    batch.add(2, False)
    batch.add(3, True)
    replies = [(False, diagnostic(source, 'enrich this'))]
    expected = [main_command(batch)]
    if not driver.cli_args.disable_metrics:
        replies.append((False, profile))
        expected.append(KotlinCompiler(source).get_error_enrichment_cmds(source)['xprofile-phases'])
    commands = Commands(replies)
    driver.run_command = commands
    faults, elapsed, metrics, _ = batch.check()
    assert_type(elapsed, float)
    assert_type(metrics, TimeMetrics)
    assert set(faults) == {1, 2}
    assert faults[1]['error'] == '1:1: enrich this'
    if driver.cli_args.disable_metrics:
        assert 'error_phase' not in faults[1]
    else:
        assert faults[1]['error_phase'] == phase
    assert 'error_phase' not in faults[2]
    assert 'time_metrics' not in batch.oracles[3].stats
    if driver.cli_args.time_metrics:
        assert set(metrics) == {1, 2, 3}
        for pid, timing in metrics.items():
            assert timing['generation_cpu'] == 0.125
            assert timing['compilation_with_ir_dumps'] == elapsed
            if pid != 1 or driver.cli_args.disable_metrics:
                assert timing['phase_profiling'] == 0
        for pid, stats in faults.items():
            assert stats['time_metrics'] == metrics[pid]
    else:
        assert metrics == {}
        assert all('time_metrics' not in stats for stats in faults.values())
    commands.finished(expected)


def enrichment_backend() -> None:
    _enrichment('FunctionInlining: 3 msec\n', 'backend')


def enrichment_frontend() -> None:
    _enrichment('unrecognized frontend diagnostic\n', 'frontend')


def unknown_output() -> None:
    driver = load_driver()
    batch = Batch()
    batch.add(1, True)
    batch.add(2, False)
    commands = Commands([(True, 'compiler informational output without diagnostics')])
    driver.run_command = commands
    faults, _, _, _ = batch.check()
    assert set(faults) == {2}
    assert faults[2]['error'] == 'SHOULD NOT BE COMPILED: injected type mismatch'
    assert not batch.directory.exists()
    commands.finished([main_command(batch)])


def _rerun(expected: bool, boundary: str) -> None:
    from src.compilers.kotlin import KotlinCompiler
    from src.ir import ast
    from tests.driver.fixtures import minimal_program, replay

    driver = load_driver()
    batch = Batch()
    source = batch.add(1, expected)
    temporary = Path(driver.cli_args.test_directory) / 'tmp' / '1'
    # Both report destinations exist in a real transformed-program run.
    final = temporary / 'program.kt'
    final.write_text('final program\n')
    replay(temporary / 'program.kt.bin')
    original_source = final.read_bytes()
    original_pickle = (temporary / 'program.kt.bin').read_bytes()
    transformations: dict[int, Path] = {}
    for tid in range(1, 4):
        path = Path(driver.get_transformations_dir(1, tid)) / 'program.kt'
        path.parent.mkdir(parents=True)
        path.write_text(f'transformation {tid}\n')
        program = minimal_program()
        integer = program.bt_factory.get_integer_type()
        program.add_declaration(ast.VariableDeclaration(
            f'transformation{tid}', ast.IntegerConstant(tid, integer), var_type=integer))
        driver.utils.dump_program(str(path) + '.bin', program)
        transformations[tid] = path
    if boundary == 'previous':
        assert driver.cli_args.rerun
        assert driver.cli_args.transformations == 3
        replies = [(not expected, diagnostic(source, 'rerun mismatch') if expected else ''),
                   (not expected, ''), (expected, '')]
        tids = [3, 2]
    elif boundary == 'latest':
        replies = [(expected, '')]
        tids = [3]
    elif boundary == 'none':
        replies = [(not expected, '')] * 3
        tids = [3, 2, 1]
    else:
        assert boundary == 'zero'
        replies = []
        tids = []
    commands = Commands(replies)
    driver.run_command = commands
    expected_commands = [KotlinCompiler(str(transformations[tid])).get_compiler_cmd()
                         for tid in tids]
    if boundary == 'previous':
        faults, _, _, _ = batch.check()
        assert set(faults) == {1}
        expected_commands.insert(0, main_command(batch))
        destination = Path(driver.cli_args.test_directory) / '1'
        assert not batch.directory.exists()
    else:
        driver._report_failed(1, 0 if boundary == 'zero' else 3,
                              KotlinCompiler(source), expected)
        destination = temporary
    initial = destination / 'initial_program.kt'
    current = destination / 'program.kt'
    if boundary in ('previous', 'latest'):
        tid = 2 if boundary == 'previous' else 3
        assert initial.read_bytes() == transformations[tid].read_bytes()
        assert Path(str(initial) + '.bin').read_bytes() == Path(str(transformations[tid]) + '.bin').read_bytes()
        contracts.program(driver.utils.load_program(str(initial) + '.bin'))
    else:
        assert not initial.exists()
        assert not Path(str(initial) + '.bin').exists()
    if boundary == 'previous':
        assert current.read_bytes() == transformations[3].read_bytes()
        assert Path(str(current) + '.bin').read_bytes() == Path(str(transformations[3]) + '.bin').read_bytes()
    else:
        assert current.read_bytes() == original_source
        assert Path(str(current) + '.bin').read_bytes() == original_pickle
    contracts.program(driver.utils.load_program(str(current) + '.bin'))
    commands.finished(expected_commands)


def rerun_correct() -> None:
    _rerun(True, 'previous')


def rerun_incorrect() -> None:
    _rerun(False, 'previous')


def report_latest_correct() -> None:
    _rerun(True, 'latest')


def report_latest_incorrect() -> None:
    _rerun(False, 'latest')


def report_no_match_correct() -> None:
    _rerun(True, 'none')


def report_no_match_incorrect() -> None:
    _rerun(False, 'none')


def report_zero() -> None:
    _rerun(True, 'zero')


def _generation_failed(crash: bool) -> None:
    driver = load_driver()
    batch = Batch()
    batch.add(2, True)
    failed = driver.ProgramRes(True, {
        'transformations': ['TypeErasure'], 'error': 'controlled generation failure',
        'program': None, 'time': 0,
    })
    contracts.program_result(failed)
    batch.oracles[1] = failed
    crash_output = 'org.jetbrains.kotlin.CompilerException\ncontrolled compiler crash'
    commands = Commands([(not crash, crash_output if crash else '')])
    driver.run_command = commands
    faults, _, _, _ = batch.check()
    commands.finished([main_command(batch)])
    assert not batch.directory.exists()
    assert failed.stats['error'] == 'controlled generation failure'
    expected_others = {2} if crash else set()
    assert set(faults) - {1} == expected_others
    if crash:
        assert faults[2]['error'] == crash_output
    if 1 not in faults:
        raise OracleDefect('generation-failed record omitted' + (' during crash' if crash else ''))
    assert faults[1] == failed.stats


def generation_failed() -> None:
    defect_scenario(lambda: _generation_failed(False))


def generation_failed_crash() -> None:
    defect_scenario(lambda: _generation_failed(True))


def _unknown_nonzero_output() -> None:
    driver = load_driver()
    batch = Batch()
    batch.add(1, True)
    message = 'controlled compiler failure without a recognized diagnostic'
    commands = Commands([(False, message)])
    driver.run_command = commands
    faults, _, metrics, publications = batch.check()
    commands.finished([main_command(batch)])
    assert metrics == {} and publications == {}
    if not faults:
        raise OracleDefect('nonzero compiler failure treated as acceptance')
    assert set(faults) == {1}
    assert message in faults[1]['error']


def unknown_nonzero_output() -> None:
    defect_scenario(_unknown_nonzero_output)


def _missing_injected_error() -> None:
    driver = load_driver()
    batch = Batch()
    batch.add(1, False, error=None)
    commands = Commands([(True, '')])
    driver.run_command = commands
    try:
        faults, _, _, _ = batch.check()
    except TypeError as error:
        if str(error) != 'can only concatenate str (not "NoneType") to str':
            raise
        frames = traceback.extract_tb(error.__traceback__)
        assert frames[-1].name == 'check_oracle'
        assert Path(frames[-1].filename).name == 'hephaestus.py'
        assert batch.oracles[1].stats['error'] is None
        commands.finished([main_command(batch)])
        raise OracleDefect('None injected error concatenated') from error
    commands.finished([main_command(batch)])
    assert set(faults) == {1}
    assert isinstance(faults[1]['error'], str)
    assert faults[1]['error'].startswith('SHOULD NOT BE COMPILED:')
    batch.preserved(1, False)


def missing_injected_error() -> None:
    defect_scenario(_missing_injected_error)


class OracleProbeFailure(RuntimeError):
    pass


def _wrapper_exception() -> None:
    from hephaestus import OracleResult

    driver = load_driver()
    batch = Batch()
    batch.add(1, True)
    failure = OracleProbeFailure('controlled oracle exception')
    calls = 0

    def fail(arguments: list[str], get_stdout: bool = True,
             cwd: str | None = None) -> tuple[bool, str]:
        nonlocal calls
        assert arguments == main_command(batch)
        assert get_stdout and cwd is None
        calls += 1
        raise failure

    driver.run_command = fail
    captured = StringIO()
    with redirect_stdout(captured):
        try:
            result = driver.check_oracle_mul(str(batch.directory), batch.oracles)
        except OracleProbeFailure as error:
            assert error is failure
            assert calls == 1
            return
    assert_type(result, OracleResult)
    contracts.oracle_result(result)
    assert calls == 1
    assert result == ({}, 0, {}, {})
    assert not driver.STOP_COND
    assert 'Internal error while checking the oracle' in captured.getvalue()
    assert 'controlled oracle exception' in captured.getvalue()
    assert ('Traceback (most recent call last):' in captured.getvalue()) == driver.cli_args.print_stacktrace
    raise OracleDefect('oracle exception swallowed by wrapper')


def wrapper_exception() -> None:
    defect_scenario(_wrapper_exception)