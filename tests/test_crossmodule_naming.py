import pytest

from src import utils as ut
from src.generators import utils as generator_utils
from src.generators.generator import Generator
from src.ir import ast, kotlin_types as kt
from src.ir.context import Context
from src.translators.kotlin import KotlinTranslator


def _function(name, body=None):
    return ast.FunctionDeclaration(
        name,
        [],
        kt.Integer,
        body or ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.FUNCTION,
    )


def _translate(program, package):
    translator = KotlinTranslator(package)
    translator.visit(program)
    return translator.result()


def test_target_module_is_stamped_on_context_and_program(monkeypatch):
    context = Context()
    generator = Generator("kotlin", target_module="src.d7")
    monkeypatch.setattr(generator, "gen_top_level_declaration", lambda: None)
    monkeypatch.setattr(generator, "generate_main_func", lambda: None)

    program = generator.generate(context)

    assert context.target_module == "src.d7"
    assert program.target_module == "src.d7"
    translator = KotlinTranslator()
    translator.visit(program)
    assert translator.context is context
    assert translator.context.target_module == "src.d7"


def test_top_level_names_are_minted_but_nested_names_stay_bare(monkeypatch):
    generator = Generator("kotlin", target_module="src.d7")
    generator.context = Context()
    monkeypatch.setattr(
        generator,
        "_gen_func_body",
        lambda _ret_type, _func=None: ast.IntegerConstant(1, kt.Integer),
    )
    monkeypatch.setattr(generator, "_select_superclass", lambda _only_interfaces: None)
    monkeypatch.setattr(generator, "gen_class_fields", lambda *_args: None)
    monkeypatch.setattr(generator, "gen_class_functions", lambda *_args: None)

    func = generator.gen_func_decl(
        etype=kt.Integer,
        func_name="helper",
        params=[],
        type_params=[],
        visibility=ast.Visibilities.PUBLIC,
    )
    cls = generator.gen_class_decl(class_name="Box", type_params=[])
    top_var = generator.gen_variable_decl(
        etype=kt.Integer,
        expr=ast.IntegerConstant(1, kt.Integer),
    )
    generator.namespace = ast.GLOBAL_NAMESPACE + (func.name,)
    local_var = generator.gen_variable_decl(
        etype=kt.Integer,
        expr=ast.IntegerConstant(2, kt.Integer),
    )

    assert func.name == "src.d7.helper"
    assert cls.name == "src.d7.Box"
    assert top_var.name.startswith("src.d7.")
    assert "." not in local_var.name
    assert set(generator.context.get_declarations(
        ast.GLOBAL_NAMESPACE, only_current=True)) >= {
            func.name, cls.name, top_var.name,
        }


def test_generated_main_name_remains_bare(monkeypatch):
    generator = Generator("kotlin", target_module="src.d7")
    generator.context = Context()
    monkeypatch.setattr(
        generator,
        "generate_expr",
        lambda: ast.IntegerConstant(1, kt.Integer),
    )

    main = generator.generate_main_func()

    assert main.name == "main"
    assert generator.context.get_funcs(
        ast.GLOBAL_NAMESPACE + ("main",))[main.name] is main
    assert generator.context.get_decl(ast.GLOBAL_NAMESPACE, main.name) is None
    assert main.name not in generator.context.get_declarations(
        ast.GLOBAL_NAMESPACE, only_current=True)


def test_qualified_class_namespace_keeps_method_conventions(monkeypatch):
    def generate_method(class_name):
        generator = Generator("kotlin", target_module="src.d7")
        generator.context = Context()
        generator.namespace = ast.GLOBAL_NAMESPACE + (class_name,)
        monkeypatch.setattr(
            generator,
            "_gen_func_body",
            lambda _ret_type, _func=None: ast.IntegerConstant(1, kt.Integer),
        )
        ut.random.r.seed(73)
        return generator.gen_func_decl(
            etype=kt.Integer,
            func_name="method",
            params=[],
            type_params=[],
            class_is_final=True,
            visibility=ast.Visibilities.UNKNOWN,
        )

    bare = generate_method("Box")
    qualified = generate_method("src.d7.Box")

    assert qualified.func_type == bare.func_type
    assert qualified.visibility == bare.visibility
    assert qualified.is_inline == bare.is_inline
    assert qualified.is_final == bare.is_final


