import json
from copy import deepcopy

import pytest

from src.generators.config import cfg
from src.ir import ast, kotlin_types as kt
from src.ir.context import Context
from src.modules.crossmodule import CrossModuleManager, ManifestDiff, VariantPublication
from src.modules.models import OracleResult
from src.modules.worker_reporting import Reporting
from src.utils import RandomUtils
from tests.test_outcome_consumers import reporting_options


CROSSMODULE_STATS_FIELDS = {
    "crossmodule", "crossmodule_manifest_diff", "crossmodule_variant_manifest_diff"}


def _function(name, visibility=ast.Visibilities.PUBLIC):
    return ast.FunctionDeclaration(
        name,
        [],
        kt.Integer,
        ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.FUNCTION,
        visibility=visibility,
    )


def _write_ledger(lab, ledger):
    lab.mkdir(parents=True, exist_ok=True)
    (lab / "crossmodule.json").write_text(json.dumps(ledger))


def _publish_artifacts(lab, pids):
    (lab / "klibs").mkdir(parents=True, exist_ok=True)
    for pid in pids:
        (lab / str(pid)).mkdir(parents=True, exist_ok=True)
        (lab / str(pid) / "program.kt.bin").write_bytes(b"")
        (lab / "klibs" / "d{}.klib".format(pid)).write_bytes(b"")


class _Random(RandomUtils):
    def __init__(self):
        pass

    def choice(self, values):
        return values[0]

    def bool(self, probability):
        return False


class _ImportedContext(Context):
    def __init__(self):
        super().__init__()
        self.prepared = False

    def prepare_this_context_for_import(self):
        self.prepared = True


def _manager(test_directory, max_module_chain_depth=0, load_program=None):
    config = deepcopy(cfg)
    config.prob.crossmodule_probability = 1.0
    config.prob.max_module_chain_depth = max_module_chain_depth
    config.prob.drop_indirect_deps_prob = 0.0
    return CrossModuleManager(
        str(test_directory), config, "native", "test",
        lambda _: None, _Random(),
        load_program or (lambda _: (_ for _ in ()).throw(AssertionError())),
    )


def _candidates(lab, max_module_chain_depth=0):
    return _manager(lab, max_module_chain_depth).provider_candidates(99)


def test_exported_names_include_qualified_main_but_skip_private_declarations():
    context = Context()
    setattr(context, 'generation_package', "src.d7")
    program = ast.Program(context, "kotlin")
    program.add_declaration(_function("src.d7.visible"))
    program.add_declaration(
        _function("src.d7.hidden", ast.Visibilities.PRIVATE))
    program.add_declaration(ast.ClassDeclaration("src.d7.Box", []))
    program.add_declaration(ast.VariableDeclaration(
        "src.d7.value",
        ast.IntegerConstant(1, kt.Integer),
        var_type=kt.Integer,
    ))
    program.add_declaration(ast.FunctionDeclaration(
        "src.d7.main",
        [],
        kt.Unit,
        ast.Block([]),
        ast.FunctionDeclaration.FUNCTION,
    ))

    assert program.get_exported_names() == [
        "src.d7.visible", "src.d7.Box", "src.d7.value", "src.d7.main",
    ]


def test_publication_record_of_a_chain_root_has_no_closure(tmp_path):
    record = _manager(tmp_path / "lab").publication_record(None)

    assert record == {
        "direct": None,
        "closure": [],
    }


def test_publication_record_extends_the_direct_dependency_closure(tmp_path):
    lab = tmp_path / "lab"
    _write_ledger(lab, {
        "3": {"direct": None, "closure": [], "exports": ["src.d3.a"]},
        "17": {"direct": 3, "closure": [3], "exports": ["src.d17.b"]},
    })
    record = _manager(lab).publication_record(17)

    assert record["direct"] == 17
    assert record["closure"] == [17, 3]


def test_publication_record_ignores_legacy_exports(tmp_path):
    lab = tmp_path / "lab"
    _write_ledger(lab, {
        "1": {"direct": None, "closure": [], "exports": ["src.d1.Provider"]},
    })
    record = _manager(lab).publication_record(1)

    assert record == {
        "direct": 1,
        "closure": [1],
    }


def test_ledger_falls_back_for_absent_invalid_or_non_mapping_json(tmp_path):
    lab = tmp_path / "lab"
    manager = _manager(lab)
    assert manager.load_ledger() == {}

    lab.mkdir()
    (lab / "crossmodule.json").write_text("{")
    assert manager.load_ledger() == {}

    (lab / "crossmodule.json").write_text("[]")
    assert manager.load_ledger() == {}


def test_malformed_ledger_entries_do_not_claim_a_dependency_closure(tmp_path):
    lab = tmp_path / "lab"
    _write_ledger(lab, {"3": "stale", "17": {"closure": 42}})
    manager = _manager(lab)

    assert manager.ledger_closure(manager.load_ledger(), 3) == []
    assert manager.ledger_closure(manager.load_ledger(), 17) == []


