from src.generators.generator import Generator
from src.ir import ast, kotlin_types as kt
from src.ir import types as tp
from src.ir.context import Context
from src import utils as ut


def _generator(target_module=None):
    generator = Generator("kotlin", target_module=target_module)
    generator.context = Context()
    generator.context.target_module = target_module
    return generator


def test_supplied_parameter_registration_does_not_change_return_type(
        monkeypatch):
    monkeypatch.setattr(ut.random, "bool",
                        lambda *_args, **_kwargs: True)

    def generate_function(skip_early_registration):
        generator = _generator()
        parameter = ast.ParameterDeclaration("value", kt.Integer)

        if skip_early_registration:
            add_node_to_parent = generator._add_node_to_parent

            def add_without_parameter(parent_namespace, node):
                if not isinstance(node, ast.ParameterDeclaration):
                    add_node_to_parent(parent_namespace, node)

            monkeypatch.setattr(
                generator, "_add_node_to_parent", add_without_parameter)

        function = generator.gen_func_decl(
            func_name="supplied",
            params=[parameter],
            etype=None,
            type_params=[],
            is_interface=True,
            visibility=ast.Visibilities.PUBLIC,
        )
        return function.ret_type

    registered_type = generate_function(
        skip_early_registration=False)
    unregistered_type = \
        generate_function(skip_early_registration=True)

    assert registered_type is unregistered_type is kt.Integer


def test_supplied_parameter_remains_available_during_body_and_after_generation(
        monkeypatch):
    generator = _generator()
    parameter = ast.ParameterDeclaration("value", kt.Integer)
    observed = {}

    def observe_body(ret_type, func):
        observed["decl"] = generator.context.get_decl(
            generator.namespace, parameter.name)
        return ast.IntegerConstant(1, kt.Integer)

    monkeypatch.setattr(generator, "_gen_func_body", observe_body)

    function = generator.gen_func_decl(
        func_name="supplied",
        params=[parameter],
        etype=kt.Integer,
        type_params=[],
        visibility=ast.Visibilities.PUBLIC,
    )

    function_namespace = ast.GLOBAL_NAMESPACE + (function.name,)
    assert observed["decl"] is parameter
    assert generator.context.get_decl(
        ast.GLOBAL_NAMESPACE, function.name) is function
    assert generator.context.get_vars(function_namespace, only_current=True) == {
        parameter.name: parameter,
    }
    assert generator.context.get_declarations(
            function_namespace, only_current=True) == {
        parameter.name: parameter,
    }
    assert generator.context.get_namespace(parameter) == function_namespace


def test_supplied_parameter_gets_target_module_owner_and_indexes():
    generator = _generator("src.d2")
    parameter = ast.ParameterDeclaration("value", kt.Integer)

    function = generator.gen_func_decl(
        func_name="supplied",
        params=[parameter],
        etype=kt.Integer,
        type_params=[],
        is_interface=True,
        visibility=ast.Visibilities.PUBLIC,
    )

    function_namespace = ast.GLOBAL_NAMESPACE + (function.name,)
    assert function.owner_module == "src.d2"
    assert parameter.owner_module == "src.d2"
    assert generator.context.get_vars(function_namespace,
                                      only_current=True)[parameter.name] \
        is parameter
    assert generator.context.get_declarations(
            function_namespace, only_current=True)[parameter.name] is parameter
    assert generator.context.get_namespace(parameter) == function_namespace


def test_override_parameters_are_fresh_and_registered_in_the_new_method():
    generator = _generator("src.d1")
    source_parameter = ast.ParameterDeclaration("value", kt.Integer)
    source_function = ast.FunctionDeclaration(
        "base", [source_parameter], kt.Integer, None,
        ast.FunctionDeclaration.CLASS_METHOD,
        is_final=False,
        visibility=ast.Visibilities.PUBLIC,
    )
    source_class = ast.ClassDeclaration(
        "Base", [], functions=[source_function], is_final=False)
    generator.context.add_declaration(source_class)

    generator.target_module = "src.d2"
    generator.context.target_module = "src.d2"
    child_class = ast.ClassDeclaration("Child", [], is_final=False)
    generator.context.add_declaration(child_class)
    generator.namespace = ast.GLOBAL_NAMESPACE + (child_class.name,)

    override = generator._gen_func_from_existing(
        source_function, {}, class_is_final=True, is_interface=True)

    override_parameter = override.params[0]
    override_namespace = ast.GLOBAL_NAMESPACE + (
        child_class.name, override.name)
    source_namespace = ast.GLOBAL_NAMESPACE + (
        source_class.name, source_function.name)
    assert override_parameter is not source_parameter
    assert source_parameter.owner_module == "src.d1"
    assert override_parameter.owner_module == "src.d2"
    assert generator.context.get_vars(
            override_namespace, only_current=True)[override_parameter.name] \
        is override_parameter
    assert generator.context.get_namespace(override_parameter) == \
        override_namespace
    assert generator.context.get_namespace(source_parameter) == source_namespace


def test_signature_generated_parameters_are_registered(monkeypatch):
    generator = _generator("src.d2")
    cls = ast.ClassDeclaration(
        "SignatureHost", [], class_type=ast.ClassDeclaration.INTERFACE,
        is_final=False)
    generator.context.add_declaration(cls)
    generator.namespace = ast.GLOBAL_NAMESPACE + (cls.name,)
    monkeypatch.setattr(ut.random, "integer", lambda *_args, **_kwargs: 0)
    signature = tp.ParameterizedType(
        generator.bt_factory.get_function_type(1), [kt.Integer, kt.String])

    functions = generator.gen_class_functions(cls, None, signature=signature)

    function = functions[0]
    parameter = function.params[0]
    function_namespace = ast.GLOBAL_NAMESPACE + (cls.name, function.name)
    assert parameter.get_type() is kt.Integer
    assert function.ret_type is kt.String
    assert function.owner_module == "src.d2"
    assert parameter.owner_module == "src.d2"
    assert generator.context.get_vars(
            function_namespace, only_current=True)[parameter.name] is parameter
    assert generator.context.get_namespace(parameter) == function_namespace


def test_supplied_parameter_is_available_during_default_generation(
        monkeypatch):
    generator = _generator("src.d2")
    parameter = ast.ParameterDeclaration("value", kt.Integer)
    parameter._pending_default = True
    observed = {}

    def observe_default(etype, *args, **kwargs):
        observed["decl"] = generator.context.get_decl(
            generator.declaration_namespace, parameter.name)
        observed["namespace"] = generator.context.get_namespace(parameter)
        return ast.IntegerConstant(1, kt.Integer)

    monkeypatch.setattr(generator, "generate_expr", observe_default)
    function = generator.gen_func_decl(
        func_name="withDefault",
        params=[parameter],
        etype=kt.Integer,
        type_params=[],
        is_interface=True,
        visibility=ast.Visibilities.PUBLIC,
    )

    function_namespace = ast.GLOBAL_NAMESPACE + (function.name,)
    assert observed == {
        "decl": parameter,
        "namespace": function_namespace,
    }
    assert isinstance(parameter.default, ast.IntegerConstant)