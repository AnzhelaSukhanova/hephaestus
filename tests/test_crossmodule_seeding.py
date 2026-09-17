import pytest

from src import utils as ut
from src.generators.generator import Generator
from src.ir import ast, kotlin_types as kt, types
from src.ir.context import Context


def _function(name, visibility=ast.Visibilities.PUBLIC):
    return ast.FunctionDeclaration(
        name,
        [],
        kt.Integer,
        ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.FUNCTION,
        visibility=visibility,
    )


def _published_context(package="src.d1"):
    """A context in the shape a finished program leaves behind."""
    context = Context()
    context.target_module = package
    program = ast.Program(context, "kotlin")
    program.add_declaration(_function(package + ".shared"))
    program.add_declaration(
        _function(package + ".hidden", ast.Visibilities.PRIVATE))
    program.add_declaration(ast.ClassDeclaration(package + ".Box", []))
    program.add_declaration(ast.FunctionDeclaration(
        package + ".main",
        [],
        kt.Unit,
        ast.Block([]),
        ast.FunctionDeclaration.FUNCTION,
    ))
    return context


def test_prepared_context_keeps_lookups_but_emits_nothing():
    context = _published_context()
    context.prepare_this_context_for_import()
    context.target_module = "src.d2"

    assert set(context.get_declarations(ast.GLOBAL_NAMESPACE,
                                        only_current=True)) == {
        "src.d1.shared", "src.d1.Box", "src.d1.main",
    }
    assert list(ast.Program(context, "kotlin").children()) == []
    funcs = context.get_funcs(ast.GLOBAL_NAMESPACE)
    assert "src.d1.shared" in funcs
    # Private declarations do not cross a file boundary, while a qualified
    # main has no special collision/removal rule.
    assert "src.d1.hidden" not in funcs
    assert "src.d1.main" in funcs
    assert context.get_decl(ast.GLOBAL_NAMESPACE, "src.d1.main") is funcs[
        "src.d1.main"]
    assert "src.d1.Box" in context.get_classes(ast.GLOBAL_NAMESPACE)


@pytest.mark.parametrize(
    "cleanup",
    [
        "_drop_unreachable_global_declarations_from_lookup",
    ],
)
def test_import_cleanup_requires_global_namespace(cleanup):
    with pytest.raises(KeyError):
        getattr(Context(), cleanup)()


def test_lookup_cleanup_requires_declaration_bucket():
    context = Context()
    context._context[ast.GLOBAL_NAMESPACE] = {}

    with pytest.raises(KeyError):
        context._drop_unreachable_global_declarations_from_lookup()


def test_import_preparation_requires_global_namespace():
    with pytest.raises(KeyError):
        Context().prepare_this_context_for_import()


def test_import_preparation_fails_loudly_for_incomplete_context():
    context = Context()
    context._context[ast.GLOBAL_NAMESPACE] = {}

    with pytest.raises(KeyError):
        context.prepare_this_context_for_import()


def test_import_preparation_does_not_special_case_bare_main():
    context = Context()
    main = _function("main")
    ast.Program(context, "kotlin").add_declaration(main)

    context.prepare_this_context_for_import()

    assert context.get_funcs(ast.GLOBAL_NAMESPACE)["main"] is main


def test_generate_captures_and_replaces_direct_dependency_modules(monkeypatch):
    generator = Generator("kotlin", target_module="src.d2")
    monkeypatch.setattr(generator, "gen_top_level_declaration", lambda: None)
    monkeypatch.setattr(generator, "generate_main_func", lambda: None)

    imported = _published_context()
    imported.prepare_this_context_for_import()
    generator.generate(imported)

    assert generator._direct_dependency_modules == frozenset({"src.d1"})
    assert imported.target_module == "src.d2"

    generator.generate(Context())

    assert generator._direct_dependency_modules == frozenset()


