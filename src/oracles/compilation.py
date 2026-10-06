from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class OracleFinding:
    kind: Literal['crash', 'unexpected_rejection', 'unexpected_acceptance']
    program: str | None
    diagnostics: tuple[str, ...]


def check_compilation(
        expectations: Mapping[str, bool],
        diagnostics: Mapping[str, Sequence[str]],
        crash_message: str | None) -> tuple[OracleFinding, ...]:
    if crash_message:
        return (OracleFinding('crash', None, (crash_message,)),)
    findings = []
    for program, oracle in expectations.items():
        if oracle and program in diagnostics:
            findings.append(OracleFinding(
                'unexpected_rejection', program, tuple(diagnostics[program])))
        if not oracle and program not in diagnostics:
            findings.append(OracleFinding('unexpected_acceptance', program, ()))
    return tuple(findings)