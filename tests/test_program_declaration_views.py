from collections import OrderedDict

import pytest

from src.ir import ast, kotlin_types as kt
from src.ir.context import Context
from src.translators.kotlin import KotlinTranslator


def _function(name, visibility=ast.Visibilities.PUBLIC, body=None):
    return ast.FunctionDeclaration(
        name, [], kt.Integer,
        body if body is not None else ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.FUNCTION, visibility=visibility)


def _program(module, declarations):
    context = Context()
    context.target_module = module
    program = ast.Program(context, "kotlin")
    for declaration in declarations:
        program.add_declaration(declaration)
    return program


def test_current_module_view_matches_exact_module_without_mutation():
    roots = [
        _function("src.d10.neighbor"),
        _function("src.d1.first"),
        _function("src.d1.extra.nestedPackage"),
        _function("bare"),
        _function("src.d1.private", ast.Visibilities.PRIVATE),
        _function("src.d1.last"),
    ]
    program = _program("src.d1", roots)
    indexes = {
        namespace: {bucket: list(entries.items())
                    for bucket, entries in buckets.items()}
        for namespace, buckets in program.context._context.items()
    }
    namespaces = dict(program.context._namespaces)
    trees = [str(root) for root in roots]

    selected = program.get_current_module_declarations()

    assert program.context.name_is_local("bare")
    assert list(selected) == [roots[i].name for i in (1, 2, 3, 4, 5)]
    assert all(selected[root.name] is root
               for root in (roots[1], roots[2], roots[3], roots[4], roots[5]))
    assert list(program.children()) == [roots[i] for i in (1, 2, 3, 4, 5)]
    assert program.get_exported_names() == [roots[i].name for i in (1, 2, 3, 5)]
    assert not hasattr(program, "declarations")
    assert list(program._declarations) == roots
    assert indexes == {
        namespace: {bucket: list(entries.items())
                    for bucket, entries in buckets.items()}
        for namespace, buckets in program.context._context.items()
    }
    assert program.context._namespaces == namespaces
    assert [str(root) for root in roots] == trees


@pytest.mark.parametrize("module", [None, ""])
def test_moduleless_program_keeps_all_roots(module):
    roots = [_function("bare"), _function("src.d1.qualified")]
    program = _program(module, roots)

    assert list(program.get_current_module_declarations().values()) == roots
    assert list(program.children()) == roots
    assert list(program._declarations) == roots
    assert program.get_exported_names() == [root.name for root in roots]


def test_empty_current_module_view_still_exposes_dependency_types():
    dependency = ast.ClassDeclaration("src.d1.Box", [])
    program = _program("src.d2", [dependency])

    assert program.get_current_module_declarations() == {}
    assert list(program.children()) == []
    assert program.get_exported_names() == []
    assert list(program._declarations) == [dependency]
    assert any(known is dependency for known in program.get_types())


def test_empty_context_has_empty_views():
    program = _program("src.d1", [])

    assert program.get_current_module_declarations() == {}
    assert program.get_foreign_module_declarations() == {}
    assert list(program._declarations) == []
    assert list(program.children()) == []


def test_declarations_and_types_bypass_filtered_accessor(monkeypatch):
    dependency = ast.ClassDeclaration("src.d1.Provider", [])
    current = ast.ClassDeclaration("src.d2.Consumer", [])
    program = _program("src.d2", [dependency, current])
    selected = OrderedDict([(current.name, current)])
    monkeypatch.setattr(program, "get_current_module_declarations", lambda: selected)

    assert list(program._declarations) == [dependency, current]
    assert [decl for decl in program.get_types()
            if isinstance(decl, ast.ClassDeclaration)] == [dependency, current]
    assert list(program.children()) == [current]
    assert program.get_exported_names() == [current.name]


