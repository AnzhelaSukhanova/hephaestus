import pytest

from src.ir import (ast, groovy_types as gt, java_types as jt,
                    kotlin_types as kt, scala_types as sc)
from src.ir.context import Context
from src.translators.base import BaseTranslator
from src.translators.groovy import GroovyTranslator
from src.translators.java import JavaTranslator
from src.translators.kotlin import KotlinTranslator
from src.translators.scala import ScalaTranslator


@pytest.mark.parametrize(
    "language, types, translator_cls",
    [
        ("kotlin", kt, KotlinTranslator),
        ("scala", sc, ScalaTranslator),
    ],
)
def test_direct_render_translator_visit_and_result(language, types, translator_cls):
    program = ast.Program(Context(), language)
    program.add_declaration(ast.VariableDeclaration(
        "answer", ast.IntegerConstant(1, types.Integer),
        var_type=types.Integer))
    translator = translator_cls(package="sample")

    with pytest.raises(Exception, match="translate the program first"):
        translator.result()

    assert translator.visit(program) is None
    assert translator.result() == "package sample\nval answer: Int = 1"
    assert translator._children_res == []
    assert translator._nodes_stack == [None]


@pytest.mark.parametrize(
    "language, types, translator_cls, prefix",
    [
        ("java", jt, JavaTranslator,
         "package sample;\n\nclass Main {\n"
         "  static final Integer answer = 1;\n}"),
        ("groovy", gt, GroovyTranslator,
         "package sample\n\nclass Main {\n"
         "  static final Integer answer = 1\n}"),
    ],
)
def test_main_class_translator_render_and_reset(
        language, types, translator_cls, prefix):
    program = ast.Program(Context(), language)
    program.add_declaration(ast.VariableDeclaration(
        "answer", ast.IntegerConstant(1, types.Integer),
        var_type=types.Integer))
    translator = translator_cls(package="sample")

    assert translator.visit(program) is None
    source = translator.result()
    assert source.startswith(prefix)
    assert source.count("interface Function") == 4
    assert translator._main_children == []
    assert translator._main_method == ""
    assert translator._children_res == []
    assert translator._nodes_stack == [None]
    assert translator.context is None
    assert translator.visit(program) is None
    assert translator.result() == source


@pytest.mark.parametrize(
    "translator_cls, types",
    [(JavaTranslator, jt), (GroovyTranslator, gt), (ScalaTranslator, sc)],
)
def test_non_kotlin_cast_is_explicitly_unsupported(translator_cls, types):
    assert "visit_enforce_type_via_cast" not in BaseTranslator.__dict__
    assert "visit_enforce_type_via_cast" in translator_cls.__dict__
    node = ast.EnforceTypeViaCast(
        ast.IntegerConstant(1, types.Integer), types.Integer)

    with pytest.raises(
            NotImplementedError,
            match="EnforceTypeViaCast is only supported by the Kotlin translator"):
        translator_cls().visit(node)