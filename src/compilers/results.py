from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.compilers.base import BaseCompiler


@dataclass(frozen=True)
class Diagnostic:
    source: str | None
    line: int | None
    column: int | None
    message: str
    name: str | None = None


@dataclass(frozen=True)
class ParsedDiagnostics:
    errors: tuple[Diagnostic, ...] = ()
    complete: bool = True
    unparsed: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    output: str
    launch_failure: str | None = None

    @property
    def status(self) -> bool:
        return self.returncode == 0 and self.launch_failure is None


@dataclass(frozen=True)
class CommandObservation:
    process: ProcessResult
    diagnostics: Mapping[str, Sequence[str]]
    parsed: ParsedDiagnostics
    filtered_errors: tuple[Diagnostic, ...]
    crash_message: str | None
    command: tuple[str, ...]
    cwd: str | None
    elapsed: float

    @property
    def status(self) -> bool:
        return self.process.status

    @property
    def output(self) -> str:
        return self.process.output

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    @property
    def launch_failure(self) -> str | None:
        return self.process.launch_failure

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return self.parsed.errors

    @property
    def parsing_complete(self) -> bool:
        return self.parsed.complete

    @property
    def unparsed(self) -> tuple[str, ...]:
        return self.parsed.unparsed

    def succeeded(self) -> bool:
        return self.status and self.crash_message is None and self.parsed.complete


@dataclass(frozen=True)
class CompilationRequest[S]:
    variant_id: str
    compiler: BaseCompiler[S]
    working_directory: str | None = None
    expected_dependency_artifact_path: str | None = None
    blocked_reason: str | None = None
    path_aliases: Mapping[str, str] = field(default_factory=dict)

'''
path_aliases : {
    "/tmp/hephaestus-batch/src/d42/program.kt" --> "program.kt",
}

needed to emit in run/<pid>/<var_id>/result.json, instead of `kotlinc /tmp/hephaestus-batch/src/d42/program.kt ...`, `kotlinc program.kt ...`
'''

@dataclass(frozen=True)
class VariantObservation[S]:
    variant_id: str
    command: CommandObservation | None
    expected_dependency_artifact_path: str | None = None
    produced_dependency_artifact_path: str | None = None
    dependency_artifact_valid: bool = True
    incomplete_reason: str | None = None
    settings: S = field(kw_only=True)