@pytest.mark.parametrize(
    "providers, accepted",
    [
        (frozenset(), {"src.d2.own"}),
        (frozenset({"src.d1"}), {"src.d1.shared", "src.d2.own"}),
        (frozenset({"src.d1", "src.d3"}),
         {"src.d1.shared", "src.d2.own", "src.d3.other"}),
    ],
)
def test_checker_uses_exact_consumer_and_direct_provider_modules(
        providers, accepted):
    context = Context()
    declarations = {
        name: _function(name)
        for name in (
            "src.d1.shared", "src.d2.own", "src.d3.other",
            "src.d0.ancestor", "src.d10.foreign",
        )
    }
    for name, declaration in declarations.items():
        context.add_func(ast.GLOBAL_NAMESPACE, name, declaration)

    generator = Generator("kotlin", target_module="src.d2")
    generator.context = context
    generator._direct_dependency_modules = providers

    assert {
        name for name, declaration in declarations.items()
        if generator._allowed_by_explicit_imports_only_in_generation_paths_checker(
                declaration)
    } == accepted


def test_checker_allows_bare_names_and_rejects_stale_typed_entries():
    context = Context()
    canonical = _function("src.d1.visible")
    context.add_func(ast.GLOBAL_NAMESPACE, canonical.name, canonical)
    stale = _function("src.d1.stale")
    context._context[ast.GLOBAL_NAMESPACE]["funcs"][stale.name] = stale

    generator = Generator("kotlin", target_module="src.d2")
    generator.context = context
    generator._direct_dependency_modules = frozenset({"src.d1"})

    assert generator._allowed_by_explicit_imports_only_in_generation_paths_checker(
        canonical)
    assert generator._allowed_by_explicit_imports_only_in_generation_paths_checker(
        _function("member"))
    assert not generator._allowed_by_explicit_imports_only_in_generation_paths_checker(
        stale)


def test_checker_is_unrestricted_for_ordinary_generation():
    generator = Generator("kotlin")
    generator.context = Context()

    assert generator._allowed_by_explicit_imports_only_in_generation_paths_checker(
        _function("src.d0.foreign"))


def test_assignable_variables_filter_foreign_roots():
    context = Context()
    context.target_module = "src.d1"
    for name in ("src.d1.mutable", "src.d0.ancestor", "src.d2.own"):
        context.add_var(
            ast.GLOBAL_NAMESPACE,
            name,
            ast.VariableDeclaration(
                name,
                ast.IntegerConstant(1, kt.Integer),
                is_final=False,
                var_type=kt.Integer,
            ),
        )
    context.prepare_this_context_for_import()
    context.target_module = "src.d2"

    generator = Generator("kotlin", target_module="src.d2")
    generator.context = context
    generator._direct_dependency_modules = frozenset({"src.d1"})

    assert {variable.name for _, variable in generator._get_assignable_vars()} == {
        "src.d1.mutable", "src.d2.own",
    }


def test_matching_objects_filter_root_variables(monkeypatch):
    context = Context()
    context.target_module = "src.d1"
    for name in ("src.d1.receiver", "src.d0.ancestor", "src.d2.own"):
        context.add_var(
            ast.GLOBAL_NAMESPACE,
            name,
            ast.VariableDeclaration(
                name,
                ast.IntegerConstant(1, kt.Integer),
                var_type=kt.Integer,
            ),
        )
    context.prepare_this_context_for_import()
    context.target_module = "src.d2"

    generator = Generator("kotlin", target_module="src.d2")
    generator.context = context
    generator._direct_dependency_modules = frozenset({"src.d1"})
    fake_class = ast.ClassDeclaration("src.d2.Fake", [])
    field = ast.FieldDeclaration("value", kt.Integer)
    monkeypatch.setattr(
        generator, "_get_class", lambda _type: (fake_class, {}))
    monkeypatch.setattr(
        generator, "_get_var_type_to_search", lambda _type: kt.Integer)
    monkeypatch.setattr(
        generator, "_get_class_attributes", lambda _cls, _attr: [field])
    monkeypatch.setattr(
        generator, "_is_sigtype_compatible", lambda *args: True)

    receivers = generator._get_matching_objects(
        kt.Integer, subtype=False, attr_name="fields")

    assert {info.receiver_expr.name for info in receivers} == {
        "src.d1.receiver", "src.d2.own",
    }


