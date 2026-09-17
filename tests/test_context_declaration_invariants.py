import pytest

from src.ir import ast, kotlin_types as kt, types
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


def test_context_registration_paths_never_pass_none_to_add_entity(monkeypatch):
    # theorem registration_non_null (value : Entity) :
    #     registered_by_context(value) → value ≠ None := by
    #   every declaration, type parameter, and lambda registration path
    #   constructs or receives a concrete entity before indexing it.
    observed = []
    original_add_entity = Context._add_entity

    def observe_registration(context, namespace, entity, name, value):
        assert value is not None
        observed.append(value)
        return original_add_entity(context, namespace, entity, name, value)

    monkeypatch.setattr(Context, "_add_entity", observe_registration)

    context = Context()
    parameter = ast.ParameterDeclaration("parameter", kt.Integer)
    local = _variable("local")
    nested = _function("nested")
    function = ast.FunctionDeclaration(
        "src.d1.function",
        [parameter],
        kt.Integer,
        ast.Block([local, nested]),
        ast.FunctionDeclaration.FUNCTION,
    )
    field = ast.FieldDeclaration("field", kt.Integer)
    method = _function("method")
    class_decl = ast.ClassDeclaration(
        "src.d1.Class", [], fields=[field], functions=[method])
    variable = _variable("src.d1.variable")
    type_parameter = types.TypeParameter("T")
    lambda_decl = ast.Lambda(
        "lambda_1", [], kt.Integer, ast.IntegerConstant(1, kt.Integer),
        types.ParameterizedType(kt.FunctionType(0), [kt.Integer]))

    for declaration in (function, class_decl, variable):
        context.add_declaration(declaration)
    context.add_type(ast.GLOBAL_NAMESPACE, type_parameter.name, type_parameter)
    context.add_lambda(ast.GLOBAL_NAMESPACE, lambda_decl.name, lambda_decl)

    assert observed
    assert all(value is not None for value in observed)


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


@pytest.mark.parametrize(
    "factory, lookup_bucket",
    [
        (lambda name: _function(name), "funcs"),
        (lambda name: _class(name), "classes"),
        (lambda name: _variable(name), "vars"),
    ],
    ids=["function", "class", "variable"],
)
def test_prepare_import_composes_with_top_level_declaration_invariants(
        factory, lookup_bucket):
    context = Context()
    context.target_module = "src.d1"
    program = ast.Program(context, "kotlin")
    public = factory("src.d1.public")
    private = factory("src.d1.private")
    public.visibility = ast.Visibilities.PUBLIC
    private.visibility = ast.Visibilities.PRIVATE

    program.add_declaration(public)
    program.add_declaration(private)

    global_entities = context._context[ast.GLOBAL_NAMESPACE]
    assert global_entities[lookup_bucket] == {
        public.name: public,
        private.name: private,
    }
    assert global_entities["decls"] == {
        public.name: public,
        private.name: private,
    }
    assert context.get_namespace(public) == ast.GLOBAL_NAMESPACE
    assert context.get_namespace(private) == ast.GLOBAL_NAMESPACE
    assert program.get_exported_names() == [public.name]

    context.prepare_this_context_for_import()
    context.target_module = "src.d2"

    assert list(program.children()) == []
    assert global_entities["decls"] == {public.name: public}
    assert global_entities[lookup_bucket] == {public.name: public}
    assert context.get_namespace(public) == ast.GLOBAL_NAMESPACE
    assert context.get_namespace(private) is None
    assert context.get_decl(ast.GLOBAL_NAMESPACE, public.name) is public
    assert context.get_decl(ast.GLOBAL_NAMESPACE, private.name) is None


def test_prepare_import_preserves_nested_indexes_when_removing_private_roots():
    context = Context()
    context.target_module = "src.d1"
    program = ast.Program(context, "kotlin")
    field = ast.FieldDeclaration("field", kt.Integer)
    method = _function("method")
    private_class = ast.ClassDeclaration(
        "src.d1.PrivateClass", [], fields=[field], functions=[method])
    private_class.visibility = ast.Visibilities.PRIVATE
    local = _variable("local")
    nested = _function("nested")
    private_function = _function(
        "src.d1.privateFunction", ast.Block([local, nested]))
    private_function.visibility = ast.Visibilities.PRIVATE

    program.add_declaration(private_class)
    program.add_declaration(private_function)
    class_namespace = ast.GLOBAL_NAMESPACE + (private_class.name,)
    function_namespace = ast.GLOBAL_NAMESPACE + (private_function.name,)

    context.prepare_this_context_for_import()
    context.target_module = "src.d2"

    assert list(program.children()) == []
    assert context.get_classes(ast.GLOBAL_NAMESPACE) == {}
    assert context.get_funcs(ast.GLOBAL_NAMESPACE) == {}
    assert context.get_namespace(private_class) is None
    assert context.get_namespace(private_function) is None
    assert context.get_vars(class_namespace) == {field.name: field}
    assert context.get_funcs(class_namespace) == {method.name: method}
    assert context.get_vars(function_namespace) == {local.name: local}
    assert context.get_funcs(function_namespace) == {nested.name: nested}
    assert context.get_namespace(field) == class_namespace
    assert context.get_namespace(method) == class_namespace
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