from typing import Any, List, NamedTuple, Optional

from src.modules.crossmodule import PublicationRecord


type ProgramStats = dict[str, Any]
type TimeMetrics = dict[int, dict[str, float]]
type OracleResult = tuple[dict[int, ProgramStats], float, TimeMetrics,
                          dict[int, PublicationRecord]]


class ProgramRes(NamedTuple):
    failed: bool
    stats: dict
    direct_dependency_pid: Optional[int] = None
    dep_transitive_closure_klibs: Optional[List[str]] = None