def test_assignable_fields_filter_foreign_classes():
    context = Context()
    context.target_module = "src.d1"
    classes = {}
    for name in ("src.d1.Box", "src.d0.Ancestor", "src.d2.Own"):
        cls = ast.ClassDeclaration(
            name,
            [],
            fields=[ast.FieldDeclaration(
                name + ".value", kt.Integer, is_immutable=False,
            )],
        )
        classes[name] = cls
        context.add_class(ast.GLOBAL_NAMESPACE, name, cls)
    context.prepare_this_context_for_import()
    context.target_module = "src.d2"

    generator = Generator("kotlin", target_module="src.d2")
    generator.context = context
    generator._direct_dependency_modules = frozenset({"src.d1"})

    class_type, _ = generator._get_classes_with_assignable_fields()

    assert class_type.name in {"src.d1.Box", "src.d2.Own"}
    assert class_type.name != "src.d0.Ancestor"


def test_subclass_selection_filters_foreign_classes_and_keeps_bottom_fallback():
    context = Context()
    context.target_module = "src.d1"
    classes = []
    for name in ("src.d1.Child", "src.d0.Ancestor", "src.d2.Own"):
        cls = ast.ClassDeclaration(name, [])
        classes.append(cls)
        context.add_class(ast.GLOBAL_NAMESPACE, name, cls)
    target_type = classes[0].get_type()
    for cls in classes:
        cls.get_type = lambda target_type=target_type: target_type
    foreign = ast.ClassDeclaration("src.d0.Foreign", [])
    context.add_class(ast.GLOBAL_NAMESPACE, foreign.name, foreign)
    context.prepare_this_context_for_import()
    context.target_module = "src.d2"

    generator = Generator("kotlin", target_module="src.d2")
    generator.context = context
    generator._direct_dependency_modules = frozenset({"src.d1"})

    selected = generator._get_subclass(target_type)
    assert selected.name in {"src.d1.Child", "src.d2.Own"}
    assert isinstance(generator.gen_new(foreign.get_type()), ast.BottomConstant)


@pytest.mark.parametrize("module", [None, "src.d1", "src.d2", "src.d10"])
def test_retained_declarations_resolve_independently_of_target_module(module):
    context = _published_context()
    retained = context.get_declarations(ast.GLOBAL_NAMESPACE)
    context.prepare_this_context_for_import()
    context.target_module = module

    for name in ("src.d1.shared", "src.d1.Box"):
        assert context.get_decl(ast.GLOBAL_NAMESPACE, name) is retained[name]
    # Resolution is through decls, not the typed buckets.
    context._context[ast.GLOBAL_NAMESPACE]["funcs"].pop("src.d1.shared")
    assert context.get_decl(ast.GLOBAL_NAMESPACE, "src.d1.shared") is retained[
        "src.d1.shared"]
    assert context.get_decl(ast.GLOBAL_NAMESPACE, "missing") is None
    assert context.get_decl(ast.GLOBAL_NAMESPACE + ("missing",), "missing") is None


@pytest.mark.parametrize("module", [None, "src.d1", "src.d2"])
@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("bucket, factory", [
    ("funcs", _function),
    ("classes", lambda name: ast.ClassDeclaration(name, [])),
    ("vars", lambda name: ast.VariableDeclaration(
        name, ast.IntegerConstant(1, kt.Integer), var_type=kt.Integer)),
    ("types", types.TypeParameter),
])
def test_typed_only_entries_do_not_resolve_as_declarations(
        module, prepared, bucket, factory):
    context = _published_context()
    if prepared:
        context.prepare_this_context_for_import()
    context.target_module = module
    stale = factory("src.d1.stale")
    context._context[ast.GLOBAL_NAMESPACE][bucket][stale.name] = stale

    assert context.get_decl(ast.GLOBAL_NAMESPACE, stale.name) is None