def test_split_qualified_name_keeps_nested_declaration_suffix():
    assert ut.split_qualified_name("src.d1.Box") == ("src.d1", "Box")
    assert ut.split_qualified_name("src.d1.Box.method") == (
        "src.d1", "Box.method")
    assert ut.split_qualified_name("Box") == (None, "Box")

    context = Context()
    context.target_module = "src.d1"
    assert context.name_is_local("src.d1.Box")
    assert context.name_is_local("Box")
    assert context.name_is_local("src.d1.Box.method")
    assert not context.name_is_local("src.d3.Box")
    assert not context.name_is_local("Box.method")
    program = ast.Program(context, "kotlin")
    assert program.context.name_is_local("src.d1.Box")
    assert program.context.name_is_local("Box")
    assert not program.context.name_is_local("src.d3.Box")


@pytest.mark.parametrize(
    "name, expected",
    [
        ("bare", True),
        ("src.d1.Box", True),
        ("src.d1.Box.method", True),
        ("src.d10.Box", False),
        ("src.d1x.Box", False),
    ],
)
def test_context_name_locality_uses_exact_module_boundaries(name, expected):
    context = Context()
    context.target_module = "src.d1"

    assert context.name_is_local(name) is expected


def test_kotlin_prefix_stripping_preserves_foreign_qualified_names():
    context = Context()
    context.target_module = "src.d7"
    translator = KotlinTranslator()
    translator.context = context

    assert translator._strip_own_prefix("src.d7.Box.method") == "Box.method"
    assert translator._strip_own_prefix("src.d3.Box.method") == (
        "src.d3.Box.method")
    assert translator._strip_own_prefix("method") == "method"


def test_receiverless_function_reference_rejects_foreign_top_level(monkeypatch):
    generator = Generator("kotlin", target_module="src.d7")
    generator.context = Context()
    generator.context.target_module = generator.target_module
    own = _function("src.d7.own")
    foreign = _function("src.d3.foreign")
    candidates = [
        generator_utils.AttrReceiverInfo(None, {}, foreign, {}),
        generator_utils.AttrReceiverInfo(None, {}, own, {}),
    ]
    monkeypatch.setattr(
        generator,
        "_get_matching_function_declarations",
        lambda *_args, **_kwargs: candidates,
    )
    monkeypatch.setattr(generator, "_get_matching_class", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(generator, "_gen_matching_func", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ut.random, "choice", lambda values: values[0])

    ref = generator._gen_func_ref(kt.Integer)

    assert ref.target_decl is own
    assert ref.func == "src.d7.own"


def test_kotlin_translation_strips_own_prefix_only_at_declaration_sites():
    context = Context()
    context.target_module = "src.d7"
    program = ast.Program(context, "kotlin")
    helper = _function("src.d7.helper")
    caller = _function(
        "src.d7.caller",
        ast.FunctionCall("src.d7.helper", []),
    )
    ref = ast.VariableDeclaration(
        "src.d7.ref",
        ast.FunctionReference("src.d7.helper", None, kt.Integer,
                              target_decl=helper),
        var_type=kt.Integer,
    )
    cls = ast.ClassDeclaration("src.d7.Box", [])
    main = ast.FunctionDeclaration(
        "src.d7.main",
        [],
        kt.Unit,
        ast.Block([ast.FunctionCall("src.d7.caller", [])]),
        ast.FunctionDeclaration.FUNCTION,
    )
    for declaration in (helper, caller, ref, cls, main):
        program.add_declaration(declaration)

    source = _translate(program, "src.d7")

    assert source.startswith("package src.d7\n")
    assert "fun helper(" in source
    assert "fun caller(" in source
    assert "val ref:" in source
    assert "class Box" in source
    assert "fun main(" in source
    assert "src.d7.helper()" in source
    assert "src.d7.caller()" in source
    assert "::helper" in source
    assert "fun src.d7." not in source
    assert "class src.d7." not in source
    assert "val src.d7." not in source
    assert "::src.d7." not in source
    assert "import " not in source