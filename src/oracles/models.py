from dataclasses import dataclass
from typing import Literal

from src.compilers.results import Diagnostic


@dataclass(frozen=True)
class OracleFinding:
    kind: Literal['crash', 'unexpected_rejection', 'unexpected_acceptance',
                  'status_mismatch', 'diagnostic_mismatch']
    program: str | None
    diagnostics: tuple[str, ...]
    variants: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagnosticDifference:
    diagnostic: Diagnostic
    counts: dict[str, int]


@dataclass(frozen=True)
class ComparisonResult:
    findings: tuple[OracleFinding, ...]
    groups: dict[str, tuple[str, ...]]
    diagnostic_groups: tuple[tuple[str, ...], ...] = ()
    differences: tuple[DiagnosticDifference, ...] = ()
    incomplete_reasons: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.incomplete_reasons

    @property
    def all_success(self) -> bool:
        return self.complete and not self.findings and bool(self.groups.get('accepted'))
