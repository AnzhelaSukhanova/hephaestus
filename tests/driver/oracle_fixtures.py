"""Typed inputs and controlled compiler responses for oracle scenarios."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, assert_type

from tests.driver import contracts
from tests.driver.support import assert_arguments, load_driver

if TYPE_CHECKING:
    from hephaestus import OracleResult, ProgramRes


class OracleDefect(AssertionError):
    pass


def defect_scenario(scenario: Callable[[], None]) -> None:
    try:
        scenario()
    except OracleDefect as error:
        print(f'CONFIRMED_ORACLE_DEFECT: {error}', flush=True)
        raise SystemExit(73) from error


@dataclass
class Commands:
    replies: list[tuple[bool, str]]
    calls: list[tuple[list[str], bool, str | None]] = field(default_factory=list)

    def __call__(self, arguments: list[str], get_stdout: bool = True,
                 cwd: str | None = None) -> tuple[bool, str]:
        assert_arguments(arguments)
        self.calls.append((list(arguments), get_stdout, cwd))
        assert self.replies, f'Unexpected compiler invocation: {arguments}'
        return self.replies.pop(0)

    def finished(self, expected: list[list[str]]) -> None:
        assert not self.replies
        assert self.calls == [(command, True, None) for command in expected]


@dataclass
class Batch:
    directory: Path = field(default_factory=lambda: Path('oracle_batch'))
    oracles: dict[int, ProgramRes] = field(default_factory=dict)
    paths: dict[tuple[int, bool], str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        driver = load_driver()
        assert driver.cfg.prob.crossmodule_probability == 0
        driver.setup_crossmodule_manager('hermetic compiler 1.0')
        (self.directory / 'src').mkdir(parents=True)

    def add(self, pid: int, expected: bool,
            error: str | None = 'injected type mismatch') -> str:
        from src.translators.kotlin import KotlinTranslator
        from tests.driver.fixtures import replay

        driver = load_driver()
        source = self.directory / 'src' / f'd{pid}{"" if expected else "_incorrect"}' / 'program.kt'
        source.parent.mkdir(parents=True)
        program = replay(Path(str(source) + '.bin'))
        text = driver.utils.translate_program(KotlinTranslator(), program)
        source.write_text(text)
        temporary = Path(driver.cli_args.test_directory) / 'tmp' / str(pid)
        temporary.mkdir(parents=True, exist_ok=True)
        saved = temporary / ('program.kt' if expected else 'incorrect.kt')
        saved.write_text(text)
        driver.utils.dump_program(str(saved) + '.bin', program)
        if pid not in self.oracles:
            self.oracles[pid] = driver.ProgramRes(False, {
                'transformations': [], 'error': error,
                'programs': {}, 'time': 0.125,
            })
        self.oracles[pid].stats['programs'][str(source)] = expected
        self.paths[pid, expected] = str(source)
        contracts.program_result(self.oracles[pid])
        return str(source)

    def check(self) -> OracleResult:
        from hephaestus import OracleResult

        driver = load_driver()
        result = driver.check_oracle(str(self.directory), self.oracles)
        assert_type(result, OracleResult)
        contracts.oracle_result(result)
        assert result[3] == {}
        return result

    def preserved(self, pid: int, expected: bool) -> None:
        driver = load_driver()
        saved = Path(driver.cli_args.test_directory) / str(pid)
        source = saved / ('program.kt' if expected else 'incorrect.kt')
        assert source.is_file()
        contracts.program(driver.utils.load_program(str(source) + '.bin'))


def diagnostic(source: str, message: str, line: int = 1,
               column: int = 1) -> str:
    return f'{source}:{line}:{column}: error: {message}'


def main_command(batch: Batch) -> list[str]:
    from src.compilers.kotlin import KotlinCompiler

    return KotlinCompiler(str(batch.directory / 'src')).get_compiler_cmd()