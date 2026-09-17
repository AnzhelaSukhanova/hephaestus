from collections import OrderedDict
import pickle

import pytest

from src import utils as ut
from src.generators.config import cfg
from src.generators.generator import Generator
from src.ir import ast, kotlin_types as kt, types
from src.ir.context import Context
from src.ir.visitors import DefaultVisitorUpdate


def _function(name, params=None, body=None):
    return ast.FunctionDeclaration(
        name,
        list(params or []),
        kt.Integer,
        body or ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.FUNCTION,
        visibility=ast.Visibilities.PUBLIC,
    )


def _variable(name):
    return ast.VariableDeclaration(
        name,
        ast.IntegerConstant(1, kt.Integer),
        var_type=kt.Integer,
    )


def _registered_declarations(context):
    declarations = {}
    for entities in context._context.values():
        for bucket in entities.values():
            for declaration in bucket.values():
                if isinstance(declaration, ast.Declaration):
                    declarations[id(declaration)] = declaration
    return tuple(declarations.values())


def _tree_nodes(roots):
    nodes = {}
    pending = list(roots)
    while pending:
        node = pending.pop()
        if id(node) in nodes:
            continue
        nodes[id(node)] = node
        pending.extend(node.children())
    return tuple(nodes.values())


def test_registration_indexes_top_level_and_nested_declarations():
    context = Context()
    context.target_module = "src.d1"

    parameter = ast.ParameterDeclaration("parameter", kt.Integer)
    local = _variable("local")
    nested_parameter = ast.ParameterDeclaration("nestedParameter", kt.Integer)
    nested = _function("nested", [nested_parameter])
    function = _function(
        "src.d1.function",
        [parameter],
        ast.Block([local, nested]),
    )

    field = ast.FieldDeclaration("field", kt.Integer)
    method_parameter = ast.ParameterDeclaration("methodParameter", kt.Integer)
    method = _function("method", [method_parameter])
    class_decl = ast.ClassDeclaration(
        "src.d1.Box", [], fields=[field], functions=[method])
    variable = _variable("src.d1.variable")

    program = ast.Program(context, "kotlin")
    for declaration in (function, class_decl, variable):
        program.add_declaration(declaration)

    function_namespace = ast.GLOBAL_NAMESPACE + (function.name,)
    class_namespace = ast.GLOBAL_NAMESPACE + (class_decl.name,)
    declarations = (
        (function, ast.GLOBAL_NAMESPACE),
        (parameter, function_namespace),
        (local, function_namespace),
        (nested, function_namespace),
        (nested_parameter, function_namespace + (nested.name,)),
        (class_decl, ast.GLOBAL_NAMESPACE),
        (field, class_namespace),
        (method, class_namespace),
        (method_parameter, class_namespace + (method.name,)),
        (variable, ast.GLOBAL_NAMESPACE),
    )
    assert list(program._declarations) == [function, class_decl, variable]
    for declaration, namespace in declarations:
        assert context.get_decl(namespace, declaration.name) is declaration
        assert context.get_declarations(
            namespace, only_current=True)[declaration.name] is declaration
        assert context.get_namespace(declaration) == namespace
        if namespace != ast.GLOBAL_NAMESPACE:
            assert "." not in declaration.name


@pytest.mark.parametrize("remove_first", [False, True])
def test_repeated_current_module_registration_preserves_indexes(remove_first):
    context = Context()
    context.target_module = "src.d1"
    parameter = ast.ParameterDeclaration("value", kt.Integer)
    declaration = _function("src.d1.function", [parameter])
    context.add_declaration(declaration)

    if remove_first:
        context.remove_declaration(declaration)
    context.add_declaration(declaration)

    namespace = ast.GLOBAL_NAMESPACE + (declaration.name,)
    assert context.get_decl(ast.GLOBAL_NAMESPACE, declaration.name) is declaration
    assert context.get_declarations(
        ast.GLOBAL_NAMESPACE, only_current=True) == {
            declaration.name: declaration,
        }
    assert context.get_namespace(declaration) == ast.GLOBAL_NAMESPACE
    assert context.get_decl(namespace, parameter.name) is parameter
    assert context.get_declarations(namespace, only_current=True) == {
        parameter.name: parameter,
    }
    assert context.get_vars(namespace, only_current=True) == {
        parameter.name: parameter,
    }
    assert context.get_namespace(parameter) == namespace


