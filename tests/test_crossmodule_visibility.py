from src.ir import ast, kotlin_types as kt
from src.ir.context import Context


def _function(name, visibility):
    return ast.FunctionDeclaration(
        name,
        [],
        kt.Integer,
        ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.FUNCTION,
        visibility=visibility,
    )


def test_visibility_boundary_predicate_resolves_visibility_before_checking():
    assert ast.Visibilities.PUBLIC.can_cross_module_boundary()
    assert not ast.Visibilities.PRIVATE.can_cross_module_boundary()
    assert ast.Visibilities.UNKNOWN.can_cross_module_boundary()


def test_export_list_and_import_lookup_use_the_same_visibility_rule():
    context = Context()
    context.target_module = "src.d1"
    program = ast.Program(context, "kotlin")

    public_function = _function("src.d1.publicFunction",
                                ast.Visibilities.PUBLIC)
    private_function = _function("src.d1.privateFunction",
                                 ast.Visibilities.PRIVATE)
    public_class = ast.ClassDeclaration("src.d1.PublicClass", [])
    private_class = ast.ClassDeclaration("src.d1.PrivateClass", [])
    private_class.visibility = ast.Visibilities.PRIVATE
    public_var = ast.VariableDeclaration(
        "src.d1.publicVar",
        ast.IntegerConstant(1, kt.Integer),
        var_type=kt.Integer,
    )
    private_var = ast.VariableDeclaration(
        "src.d1.privateVar",
        ast.IntegerConstant(1, kt.Integer),
        var_type=kt.Integer,
    )
    private_var.visibility = ast.Visibilities.PRIVATE

    for declaration in (public_function, private_function, public_class,
                        private_class,
                        public_var, private_var):
        program.add_declaration(declaration)

    assert program.get_exported_names() == [
        "src.d1.publicFunction", "src.d1.PublicClass", "src.d1.publicVar",
    ]

    context.prepare_this_context_for_import()
    context.target_module = "src.d2"

    assert context.get_declarations(ast.GLOBAL_NAMESPACE,
                                    only_current=True) == {
        public_function.name: public_function,
        public_class.name: public_class,
        public_var.name: public_var,
    }
    assert list(program.children()) == []
    assert program.get_exported_names() == []
    assert context.get_funcs(ast.GLOBAL_NAMESPACE) == {
        public_function.name: public_function,
    }
    assert context.get_classes(ast.GLOBAL_NAMESPACE) == {
        public_class.name: public_class,
    }
    assert context.get_vars(ast.GLOBAL_NAMESPACE) == {
        public_var.name: public_var,
    }
    for declaration in (private_function, private_class, private_var):
        assert context.get_decl(ast.GLOBAL_NAMESPACE,
                                declaration.name) is None


def test_context_owns_declaration_registration_and_program_delegates():
    context = Context()
    program = ast.Program(context, "kotlin")
    field = ast.FieldDeclaration("field", kt.Integer)
    method = _function("method", ast.Visibilities.PUBLIC)
    class_decl = ast.ClassDeclaration(
        "src.d1.Box", [], fields=[field], functions=[method])

    context.add_declaration(class_decl)

    assert context.get_classes(ast.GLOBAL_NAMESPACE) == {
        class_decl.name: class_decl,
    }
    class_namespace = ast.GLOBAL_NAMESPACE + (class_decl.name,)
    assert context.get_vars(class_namespace) == {field.name: field}
    assert context.get_funcs(class_namespace) == {method.name: method}

    program.remove_declaration(class_decl)

    assert context.get_classes(ast.GLOBAL_NAMESPACE) == {}
    assert context.get_declarations(ast.GLOBAL_NAMESPACE,
                                    only_current=True) == {}


def test_import_visibility_cleanup_retains_visible_roots_from_all_modules():
    context = Context()
    context.target_module = "src.d2"
    program = ast.Program(context, "kotlin")
    retained = []
    removed = []
    for module in ("src.d0", "src.d1", "src.d2"):
        for visibility in (ast.Visibilities.PUBLIC, ast.Visibilities.PRIVATE):
            declaration = _function(module + "." + visibility.name, visibility)
            program.add_declaration(declaration)
            (retained if visibility is ast.Visibilities.PUBLIC else removed).append(
                declaration)
    global_entities = context._context[ast.GLOBAL_NAMESPACE]
    declarations_bucket = global_entities["decls"]

    context.prepare_this_context_for_import()
    context.target_module = "src.d3"

    assert global_entities["decls"] is declarations_bucket
    assert list(program._declarations) == retained
    assert list(context.get_funcs(ast.GLOBAL_NAMESPACE).values()) == retained
    assert list(program.children()) == []
    for declaration in retained:
        assert context.get_decl(ast.GLOBAL_NAMESPACE, declaration.name) is declaration
        assert context.get_namespace(declaration) == ast.GLOBAL_NAMESPACE
    for declaration in removed:
        assert context.get_decl(ast.GLOBAL_NAMESPACE, declaration.name) is None
        assert context.get_namespace(declaration) is None

    indexes = {name: dict(bucket) for name, bucket in global_entities.items()}
    namespaces = dict(context._namespaces)
    context.prepare_this_context_for_import()

    assert global_entities == indexes
    assert context._namespaces == namespaces
    assert global_entities["decls"] is declarations_bucket