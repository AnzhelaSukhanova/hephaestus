import pytest

from src.ir import ast, kotlin_types as kt
from src.ir.context import Context


def _function(name, body=None):
    return ast.FunctionDeclaration(
        name,
        [],
        kt.Integer,
        body or ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.FUNCTION,
    )


def _variable(name):
    return ast.VariableDeclaration(
        name,
        ast.IntegerConstant(1, kt.Integer),
        var_type=kt.Integer,
    )


def _class(name):
    return ast.ClassDeclaration(name, [])


@pytest.mark.parametrize(
    "factory, lookup_bucket",
    [
        (lambda: _function("src.d1.function"), "funcs"),
        (lambda: _class("src.d1.Class"), "classes"),
        (lambda: _variable("src.d1.variable"), "vars"),
    ],
    ids=["function", "class", "variable"],
)
def test_top_level_declarations_have_only_their_canonical_global_indexes(
        factory, lookup_bucket):
    context = Context()
    program = ast.Program(context, "kotlin")
    declaration = factory()

    program.add_declaration(declaration)

    global_entities = context._context[ast.GLOBAL_NAMESPACE]
    locations = [
        (entity, name)
        for entity, bucket in global_entities.items()
        for name, candidate in bucket.items()
        if candidate is declaration
    ]

    assert global_entities[lookup_bucket] == {
        declaration.name: declaration,
    }
    assert global_entities["decls"] == {
        declaration.name: declaration,
    }
    assert set(locations) == {
        (lookup_bucket, declaration.name),
        ("decls", declaration.name),
    }
    assert len(locations) == 2
    assert context.get_namespace(declaration) == ast.GLOBAL_NAMESPACE


def test_distinct_top_level_declarations_keep_distinct_canonical_keys():
    context = Context()
    program = ast.Program(context, "kotlin")
    function = _function("src.d1.function")
    class_decl = _class("src.d1.Class")
    variable = _variable("src.d1.variable")
    declarations = (function, class_decl, variable)

    for declaration in declarations:
        program.add_declaration(declaration)

    global_entities = context._context[ast.GLOBAL_NAMESPACE]
    assert global_entities["decls"] == {
        declaration.name: declaration for declaration in declarations
    }
    assert global_entities["funcs"] == {function.name: function}
    assert global_entities["classes"] == {class_decl.name: class_decl}
    assert global_entities["vars"] == {variable.name: variable}
    assert all(context.get_namespace(declaration) == ast.GLOBAL_NAMESPACE
               for declaration in declarations)

    context.remove_declaration(class_decl)

    assert global_entities["decls"] == {
        function.name: function,
        variable.name: variable,
    }
    assert global_entities["funcs"] == {function.name: function}
    assert global_entities["classes"] == {}
    assert global_entities["vars"] == {variable.name: variable}


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _function("src.d1.function"),
        lambda: _class("src.d1.Class"),
        lambda: _variable("src.d1.variable"),
    ],
    ids=["function", "class", "variable"],
)
def test_remove_declaration_removes_every_current_top_level_index(factory):
    context = Context()
    program = ast.Program(context, "kotlin")
    declaration = factory()
    program.add_declaration(declaration)

    context.remove_declaration(declaration)

    global_entities = context._context[ast.GLOBAL_NAMESPACE]
    assert all(
        candidate is not declaration
        for bucket in global_entities.values()
        for candidate in bucket.values()
    )
    assert context.get_namespace(declaration) is None


def test_class_type_is_derived_from_program_declarations_not_a_context_index():
    context = Context()
    program = ast.Program(context, "kotlin")
    declaration = _class("src.d1.Class")
    program.add_declaration(declaration)

    assert declaration in program.get_types()
    assert declaration not in context.get_types(ast.GLOBAL_NAMESPACE).values()

    context.remove_declaration(declaration)

    assert declaration not in program.get_types()


def test_removing_a_class_keeps_its_child_namespace_and_members():
    context = Context()
    program = ast.Program(context, "kotlin")
    field = ast.FieldDeclaration("field", kt.Integer)
    method = _function("method")
    declaration = ast.ClassDeclaration(
        "src.d1.Class", [], fields=[field], functions=[method])
    program.add_declaration(declaration)
    class_namespace = ast.GLOBAL_NAMESPACE + (declaration.name,)

    context.remove_declaration(declaration)

    assert context.get_namespace(declaration) is None
    assert context.get_vars(class_namespace) == {field.name: field}
    assert context.get_funcs(class_namespace) == {method.name: method}
    assert context.get_namespace(field) == class_namespace
    assert context.get_namespace(method) == class_namespace


def test_removing_a_function_keeps_its_child_namespace_and_members():
    context = Context()
    program = ast.Program(context, "kotlin")
    local = _variable("local")
    nested = _function("nested")
    declaration = _function(
        "src.d1.function",
        ast.Block([local, nested]),
    )
    program.add_declaration(declaration)
    function_namespace = ast.GLOBAL_NAMESPACE + (declaration.name,)

    context.remove_declaration(declaration)

    assert context.get_namespace(declaration) is None
    assert context.get_vars(function_namespace) == {local.name: local}
    assert context.get_funcs(function_namespace) == {nested.name: nested}
    assert context.get_namespace(local) == function_namespace
    assert context.get_namespace(nested) == function_namespace


def test_context_and_namespaces_indexes_remain_bidirectionally_consistent():
    context = Context()
    program = ast.Program(context, "kotlin")
    field = ast.FieldDeclaration("field", kt.Integer)
    method = _function("method")
    class_decl = ast.ClassDeclaration(
        "src.d1.Class", [], fields=[field], functions=[method])
    local = _variable("local")
    nested = _function("nested")
    function = _function(
        "src.d1.function", ast.Block([local, nested]))
    variable = _variable("src.d1.variable")

    for declaration in (class_decl, function, variable):
        program.add_declaration(declaration)

    def assert_indexes_are_tied():
        context_entries = {}
        for namespace, entities in context._context.items():
            for bucket in entities.values():
                for declaration in bucket.values():
                    assert context._namespaces[declaration] == namespace
                    context_entries.setdefault(declaration, set()).add(namespace)

        expected_entries = {
            declaration: {namespace}
            for declaration, namespace in context._namespaces.items()
        }
        assert context_entries == expected_entries

    assert_indexes_are_tied()

    context.remove_declaration(class_decl)
    assert_indexes_are_tied()

    context.remove_declaration(function)
    assert_indexes_are_tied()