def test_all_prepared_direct_provider_roots_are_call_candidates():
    context = Context()
    context.target_module = "src.d1"
    for name in ("src.d1.exported", "src.d1.internal", "src.d0.ancestor",
                 "src.d2.own"):
        context.add_func(ast.GLOBAL_NAMESPACE, name, _function(name))
    context.prepare_this_context_for_import()
    context.target_module = "src.d2"
    generator = Generator("kotlin", target_module="src.d2")
    generator.context = context
    generator._direct_dependency_modules = frozenset({"src.d1"})
    generator.namespace = ast.GLOBAL_NAMESPACE

    candidates = generator._get_matching_function_declarations(
        kt.Integer, subtype=False)

    assert {c.attr_decl.name for c in candidates} == {
        "src.d1.exported", "src.d1.internal", "src.d2.own",
    }


def test_ancestor_classes_are_never_fresh_targets_but_stay_resolvable():
    context = Context()
    context.target_module = "src.d1"
    for name in ("src.d1.Exported", "src.d0.Ancestor"):
        context.add_class(ast.GLOBAL_NAMESPACE, name,
                           ast.ClassDeclaration(name, []))
    context.prepare_this_context_for_import()
    context.target_module = "src.d2"
    generator = Generator("kotlin", target_module="src.d2")
    generator.context = context
    generator._direct_dependency_modules = frozenset({"src.d1"})
    generator.namespace = ast.GLOBAL_NAMESPACE

    type_names = {t.name for t in generator.get_types()}
    assert "src.d1.Exported" in type_names
    assert "src.d0.Ancestor" not in type_names
    # The ancestor still resolves, which is what signatures mentioning it
    # depend on.
    assert generator.context.get_decl(
        ast.GLOBAL_NAMESPACE, "src.d0.Ancestor") is not None
    program = ast.Program(generator.context, "kotlin")
    assert {t.name for t in program.get_types()} >= {
        "src.d1.Exported", "src.d0.Ancestor",
    }
    assert list(program.children()) == []


def test_a_bare_name_may_be_minted_again_by_the_next_module(monkeypatch):
    """The motivating property: no name coordination between modules."""
    context = _published_context()
    context.prepare_this_context_for_import()
    context.target_module = "src.d2"
    generator = Generator("kotlin", target_module="src.d2")
    generator.context = context
    generator._direct_dependency_modules = frozenset({"src.d1"})
    generator.namespace = ast.GLOBAL_NAMESPACE
    monkeypatch.setattr(
        generator,
        "_gen_func_body",
        lambda _ret_type, _func=None: ast.IntegerConstant(1, kt.Integer),
    )

    # The dependency already holds a top-level ``shared``.
    func = generator.gen_func_decl(
        etype=kt.Integer,
        func_name="shared",
        params=[],
        type_params=[],
        visibility=ast.Visibilities.PUBLIC,
    )

    assert func.name == "src.d2.shared"
    assert set(context.get_funcs(ast.GLOBAL_NAMESPACE)) >= {
        "src.d1.shared", "src.d2.shared",
    }
    assert set(context.get_declarations(ast.GLOBAL_NAMESPACE,
                                        only_current=True)) == {
        "src.d1.shared", "src.d1.Box", "src.d1.main", "src.d2.shared",
    }
    assert list(ast.Program(context, "kotlin").children()) == [func]


def test_a_whole_program_generates_on_top_of_a_seeded_context():
    ut.random.reset_word_pool()
    ut.random.r.seed(11)
    provider = Generator("kotlin", target_module="src.d1").generate()
    provider_names = {decl.name for decl in provider.children()}

    context = provider.context
    context.prepare_this_context_for_import()
    consumer = Generator("kotlin", target_module="src.d2").generate(context)

    consumer_names = {decl.name for decl in consumer.children()}
    assert consumer_names
    # Only the consumer's own declarations are emitted, and every one of them
    # belongs to the consumer's package.
    assert all(name.startswith("src.d2.") for name in consumer_names)
    assert not consumer_names & provider_names