def test_every_published_program_is_a_provider_candidate(tmp_path):
    lab = tmp_path / "lab"
    _write_ledger(lab, {
        "3": {"direct": None, "closure": []},
        "17": {"direct": 3, "closure": [3]},
    })
    _publish_artifacts(lab, [3, 17])

    assert _candidates(lab) == [3, 17]


def test_max_module_chain_depth_bounds_provider_candidates(tmp_path):
    lab = tmp_path / "lab"
    _write_ledger(lab, {
        "3": {"direct": None, "closure": []},
        "17": {"direct": 3, "closure": [3]},
    })
    _publish_artifacts(lab, [3, 17])

    # A consumer of 17 would be the third module of its chain, a consumer of
    # 3 only the second one.
    assert _candidates(lab, max_module_chain_depth=3) == [3, 17]
    assert _candidates(lab, max_module_chain_depth=2) == [3]
    assert _candidates(lab, max_module_chain_depth=1) == []


def test_provider_candidates_require_complete_artifacts(tmp_path):
    lab = tmp_path / "lab"
    _write_ledger(lab, {
        "3": {"direct": None, "closure": []},
        "17": {"direct": None, "closure": []},
        "99": {"direct": None, "closure": []},
    })
    _publish_artifacts(lab, [3, 17, 99])
    (lab / "klibs" / "d17.klib").unlink()

    # 17 lost its KLIB and 99 is the program being generated.
    assert _candidates(lab) == [3]


def test_missing_klib_does_not_publish_provider(tmp_path):
    lab = tmp_path / "lab"
    source_dir = lab / "tmp" / "7"
    source_dir.mkdir(parents=True)
    (source_dir / "program.kt").write_text("fun main() {}")
    (source_dir / "program.kt.bin").write_bytes(b"pickle")

    record = _manager(lab).publish_provider(
        7, str(lab / "batch" / "d7.klib"), ["fake-compiler"], None)

    assert record is None
    assert not (lab / "7").exists()


def test_missing_source_or_pickle_does_not_publish_provider(tmp_path):
    lab = tmp_path / "lab"
    source_dir = lab / "tmp" / "7"
    source_dir.mkdir(parents=True)
    klib = lab / "batch" / "d7.klib"
    klib.parent.mkdir()
    klib.write_bytes(b"klib")
    manager = _manager(lab)

    for filename in ("program.kt.bin", "program.kt"):
        artifact = source_dir / filename
        artifact.write_bytes(b"source")
        assert manager.publish_provider(7, str(klib), [], None) is None
        assert not (lab / "7").exists()
        artifact.unlink()


def test_prepare_dependency_restores_context_and_full_closure(tmp_path):
    lab = tmp_path / "lab"
    _write_ledger(lab, {"17": {"direct": 3, "closure": [3]}})
    _publish_artifacts(lab, [17, 3])
    imported_context = _ImportedContext()

    dependency = _manager(
        lab, load_program=lambda _: ast.Program(imported_context, "kotlin")
    ).prepare_dependency(99)

    assert dependency["pid"] == 17
    assert dependency["context"] is imported_context
    assert dependency["closure"] == [17, 3]
    assert "klibs" not in dependency
    assert _manager(lab).dependency_klibs(dependency["closure"]) == [
        str((lab / "klibs" / "d17.klib").resolve()),
        str((lab / "klibs" / "d3.klib").resolve()),
    ]
    assert imported_context.prepared


def test_prepare_dependency_skips_failed_program_load(tmp_path):
    lab = tmp_path / "lab"
    _write_ledger(lab, {"17": {"direct": None, "closure": []}})
    _publish_artifacts(lab, [17])

    assert _manager(lab).prepare_dependency(99) is None


def test_prepare_dependency_requires_every_klib_in_the_closure(tmp_path):
    lab = tmp_path / "lab"
    _write_ledger(lab, {"17": {"direct": 3, "closure": [3]}})
    _publish_artifacts(lab, [17, 3])
    (lab / "klibs" / "d3.klib").unlink()

    assert _manager(lab).prepare_dependency(99) is None


def test_manifest_diff_reports_only_unexpected_dependencies(tmp_path):
    manager = _manager(tmp_path)
    manager.read_manifest_depends = lambda _: {"d17", "d3", "d9"}

    assert manager.manifest_closure_diff("unused.klib", [17, 3]) == {
        "manifest_depends": ["d17", "d3", "d9"],
        "closure": ["d17", "d3"],
        "unexpected": ["d9"],
    }
    assert manager.manifest_closure_diff("unused.klib", [17, 3], [3]) == {
        "manifest_depends": ["d17", "d3", "d9"],
        "closure": ["d17"],
        "unexpected": ["d3", "d9"],
    }


def _staged_provider(lab, pid):
    source_dir = lab / "tmp" / str(pid)
    source_dir.mkdir(parents=True)
    (source_dir / "program.kt").write_text("fun main() {}")
    (source_dir / "program.kt.bin").write_bytes(b"pickle")
    klib = lab / "batch" / "d{}.klib".format(pid)
    klib.parent.mkdir(exist_ok=True)
    klib.write_bytes(b"klib")
    return str(klib)


