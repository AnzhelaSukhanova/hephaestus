import json
from copy import deepcopy

from src.generators.config import cfg
from src.modules.crossmodule import CrossModuleManager
from src.utils import RandomUtils


KLIBS = ["/lab/klibs/d17.klib", "/lab/klibs/d3.klib", "/lab/klibs/d1.klib"]


class _Random(RandomUtils):
    def __init__(self):
        pass

    def choice(self, values):
        return values[0]

    def bool(self, probability):
        return probability == 1.0


def _unavailable_program(_path):
    raise AssertionError("program loading is not expected")


def _manager(test_directory, probability):
    config = deepcopy(cfg)
    config.prob.crossmodule_probability = 1.0
    config.prob.max_module_chain_depth = 0
    config.prob.drop_indirect_deps_prob = probability
    return CrossModuleManager(
        str(test_directory), config, "native", "test",
        lambda _: None, _Random(), _unavailable_program,
    )


def test_every_indirect_dependency_is_dropped_at_probability_one(tmp_path):
    kept, dropped = _manager(tmp_path, 1.0).apply_drop_knob(KLIBS)

    # The direct dependency is never dropped.
    assert kept == [KLIBS[0]]
    assert dropped == [3, 1]


def test_nothing_is_dropped_by_default(tmp_path):
    kept, dropped = _manager(tmp_path, 0.0).apply_drop_knob(KLIBS)

    assert kept == KLIBS
    assert dropped == []


def test_a_chain_root_dependency_is_never_dropped(tmp_path):
    kept, dropped = _manager(tmp_path, 1.0).apply_drop_knob([KLIBS[0]])

    assert kept == [KLIBS[0]]
    assert dropped == []


def test_dropping_leaves_the_ledger_closure_untouched(tmp_path):
    (tmp_path / "crossmodule.json").write_text(json.dumps({
        "3": {"direct": None, "closure": []},
        "17": {"direct": 3, "closure": [3]},
    }))
    manager = _manager(tmp_path, 1.0)

    _, dropped = manager.apply_drop_knob(
        ['/lab/klibs/d17.klib', '/lab/klibs/d3.klib'])
    record = manager.publication_record(17)

    assert dropped == [3]
    assert record['closure'] == [17, 3]
