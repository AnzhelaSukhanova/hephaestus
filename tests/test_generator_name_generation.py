from src.generators import utils as generator_utils
from src.generators.generator import Generator
from src.ir import ast, kotlin_types as kt
from src.ir.context import Context
from src import utils as ut


def _generator(target_module="src.d2"):
    generator = Generator("kotlin", target_module=target_module)
    generator.context = Context()
    return generator


def test_gen_name_qualifies_generated_global_names(monkeypatch):
    words = {
        "lower": "helper",
        "capitalize": "Helper",
        None: "word",
    }
    monkeypatch.setattr(
        generator_utils,
        "gen_identifier",
        lambda ident_type=None: words[ident_type],
    )
    generator = _generator()

    assert generator._gen_name("lower") == "src.d2.helper"
    assert generator._gen_name("capitalize") == "src.d2.Helper"
    assert generator._gen_name() == "src.d2.word"


def test_gen_name_leaves_names_bare_without_target_module(monkeypatch):
    monkeypatch.setattr(
        generator_utils,
        "gen_identifier",
        lambda _ident_type=None: "value",
    )
    generator = _generator(target_module=None)

    assert generator._gen_name("lower") == "value"


def test_gen_name_does_not_double_prefix_supplied_name():
    generator = _generator()

    assert generator._gen_name(name="src.d2.value") == "src.d2.value"


def test_gen_name_keeps_nested_and_parameter_names_bare(monkeypatch):
    monkeypatch.setattr(
        generator_utils,
        "gen_identifier",
        lambda _ident_type=None: "value",
    )
    generator = _generator()

    assert generator._gen_name("lower", for_param=True) == "value"

    generator.namespace = ast.GLOBAL_NAMESPACE + ("function",)
    assert generator._gen_name("lower") == "value"


def test_gen_param_decl_uses_the_bare_name_path(monkeypatch):
    monkeypatch.setattr(
        generator_utils,
        "gen_identifier",
        lambda _ident_type=None: "parameter",
    )
    generator = _generator()

    parameter = generator.gen_param_decl(kt.Integer)

    assert parameter.name == "parameter"


def test_declaration_generators_qualify_only_global_declarations(monkeypatch):
    monkeypatch.setattr(
        generator_utils,
        "gen_identifier",
        lambda ident_type=None: {
            "lower": "value",
            "capitalize": "Box",
        }[ident_type],
    )
    monkeypatch.setattr(
        Generator,
        "_gen_func_body",
        lambda _self, _ret_type, _func=None: ast.IntegerConstant(1, kt.Integer),
    )
    monkeypatch.setattr(Generator, "_select_superclass", lambda *_args: None)
    monkeypatch.setattr(Generator, "gen_class_fields", lambda *_args: None)
    monkeypatch.setattr(Generator, "gen_class_functions", lambda *_args: None)
    generator = _generator()

    function = generator.gen_func_decl(
        etype=kt.Integer,
        func_name="helper",
        params=[],
        type_params=[],
        visibility=ast.Visibilities.PUBLIC,
    )
    cls = generator.gen_class_decl(type_params=[])
    variable = generator.gen_variable_decl(
        etype=kt.Integer,
        expr=ast.IntegerConstant(1, kt.Integer),
    )

    generator.namespace = ast.GLOBAL_NAMESPACE + (cls.name,)
    field = generator.gen_field_decl(
        kt.Integer,
        add_to_parent=False,
        visibility=ast.Visibilities.PUBLIC,
    )

    assert function.name == "src.d2.helper"
    assert cls.name == "src.d2.Box"
    assert variable.name == "src.d2.value"
    assert field.name == "value"


def test_generate_stamps_target_module_on_context_and_program(monkeypatch):
    context = Context()
    generator = _generator()
    monkeypatch.setattr(generator, "gen_top_level_declaration", lambda: None)
    monkeypatch.setattr(generator, "generate_main_func", lambda: None)

    program = generator.generate(context)

    assert context.target_module == "src.d2"
    assert program.target_module == "src.d2"


def test_matching_helper_class_is_named_in_global_isolation(monkeypatch):
    generator = _generator()
    generator.namespace = ast.GLOBAL_NAMESPACE + ("caller",)
    observed = {}

    monkeypatch.setattr(
        generator_utils,
        "gen_identifier",
        lambda _ident_type=None: "Helper",
    )

    def fake_gen_class_decl(**kwargs):
        observed["namespace"] = generator.namespace
        observed["name"] = kwargs["class_name"]
        return ast.ClassDeclaration(
            kwargs["class_name"],
            [],
            fields=[ast.FieldDeclaration("value", kt.Integer)],
        )

    monkeypatch.setattr(generator, "gen_class_decl", fake_gen_class_decl)
    monkeypatch.setattr(
        generator,
        "_is_sigtype_compatible",
        lambda *_args, **_kwargs: True,
    )

    access = generator._gen_matching_class(kt.Integer, "fields")

    assert observed == {
        "namespace": ast.GLOBAL_NAMESPACE,
        "name": "src.d2.Helper",
    }
    assert access.attr_decl.name == "value"
    assert generator.namespace == ast.GLOBAL_NAMESPACE + ("caller",)


def test_default_value_lambda_parameters_stay_bare(monkeypatch):
    monkeypatch.setattr(
        generator_utils,
        "gen_identifier",
        lambda _ident_type=None: "value",
    )
    monkeypatch.setattr(ut.random, "bool", lambda *_args, **_kwargs: False)
    generator = _generator()
    monkeypatch.setattr(
        generator,
        "_gen_func_body",
        lambda _ret_type, _func=None: ast.IntegerConstant(1, kt.Integer),
    )

    signature = generator.bt_factory.get_function_type(1).new(
        [kt.Integer, kt.Integer])
    parameter = ast.ParameterDeclaration(
        "callback",
        signature,
        inlining_scope=ast.InliningScope.INLINE,
    )
    parameter._pending_default = True
    function = ast.FunctionDeclaration(
        "src.d2.owner",
        [parameter],
        kt.Integer,
        ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.FUNCTION,
        is_inline=True,
        visibility=ast.Visibilities.PUBLIC,
    )
    generator.namespace = ast.GLOBAL_NAMESPACE + (function.name,)

    generator._gen_param_defaults(function, [parameter])

    assert isinstance(parameter.default, ast.Lambda)
    assert [param.name for param in parameter.default.params] == ["value"]
    assert "." not in parameter.default.params[0].name