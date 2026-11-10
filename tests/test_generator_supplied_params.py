import pickle

import pytest

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


def test_func_return_type_does_not_depend_on_parameter_lookup(monkeypatch):
    monkeypatch.setattr(ut.random, "bool",
                        lambda *_args, **_kwargs: True)

    def resolve_return_type(register_parameter):
        generator = _generator()
        parameter = ast.ParameterDeclaration("value", kt.Integer)

        if register_parameter:
            generator.context.add_var(
                generator.namespace, parameter.name, parameter)
        visible_parameter = generator.context.get_decl(
            generator.namespace, parameter.name)
        return (
            generator._get_func_ret_type([parameter], None),
            visible_parameter,
        )

    registered_type, registered_parameter = resolve_return_type(True)
    unregistered_type, unregistered_parameter = resolve_return_type(False)

    assert registered_parameter is not None
    assert unregistered_parameter is None
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


@pytest.mark.parametrize("target_module", [None, "src.d1", "src.d2"])
def test_same_named_supplied_parameters_have_distinct_function_indexes(
        target_module):
    generator = _generator(target_module)
    parameters = [ast.ParameterDeclaration("value", kt.Integer)
                  for _ in range(2)]
    functions = [
        generator.gen_func_decl(
            func_name=name,
            params=[parameter],
            etype=kt.Integer,
            type_params=[],
            is_interface=True,
            visibility=ast.Visibilities.PUBLIC,
        )
        for name, parameter in zip(("firstSupplied", "secondSupplied"), parameters)
    ]

    assert parameters[0] is not parameters[1]
    assert functions[0] is not functions[1]
    assert functions[0].name != functions[1].name
    for function, parameter in zip(functions, parameters):
        function_namespace = ast.GLOBAL_NAMESPACE + (function.name,)
        assert function.params[0] is parameter
        assert parameter.name == "value"
        assert generator.context.get_decl(
            ast.GLOBAL_NAMESPACE, function.name) is function
        assert generator.context.get_decl(
            function_namespace, "value") is parameter
        assert generator.context.get_vars(
            function_namespace, only_current=True) == {"value": parameter}
        assert generator.context.get_declarations(
            function_namespace, only_current=True) == {"value": parameter}
        assert generator.context.get_namespace(parameter) == function_namespace


def test_override_parameters_are_fresh_and_registered_in_the_new_method():
    generator = _generator("src.d1")
    source_parameter = ast.ParameterDeclaration("value", kt.Integer)
    source_default = ast.IntegerConstant(7, kt.Integer)
    source_parameter.default = source_default
    source_function = ast.FunctionDeclaration(
        "base", [source_parameter], kt.Integer, None,
        ast.FunctionDeclaration.CLASS_METHOD,
        is_final=False,
        visibility=ast.Visibilities.PUBLIC,
    )
    source_class = ast.ClassDeclaration(
        "Base", [], functions=[source_function], is_final=False)
    generator.context.add_declaration(source_class)
    source_snapshot = pickle.dumps(source_parameter)

    generator.target_module = "src.d2"
    generator.context.target_module = "src.d2"
    child_class = ast.ClassDeclaration(
        "Child", [], functions=[], is_final=False)
    generator.context.add_declaration(child_class)
    generator.namespace = ast.GLOBAL_NAMESPACE + (child_class.name,)

    override = generator._gen_func_from_existing(
        source_function, {}, class_is_final=True, is_interface=True)

    override_parameter = override.params[0]
    override_namespace = ast.GLOBAL_NAMESPACE + (
        child_class.name, override.name)
    source_namespace = ast.GLOBAL_NAMESPACE + (
        source_class.name, source_function.name)
    assert override is not source_function
    assert override_parameter is not source_parameter
    assert source_function.params[0] is source_parameter
    assert source_parameter.name == override_parameter.name == "value"
    assert source_parameter.get_type() is kt.Integer
    assert source_parameter.default is source_default
    assert pickle.dumps(source_parameter) == source_snapshot
    assert override_parameter.get_type() == kt.Integer
    assert override_parameter.default is not source_default
    for namespace, parameter in (
            (source_namespace, source_parameter),
            (override_namespace, override_parameter)):
        assert generator.context.get_decl(namespace, "value") is parameter
        assert generator.context.get_vars(namespace, only_current=True) == {
            "value": parameter,
        }
        assert generator.context.get_declarations(
            namespace, only_current=True) == {"value": parameter}
        assert generator.context.get_namespace(parameter) == namespace


def test_signature_generated_parameters_are_registered(monkeypatch):
    generator = _generator("src.d2")
    cls = ast.ClassDeclaration(
        "SignatureHost", [], class_type=ast.ClassDeclaration.INTERFACE,
        functions=[], is_final=False)
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
    assert "." not in parameter.name
    assert generator.context.get_decl(
        function_namespace, parameter.name) is parameter
    assert generator.context.get_vars(
            function_namespace, only_current=True)[parameter.name] is parameter
    assert generator.context.get_declarations(
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