def test_crossmodule_reuse_keeps_provider_lookup_separate_from_consumer_decls():
    context = Context()
    context.target_module = "src.d1"
    parameter = ast.ParameterDeclaration("value", kt.Integer)
    provider = _function("src.d1.provider", [parameter])
    context.add_declaration(provider)

    context.prepare_this_context_for_import()
    context.target_module = "src.d2"
    consumer = _function("src.d2.consumer")
    context.add_declaration(consumer)

    program = ast.Program(context, "kotlin")
    assert context.get_funcs(ast.GLOBAL_NAMESPACE)[provider.name] is provider
    assert context.get_decl(ast.GLOBAL_NAMESPACE, provider.name) is provider
    assert context.get_declarations(
        ast.GLOBAL_NAMESPACE, only_current=True) == {
            provider.name: provider,
            consumer.name: consumer,
        }
    assert any(declaration is provider
               for declaration in program._declarations)
    assert program.get_current_module_declarations() == {
        consumer.name: consumer,
    }
    assert list(program.children()) == [consumer]
    namespace = ast.GLOBAL_NAMESPACE + (provider.name,)
    assert context.get_decl(namespace, parameter.name) is parameter
    assert context.get_namespace(parameter) == namespace


@pytest.mark.parametrize(
    "target_module, selected_names",
    [
        (None, None),
        ("", None),
        ("src.d1", ("bare", "src.d1.current", "src.d1.deep.nested")),
        ("src.d1.deep", ("bare",)),
        ("src.d2", ("bare", "src.d2.provider")),
        ("src.missing", ("bare",)),
    ],
)
def test_program_selection_uses_context_module_locality(
        target_module, selected_names):
    context = Context()
    names = (
        "bare", "src.d1.current", "src.d10.nearby", "src.d1.deep.nested",
        "src.d2.provider", "src.d1",
    )
    declarations = OrderedDict((name, _function(name)) for name in names)
    for declaration in declarations.values():
        context.add_declaration(declaration)
    context.target_module = target_module
    program = ast.Program(context, "kotlin")

    expected = OrderedDict(
        (name, declarations[name])
        for name in (names if selected_names is None else selected_names)
    )
    assert list(program._declarations) == list(declarations.values())
    assert program.get_current_module_declarations() == expected
    assert list(program.children()) == list(expected.values())
    assert expected == {
        key: declaration for key, declaration in declarations.items()
        if context.name_is_local(key)
    }

    selected_context = Context()
    selected_context.target_module = target_module
    for name in expected:
        selected_context.add_declaration(_function(name))
    selected_program = ast.Program(selected_context, "kotlin")
    assert program.get_exported_names() == selected_program.get_exported_names()