def test_program_replacement_preserves_non_current_roots_and_order(monkeypatch):
    provider = _function("src.d1.provider")
    old = _function("src.d2.old")
    ancestor = _function("src.d0.ancestor")
    replaced = _function("src.d2.replaced")
    program = _program("src.d2", [provider, old, ancestor, replaced])
    new = _function("src.d2.new")
    replacement = _function(replaced.name)
    supplied = OrderedDict([(replacement.name, replacement), (new.name, new)])
    original_add = program.context._add_declaration_entity

    def check_registration(namespace, entity, name, value):
        assert value is not provider and value is not ancestor
        return original_add(namespace, entity, name, value)

    monkeypatch.setattr(program.context, "_add_declaration_entity", check_registration)

    program.update_declarations(supplied)

    assert list(program._declarations) == [provider, ancestor, replacement, new]
    assert program.context.get_decl(ast.GLOBAL_NAMESPACE, provider.name) is provider
    assert program.context.get_decl(ast.GLOBAL_NAMESPACE, ancestor.name) is ancestor
    assert program.context.get_namespace(provider) == ast.GLOBAL_NAMESPACE
    assert program.context.get_namespace(ancestor) == ast.GLOBAL_NAMESPACE
    assert program.get_current_module_declarations() == supplied
    assert list(program.children()) == [replacement, new]
    assert old.name not in program.context.get_declarations(ast.GLOBAL_NAMESPACE)
    assert list(supplied.values()) == [replacement, new]

    program.update_declarations(OrderedDict())

    assert list(program._declarations) == [provider, ancestor]
    assert list(program.children()) == []


def test_foreign_module_view_preserves_order_and_identity():
    roots = [
        _function("src.d1.provider"),
        _function("src.d2.current"),
        _function("src.d1.deep.nested"),
        _function("src.d10.nearby"),
        _function("bare"),
    ]
    program = _program("src.d2", roots)

    foreign = program.get_foreign_module_declarations()

    assert list(foreign) == [root.name for root in (roots[0], roots[2],
                                                     roots[3])]
    assert list(foreign.values()) == [roots[0], roots[2], roots[3]]


@pytest.mark.parametrize("module", [None, ""])
def test_moduleless_program_has_no_foreign_roots(module):
    program = _program(module, [_function("bare"),
                                _function("src.d1.provider")])

    assert program.get_foreign_module_declarations() == {}


@pytest.mark.parametrize("module", [None, ""])
def test_moduleless_replacement_replaces_entire_view(module):
    program = _program(module, [_function("old"), _function("src.d1.provider")])
    replacement = _function("replacement")

    program.update_declarations(OrderedDict([(replacement.name, replacement)]))

    assert list(program._declarations) == [replacement]
    assert list(program.children()) == [replacement]


def test_program_replacement_uses_selector_keys(monkeypatch):
    retained = _function("src.d2.retained")
    replaced = _function("src.d2.replaced")
    program = _program("src.d2", [retained, replaced])
    monkeypatch.setattr(program, "get_foreign_module_declarations",
                        lambda: {retained.name: retained})

    program.update_declarations({})

    assert list(program._declarations) == [retained]


def test_context_replacement_remains_a_full_index_operation():
    provider = _function("src.d1.provider")
    current = _function("src.d2.current")
    program = _program("src.d2", [provider, current])
    replacement = OrderedDict([(provider.name, provider)])

    program.context.update_declarations(replacement)

    assert program.context._context[ast.GLOBAL_NAMESPACE]["decls"] is replacement
    assert list(program._declarations) == [provider]
    assert list(program.children()) == []


def test_kotlin_emits_only_consumer_roots_with_qualified_dependency_references():
    provider = _function("src.d1.provider")
    provider_class = ast.ClassDeclaration("src.d1.Provider", [])
    provider_var = ast.VariableDeclaration(
        "src.d1.value", ast.IntegerConstant(2, kt.Integer), var_type=kt.Integer)
    caller = _function("src.d2.caller", body=ast.FunctionCall(provider.name, []))
    reader = _function("src.d2.reader", body=ast.Variable(provider_var.name))
    private = _function("src.d2.hidden", ast.Visibilities.PRIVATE)
    program = _program("src.d2", [provider, provider_class, provider_var,
                                  caller, reader, private])

    translator = KotlinTranslator("src.d2")
    translator.visit(program)
    source = translator.result()

    assert source.startswith("package src.d2\n")
    assert "fun caller(" in source
    assert "fun reader(" in source
    assert "private fun hidden(" in source
    assert "src.d1.provider()" in source
    assert "src.d1.value" in source
    assert "fun provider(" not in source
    assert "class Provider" not in source
    assert "val value" not in source
    assert "fun src.d1." not in source
    assert "class src.d1." not in source
    assert "val src.d1." not in source
    assert program.get_exported_names() == [caller.name, reader.name]