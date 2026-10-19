from copy import deepcopy

from src.generators.config import cfg
from src.modules.crossmodule import CrossModuleManager
from src.modules.models import ProgramRes
from src.utils import RandomUtils


class _Random(RandomUtils):
    def __init__(self):
        pass


def test_program_result_keeps_logical_dependency_ids_out_of_stats(tmp_path):
    config = deepcopy(cfg)
    manager = CrossModuleManager(
        str(tmp_path), config, 'native', 'test', lambda _: None, _Random(),
        lambda _: None)
    result = ProgramRes(
        False, {}, direct_dependency_pid=7,
        dep_transitive_closure_pids=(7, 3))

    assert result.direct_dependency_pid == 7
    assert result.dep_transitive_closure_pids == (7, 3)
    assert manager.dependency_klibs(result.dep_transitive_closure_pids) == [
        str((tmp_path / 'klibs' / 'd7.klib').resolve()),
        str((tmp_path / 'klibs' / 'd3.klib').resolve()),
    ]
    assert 'dependency' not in result.stats
    assert ProgramRes.__annotations__['dep_transitive_closure_pids'] == tuple[int, ...]