def test_update_declarations_replaces_current_roots_and_retains_provider_indexes():
    context = Context()
    context.target_module = "src.d1"
    provider_parameter = ast.ParameterDeclaration("value", kt.Integer)
    provider = _function("src.d1.provider", [provider_parameter])
    context.add_declaration(provider)
    context.prepare_this_context_for_import()
    context.target_module = "src.d2"
    existing = _function("src.d2.existing")
    context.add_declaration(existing)
    parameter = ast.ParameterDeclaration("value", kt.Integer)
    replacement = _function("src.d2.replacement", [parameter])
    context.add_declaration(replacement)
    program = ast.Program(context, "kotlin")

    for _ in range(2):
        program.update_declarations(
            OrderedDict([(replacement.name, replacement)]))

        assert list(program._declarations) == [provider, replacement]
        assert any(declaration is provider
                   for declaration in program._declarations)
        assert program.get_current_module_declarations() == {
            replacement.name: replacement,
        }
        assert context.get_decl(ast.GLOBAL_NAMESPACE, existing.name) is None
        for declaration in (provider, replacement):
            assert context.get_decl(
                ast.GLOBAL_NAMESPACE, declaration.name) is declaration
            assert context.get_namespace(declaration) == ast.GLOBAL_NAMESPACE
            namespace = ast.GLOBAL_NAMESPACE + (declaration.name,)
            assert context.get_decl(
                namespace, declaration.params[0].name) is declaration.params[0]
            assert context.get_declarations(namespace, only_current=True) == {
                declaration.params[0].name: declaration.params[0],
            }
            assert context.get_namespace(declaration.params[0]) == namespace


