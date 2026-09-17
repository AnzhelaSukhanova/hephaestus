import json
from types import SimpleNamespace

from src.modules.crossmodule import CrossModuleManager


KLIBS = ["/lab/klibs/d17.klib", "/lab/klibs/d3.klib", "/lab/klibs/d1.klib"]


class _Random:

    def bool(self, probability):
        return probability == 1.0


def _manager(test_directory, probability):
    config = SimpleNamespace(prob=SimpleNamespace(
        crossmodule_probability=1.0,
        drop_indirect_deps_prob=probability,
    ))
    return CrossModuleManager(
        str(test_directory), config, "native", "test",
        lambda _: None, _Random(), lambda _: None,
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
