from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple, NotRequired, Optional, TypedDict

from src.modules.crossmodule import PublicationRecord


type ProgramStats = dict[str, Any]


def mark_inconclusive(stats: ProgramStats, oracle: str, reason: str,
                      details: str, **evidence: object) -> ProgramStats:
    result = dict(stats)
    result['oracle'] = oracle
    result['inconclusive_reason'] = reason
    result['inconclusive_details'] = details
    if evidence:
        result['inconclusive_evidence'] = evidence
    return result


class ProgramTimeMetrics(TypedDict):
    generation_cpu: float
    compilation_with_ir_dumps: float
    phase_profiling: float
    variants: NotRequired[dict[str, float | None]]


type TimeMetrics = dict[int, ProgramTimeMetrics]
@dataclass
class OracleResult:
    faults: dict[int, ProgramStats] = field(default_factory=dict)
    compilation_time: float = 0.0
    time_metrics: TimeMetrics = field(default_factory=dict)
    publications: dict[int, PublicationRecord] = field(default_factory=dict)
    inconclusive: dict[int, ProgramStats] = field(default_factory=dict)


class ProgramRes(NamedTuple):
    failed: bool
    stats: dict
    direct_dependency_pid: Optional[int] = None
    dep_transitive_closure_pids: tuple[int, ...] = ()


@dataclass(frozen=True)
class VariantDependencies:
    dependencies: tuple[str, ...]
    friends: tuple[str, ...]


@dataclass(frozen=True)
class ComparisonDependencies:
    direct: int | None
    closure: tuple[int, ...]
    kept: tuple[int, ...]
    dropped: tuple[int, ...]
    variants: Mapping[str, VariantDependencies]