def test_crossmodule_generation_never_reregisters_provider_declarations(
        monkeypatch):
    # theorem registration_isolation (d) : d ∈ loaded_context →
    #     ¬register_consumer d := by exact identity_guard
    # Retained provider indexes support lookup, not consumer registration.
    original_add = Context._add_declaration_entity
    protected = ()
    protected_ids = set()
    registered = {}
    registration_counts = {}

    def check_registration(context, namespace, entity, name, value):
        if isinstance(value, ast.Declaration):
            assert id(value) not in protected_ids, (
                "provider declaration was registered in a consumer")
            registered[id(value)] = value
            registration_counts[id(value)] = registration_counts.get(
                id(value), 0) + 1
        return original_add(context, namespace, entity, name, value)

    class RecordingIntegerUpdate(DefaultVisitorUpdate):
        def __init__(self):
            super().__init__()
            self.replacements = {}

        def visit_integer_constant(self, node):
            replacement = ast.IntegerConstant(2, kt.Integer)
            self.replacements[id(node)] = node, replacement
            return replacement

    monkeypatch.setattr(
        Context, "_add_declaration_entity", check_registration)
    monkeypatch.setattr(cfg.limits, "min_top_level", 2)
    monkeypatch.setattr(cfg.limits, "max_top_level", 3)
    monkeypatch.setattr(cfg.limits, "max_depth", 3)
    monkeypatch.setattr(cfg.limits, "inline_default_depth", 2)
    monkeypatch.setattr(cfg.limits, "ordinary_default_depth", 2)
    monkeypatch.setattr(cfg.limits.cls, "max_funcs", 1)
    monkeypatch.setattr(cfg.limits.cls, "max_fields", 1)
    monkeypatch.setattr(
        cfg.prob.top_level_declarations, "function_declaration", 0.5)
    monkeypatch.setattr(
        cfg.prob.top_level_declarations, "class_declaration", 0.5)
    monkeypatch.setattr(
        cfg.prob.top_level_declarations, "variable_declaration", 0.0)

    random_state = ut.random.r.getstate()
    word_pool = set(ut.random.WORDS)
    try:
        ut.random.r.seed(0)
        program = None
        modules = ("src.d1", "src.d2", "src.d3")
        for index, module in enumerate(modules):
            registered.clear()
            registration_counts.clear()
            provider_roots = {}
            if program is None:
                context = None
                protected = ()
                protected_ids = set()
            else:
                program = pickle.loads(pickle.dumps(program))
                context = program.context
                protected = _registered_declarations(context)
                protected_ids = {id(declaration) for declaration in protected}
                assert protected
                root_ids = {
                    id(declaration) for declaration in program._declarations
                }
                assert protected_ids - root_ids
                context.prepare_this_context_for_import()
                provider_roots = dict(context.get_declarations(
                    ast.GLOBAL_NAMESPACE, only_current=True))
                assert provider_roots
                for previous_module in modules[:index]:
                    name = previous_module + ".isolationIntegerProbe"
                    assert name in provider_roots
                    assert id(provider_roots[name].params[0]) in protected_ids

            ut.random.reset_word_pool()
            program = Generator(
                "kotlin",
                target_module=module,
            ).generate(context=context)
            assert registered
            assert program.get_current_module_declarations()

            # A public literal probe makes traversal coverage non-vacuous for
            # every random module, without replacing real generator coverage.
            probe = _function(
                module + ".isolationIntegerProbe",
                [ast.ParameterDeclaration("value", kt.Integer)],
            )
            program.add_declaration(probe)
            program.add_declaration(probe)
            assert registered[id(probe)] is probe
            assert registered[id(probe.params[0])] is probe.params[0]
            assert registration_counts[id(probe)] >= 2

            root_declarations = program.context.get_declarations(
                ast.GLOBAL_NAMESPACE)
            selected = {
                key: declaration
                for key, declaration in root_declarations.items()
                if program.context.name_is_local(key)
            }
            assert selected
            assert program.get_current_module_declarations() == selected
            assert list(program.children()) == list(selected.values())
            for name, declaration in provider_roots.items():
                assert any(
                    candidate is declaration
                    for candidate in program._declarations
                )

            # theorem traversal_isolation :
            #     visit(program) = visit(selected_roots) := by exact key_filter
            # Consumer leaves change; retained provider trees stay intact.
            provider_snapshot = pickle.dumps(protected)
            provider_constants = {
                id(node) for node in _tree_nodes(protected)
                if isinstance(node, ast.IntegerConstant)
            }
            consumer_constants = {
                id(node): node for node in _tree_nodes(program.children())
                if isinstance(node, ast.IntegerConstant)
            }
            assert consumer_constants
            if index:
                assert provider_constants
            assert not provider_constants.intersection(consumer_constants)

            visitor = RecordingIntegerUpdate()
            program.accept(visitor)

            assert set(visitor.replacements) == set(consumer_constants)
            assert not provider_constants.intersection(visitor.replacements)
            updated_constants = {
                id(node) for node in _tree_nodes(program.children())
                if isinstance(node, ast.IntegerConstant)
            }
            assert not updated_constants.intersection(consumer_constants)
            assert all(
                original is consumer_constants[key]
                and id(replacement) in updated_constants
                for key, (original, replacement) in visitor.replacements.items()
            )
            assert pickle.dumps(protected) == provider_snapshot
            assert program.get_current_module_declarations() == selected
            for name, declaration in provider_roots.items():
                assert any(
                    candidate is declaration
                    for candidate in program._declarations
                )
    finally:
        ut.random.r.setstate(random_state)
        ut.random.WORDS = word_pool


@pytest.mark.parametrize(
    "add, bucket, factory",
    [
        (Context.add_type, "types", lambda: types.TypeParameter("T")),
        (Context.add_lambda, "lambdas", lambda: ast.Lambda(
            "lambda_1", [], kt.Integer, ast.IntegerConstant(1, kt.Integer),
            types.ParameterizedType(kt.FunctionType(0), [kt.Integer]))),
    ],
    ids=["type_parameter", "lambda"],
)
def test_non_declaration_registration_only_indexes_the_entity(
        add, bucket, factory):
    context = Context()
    context.target_module = "src.d1"
    entity = factory()

    add(context, ast.GLOBAL_NAMESPACE, entity.name, entity)

    assert context.get_namespace(entity) == ast.GLOBAL_NAMESPACE
    assert context.get_decl(ast.GLOBAL_NAMESPACE, entity.name) is None
    assert context.get_declarations(
        ast.GLOBAL_NAMESPACE, only_current=True) == {}
    assert context._context[ast.GLOBAL_NAMESPACE][bucket] == {
        entity.name: entity,
    }
    assert all(not entries for name, entries in
               context._context[ast.GLOBAL_NAMESPACE].items() if name != bucket)