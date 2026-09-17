import json
from types import SimpleNamespace

from src.ir import ast, kotlin_types as kt
from src.ir.context import Context
from src.modules.crossmodule import CrossModuleManager


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


class _Random:

    def choice(self, values):
        return values[0]

    def bool(self, probability):
        return False


def _manager(test_directory, max_module_chain_depth=0, load_program=None):
    config = SimpleNamespace(prob=SimpleNamespace(
        crossmodule_probability=1.0,
        max_module_chain_depth=max_module_chain_depth,
        drop_indirect_deps_prob=0.0,
    ))
    return CrossModuleManager(
        str(test_directory), config, "native", "test",
        lambda _: None, _Random(),
        load_program or (lambda _: (_ for _ in ()).throw(AssertionError())),
    )


def _candidates(lab, max_module_chain_depth=0):
    return _manager(lab, max_module_chain_depth).provider_candidates(99)


def test_exported_names_include_qualified_main_but_skip_private_declarations():
    context = Context()
    context.generation_package = "src.d7"
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


def test_prepare_dependency_restores_context_and_full_closure(tmp_path):
    lab = tmp_path / "lab"
    _write_ledger(lab, {"17": {"direct": 3, "closure": [3]}})
    _publish_artifacts(lab, [17, 3])
    imported_context = SimpleNamespace(prepared=False)
    imported_context.prepare_this_context_for_import = (
        lambda: setattr(imported_context, "prepared", True))

    dependency = _manager(
        lab, load_program=lambda _: SimpleNamespace(context=imported_context)
    ).prepare_dependency(99)

    assert dependency["pid"] == 17
    assert dependency["context"] is imported_context
    assert dependency["closure"] == [17, 3]
    assert dependency["klibs"] == [
        str((lab / "klibs" / "d17.klib").resolve()),
        str((lab / "klibs" / "d3.klib").resolve()),
    ]
    assert imported_context.prepared