def test_manifest_disagreement_is_klib_evidence_not_ledger_state(tmp_path, capsys):
    lab = tmp_path / "lab"
    _write_ledger(lab, {"3": {"direct": None, "closure": []}})
    _publish_artifacts(lab, [3])
    manager = _manager(lab, load_program=lambda _: ast.Program(Context(), "kotlin"))
    manager.read_manifest_depends = lambda _: {"d3", "ghost"}
    diff = {"manifest_depends": ["d3", "ghost"], "closure": ["d3"],
            "unexpected": ["ghost"]}

    record = manager.publish_provider(
        7, _staged_provider(lab, 7), ["kotlinc", "-Xflag"], 3)

    assert record == {"direct": 3, "closure": [3], "manifest_diff": diff}
    metadata = json.loads((lab / "klibs" / "d7.meta.json").read_text())
    assert metadata["manifest_diff"] == diff
    assert metadata["command"] == ["kotlinc", "-Xflag"]
    # Publication alone does not advertise the provider.
    assert manager.load_ledger() == {"3": {"direct": None, "closure": []}}
    assert _candidates(lab) == [3]

    manager.commit_publications({7: record})

    assert "Program 7 records dependencies outside its closure: ['ghost']" in (
        capsys.readouterr().out)
    assert manager.load_ledger() == {
        "3": {"direct": None, "closure": []},
        "7": {"direct": 3, "closure": [3]},
    }
    assert record["manifest_diff"] == diff
    assert _candidates(lab) == [3, 7]
    assert manager.publication_record(7)["closure"] == [7, 3]
    assert not list(lab.glob("*.tmp"))


def test_commit_publications_merges_batches_atomically(tmp_path, monkeypatch):
    lab = tmp_path / "lab"
    manager = _manager(lab)
    lab.mkdir()
    manager.commit_publications({})
    assert not (lab / "crossmodule.json").exists()

    _write_ledger(lab, {"3": {"direct": None, "closure": [], "exports": ["a"]}})
    manager.commit_publications({17: {"direct": 3, "closure": [3]}})
    manager.commit_publications({
        18: {"direct": 17, "closure": [17, 3]},
        19: {"direct": None, "closure": [], "variants": {}},
    })
    expected = {
        "3": {"direct": None, "closure": [], "exports": ["a"]},
        "17": {"direct": 3, "closure": [3]},
        "18": {"direct": 17, "closure": [17, 3]},
        "19": {"direct": None, "closure": [], "variants": {}},
    }
    assert manager.load_ledger() == expected
    assert manager.ledger_closure(manager.load_ledger(), 18) == [17, 3]

    committed = (lab / "crossmodule.json").read_bytes()
    manager.commit_publications({})
    assert (lab / "crossmodule.json").read_bytes() == committed

    def interrupted(source, destination):
        raise OSError("interrupted ledger replacement")

    monkeypatch.setattr("src.modules.crossmodule.os.replace", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        manager.commit_publications({20: {"direct": None, "closure": []}})
    assert (lab / "crossmodule.json").read_bytes() == committed


def test_run_statistics_hold_no_crossmodule_bookkeeping(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, "crossmodule_probability", 1.0)
    lab = tmp_path / "lab"
    _write_ledger(lab, {"3": {"direct": None, "closure": []}})
    manager = _manager(lab)
    reporting = Reporting(reporting_options(lab), manager)
    assert not CROSSMODULE_STATS_FIELDS & reporting.stats.keys()
    diff: ManifestDiff = {
        "manifest_depends": ["ghost"], "closure": ["d3"], "unexpected": ["ghost"]}
    variants: dict[str, VariantPublication] = {"js": {
        "backend": "js", "module_name": "d8",
        "klib": "klibs/d8-js.klib", "metadata": "klibs/d8-js.meta.json"}}

    reporting.update_stats(OracleResult(publications={
        7: {"direct": 3, "closure": [3], "manifest_diff": diff}}), 1, 0)
    reporting.update_stats(OracleResult(publications={
        8: {"direct": 7, "closure": [7, 3], "variants": variants,
            "variant_manifest_diff": {"js": diff}}}), 1, 0)

    assert manager.load_ledger() == {
        "3": {"direct": None, "closure": []},
        "7": {"direct": 3, "closure": [3]},
        "8": {"direct": 7, "closure": [7, 3], "variants": variants},
    }
    stats = json.loads((lab / "stats.json").read_text())
    assert stats["totals"]["passed"] == 2
    assert not CROSSMODULE_STATS_FIELDS & stats.keys()
    assert not CROSSMODULE_STATS_FIELDS & reporting.stats.keys()

    # Saving statistics, or a batch without publications, leaves the ledger alone.
    ledger = lab / "crossmodule.json"
    committed = ledger.read_bytes()
    reporting.save_stats()
    reporting.update_stats(OracleResult(), 1, 0)
    assert ledger.read_bytes() == committed
    ledger.unlink()
    reporting.save_stats()
    assert not ledger.exists()
