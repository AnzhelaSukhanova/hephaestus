from copy import deepcopy

import pytest

from src.analysis import type_dependency_analysis as tda
from src.generators import utils as generator_utils
from src.generators.config import cfg
from src.generators.generator import Generator, _ReceiverClassification
from src.ir import ast, context as ctx, kotlin_types as kt, type_utils
from src.ir import types as tp
from src.ir.visitors import DefaultVisitor, DefaultVisitorUpdate
from src.transformations.type_erasure import TypeErasure
from src.translators.groovy import GroovyTranslator
from src.translators.java import JavaTranslator
from src.translators.kotlin import KotlinTranslator
from src.translators.scala import ScalaTranslator


KT_FACTORY = kt.KotlinBuiltinFactory()


def make_class(name="A", class_type=ast.ClassDeclaration.REGULAR,
               is_final=False, functions=None, fields=None):
    return ast.ClassDeclaration(
        name,
        [],
        class_type,
        fields=fields or [],
        functions=functions or [],
        is_final=is_final,
    )


def make_generator(class_decl=None):
    generator = Generator("kotlin")
    generator.context = ctx.Context()
    if class_decl is not None:
        generator.context.add_class(
            ast.GLOBAL_NAMESPACE, class_decl.name, class_decl)
    return generator


def test_enforce_type_via_cast_ast_and_visitors():
    child = ast.BottomConstant(kt.Integer)
    node = ast.EnforceTypeViaCast(child, kt.Number)
    assert node.children() == [child]
    assert node.get_type() == kt.Number
    assert node.is_bottom()
    assert node.is_equal(ast.EnforceTypeViaCast(
        ast.BottomConstant(kt.Integer), kt.Number))
    assert not node.is_equal(ast.EnforceTypeViaCast(
        ast.BottomConstant(kt.Integer), kt.Any))
    assert "EnforceTypeViaCast" in str(node)

    visited = []

    class Visitor(DefaultVisitor):
        def visit_enforce_type_via_cast(self, current):
            visited.append(current)
            return super().visit_enforce_type_via_cast(current)

    node.accept(Visitor())
    assert visited == [node]

    replacement = ast.IntegerConstant(2, kt.Integer)

    class UpdatingVisitor(DefaultVisitorUpdate):
        def visit_bottom_constant(self, current):
            return replacement

    assert node.accept(UpdatingVisitor()) is node
    assert node.expr is replacement


def test_enforce_type_via_cast_translation_and_non_kotlin_fallback():
    node = ast.EnforceTypeViaCast(ast.IntegerConstant(1, kt.Integer), kt.Number)
    translator = KotlinTranslator()
    node.accept(translator)
    assert translator._children_res[-1] == "(1 as Number)"

    for translator_class in (JavaTranslator, GroovyTranslator, ScalaTranslator):
        with pytest.raises(NotImplementedError, match="only supported"):
            node.accept(translator_class())


def test_type_hint_direct_and_chained_casts():
    method = ast.FunctionDeclaration(
        "make", [], kt.String, ast.StringConstant("x"),
        ast.FunctionDeclaration.CLASS_METHOD,
    )
    field = ast.FieldDeclaration("value", kt.Integer)
    cls = make_class(functions=[method], fields=[field])
    context = ctx.Context()
    context.add_class(ast.GLOBAL_NAMESPACE, cls.name, cls)
    context.add_func(ast.GLOBAL_NAMESPACE + (cls.name,), method.name, method)
    context.add_var(ast.GLOBAL_NAMESPACE + (cls.name,), field.name, field)

    cast = ast.EnforceTypeViaCast(ast.New(cls.get_type(), []), cls.get_type())
    assert type_utils.get_type_hint(
        cast, context, ast.GLOBAL_NAMESPACE, KT_FACTORY, []) == cls.get_type()
    assert type_utils.get_type_hint(
        ast.FieldAccess(cast, "value"), context, ast.GLOBAL_NAMESPACE,
        KT_FACTORY, []) == kt.Integer
    assert type_utils.get_type_hint(
        ast.FunctionCall("make", [], receiver=cast), context,
        ast.GLOBAL_NAMESPACE, KT_FACTORY, []) == kt.String


def test_tda_isolates_cast_child_and_reports_explicit_target():
    cls = make_class()
    context = ctx.Context()
    context.add_class(ast.GLOBAL_NAMESPACE, cls.name, cls)
    target = cls.get_type()
    decl = ast.VariableDeclaration(
        "x",
        ast.EnforceTypeViaCast(ast.New(target, []), target),
        var_type=target,
    )
    context.add_var(ast.GLOBAL_NAMESPACE, decl.name, decl)
    program = ast.Program(context, "kotlin")

    analysis = tda.TypeDependencyAnalysis(program)
    analysis.visit(decl)
    source = next(node for node in analysis.result()
                  if isinstance(node, tda.DeclarationNode) and
                  node.decl is decl)
    targets = [edge.target for edge in analysis.result()[source]]
    assert any(isinstance(node, tda.TypeNode) and
               node.t == target and node.parent_id is None
               for node in targets)
    assert not any(isinstance(node, tda.DeclarationNode) and
                   node.get_type() == target
                   for node in targets)
    assert any("/__TYPE_ENFORCEMENT__" in node_id
               for node_id in analysis._inferred_nodes)
    assert analysis._exp_type is None
    assert analysis._exp_node_id is None


def test_type_erasure_visitor_preserves_cast_target():
    target = kt.String
    body = ast.EnforceTypeViaCast(ast.StringConstant("x"), target)
    func = ast.FunctionDeclaration(
        "f", [], target, body, ast.FunctionDeclaration.FUNCTION)
    context = ctx.Context()
    context.add_func(ast.GLOBAL_NAMESPACE, func.name, func)
    program = ast.Program(context, "kotlin")

    transformer = TypeErasure(program, "kotlin", options={"max_combinations": 0})
    transformer.transform()
    assert isinstance(func.body, ast.EnforceTypeViaCast)
    assert func.body.target_type == target


def test_receiver_classifier_categories_and_type_variable_assertion():
    method = ast.FunctionDeclaration(
        "m", [], kt.Integer, ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.CLASS_METHOD, is_final=False)
    final_class = make_class("Final", is_final=True, functions=[method])
    open_class = make_class("Open", is_final=False, functions=[method])
    interface = make_class("Iface", ast.ClassDeclaration.INTERFACE,
                           functions=[method])
    abstract = make_class("Abs", ast.ClassDeclaration.ABSTRACT,
                          functions=[method])
    generator = make_generator(final_class)
    for cls in (open_class, interface, abstract):
        generator.context.add_class(ast.GLOBAL_NAMESPACE, cls.name, cls)

    assert generator._classify_receiver(None, method) == \
        _ReceiverClassification.NOT_RECEIVER
    assert generator._classify_receiver(final_class.get_type(), method) == \
        _ReceiverClassification.EFFECTIVELY_FINAL
    assert generator._classify_receiver(open_class.get_type(), method) == \
        _ReceiverClassification.OPEN_REGULAR
    assert generator._classify_receiver(interface.get_type(), method) == \
        _ReceiverClassification.INTERFACE_OR_ABSTRACT
    assert generator._classify_receiver(abstract.get_type(), method) == \
        _ReceiverClassification.INTERFACE_OR_ABSTRACT
    assert generator._classify_receiver(tp.SimpleClassifier("Missing"), method) == \
        _ReceiverClassification.CANT_DISPROVE_FINAL
    with pytest.raises(AssertionError):
        generator._classify_receiver(tp.TypeParameter("T"), method)


def test_receiver_policy_uses_exact_x_and_y_subtype_flags(monkeypatch):
    method = ast.FunctionDeclaration(
        "m", [], kt.Integer, ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.CLASS_METHOD, is_final=False)
    cls = make_class("Open", is_final=False, functions=[method])
    generator = make_generator(cls)
    type_fun = generator_utils.AttrAccessInfo(cls.get_type(), {}, method, {})
    calls = []

    def generate_expr(etype, only_leaves=False, subtype=True):
        calls.append((etype, only_leaves, subtype))
        return ast.New(cls.get_type(), [])

    monkeypatch.setattr(generator, "generate_expr", generate_expr)
    monkeypatch.setattr("src.utils.random.bool", lambda prob=0.5: False)
    receiver = generator._gen_fresh_receiver(type_fun, only_leaves=True)
    assert isinstance(receiver, ast.New)
    assert calls[-1] == (cls.get_type(), True, False)

    calls.clear()
    monkeypatch.setattr("src.utils.random.bool", lambda prob=0.5: True)
    receiver = generator._gen_fresh_receiver(type_fun, only_leaves=False)
    assert isinstance(receiver, ast.EnforceTypeViaCast)
    assert receiver.target_type == cls.get_type()
    assert calls[-1] == (cls.get_type(), False, True)


def test_missing_metadata_shares_open_regular_probability(monkeypatch):
    method = ast.FunctionDeclaration(
        "m", [], kt.Integer, ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.CLASS_METHOD, is_final=False)
    receiver_type = tp.SimpleClassifier("Unknown")
    generator = make_generator()
    type_fun = generator_utils.AttrAccessInfo(
        receiver_type, {}, method, {})
    coins = []
    calls = []

    monkeypatch.setattr(
        "src.utils.random.bool",
        lambda prob=0.5: coins.append(prob) or False)
    monkeypatch.setattr(
        generator,
        "generate_expr",
        lambda *args, **kwargs: calls.append((args, kwargs)) or
        ast.BottomConstant(receiver_type),
    )

    receiver = generator._gen_fresh_receiver(type_fun)
    assert receiver.t == receiver_type
    assert coins == [cfg.prob.receiver_ascribe_prob_open_regular]
    assert calls[-1][1]["subtype"] is False


@pytest.mark.parametrize("receiver", [
    ast.Conditional(ast.BooleanConstant("true"),
                    ast.New(tp.SimpleClassifier("A"), []),
                    ast.New(tp.SimpleClassifier("A"), []),
                    tp.SimpleClassifier("A")),
    ast.New(tp.SimpleClassifier("A"), []),
    ast.Variable("x"),
])
def test_y_arm_wraps_expression_shapes(monkeypatch, receiver):
    method = ast.FunctionDeclaration(
        "m", [], kt.Integer, ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.CLASS_METHOD, is_final=False)
    cls = make_class("Open", is_final=False, functions=[method])
    generator = make_generator(cls)
    type_fun = generator_utils.AttrAccessInfo(cls.get_type(), {}, method, {})
    monkeypatch.setattr(generator, "generate_expr",
                        lambda *args, **kwargs: receiver)
    monkeypatch.setattr("src.utils.random.bool", lambda prob=0.5: True)
    result = generator._gen_fresh_receiver(type_fun)
    assert isinstance(result, ast.EnforceTypeViaCast)
    assert result.expr is receiver


def test_bottom_and_existing_cast_are_not_double_wrapped(monkeypatch):
    method = ast.FunctionDeclaration(
        "m", [], kt.Integer, ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.CLASS_METHOD, is_final=False)
    cls = make_class("Open", is_final=False, functions=[method])
    generator = make_generator(cls)
    type_fun = generator_utils.AttrAccessInfo(cls.get_type(), {}, method, {})
    monkeypatch.setattr("src.utils.random.bool", lambda prob=0.5: True)

    exact_bottom = ast.BottomConstant(cls.get_type())
    monkeypatch.setattr(generator, "generate_expr",
                        lambda *args, **kwargs: exact_bottom)
    assert generator._gen_fresh_receiver(type_fun) is exact_bottom

    narrowed_bottom = ast.BottomConstant(tp.SimpleClassifier("Sub", [cls.get_type()]))
    monkeypatch.setattr(generator, "generate_expr",
                        lambda *args, **kwargs: narrowed_bottom)
    wrapped = generator._gen_fresh_receiver(type_fun)
    assert isinstance(wrapped, ast.EnforceTypeViaCast)
    assert wrapped.expr is narrowed_bottom

    existing = ast.EnforceTypeViaCast(ast.Variable("x"), cls.get_type())
    monkeypatch.setattr(generator, "generate_expr",
                        lambda *args, **kwargs: existing)
    assert generator._gen_fresh_receiver(type_fun) is existing


def test_final_receivers_do_not_consume_policy_coin_or_use_x_arm(monkeypatch):
    method = ast.FunctionDeclaration(
        "m", [], kt.Integer, ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.CLASS_METHOD, is_final=False)
    cls = make_class("Final", is_final=True, functions=[method])
    generator = make_generator(cls)
    type_fun = generator_utils.AttrAccessInfo(cls.get_type(), {}, method, {})
    coins = []
    monkeypatch.setattr("src.utils.random.bool",
                        lambda prob=0.5: coins.append(prob) or False)
    calls = []
    monkeypatch.setattr(
        generator, "generate_expr",
        lambda *args, **kwargs: calls.append(kwargs) or ast.New(cls.get_type(), []))
    generator._gen_fresh_receiver(type_fun)
    assert not coins
    assert calls == [{}]

    final_method = deepcopy(method)
    final_method.is_final = True
    type_fun = generator_utils.AttrAccessInfo(cls.get_type(), {}, final_method, {})
    generator._gen_fresh_receiver(type_fun)
    assert not coins


def test_non_kotlin_fresh_receiver_never_constructs_cast(monkeypatch):
    method = ast.FunctionDeclaration(
        "m", [], kt.Integer, ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.CLASS_METHOD, is_final=False)
    cls = make_class("Open", is_final=False, functions=[method])
    generator = Generator("java")
    generator.context = ctx.Context()
    type_fun = generator_utils.AttrAccessInfo(cls.get_type(), {}, method, {})
    expression = ast.New(cls.get_type(), [])
    monkeypatch.setattr(generator, "generate_expr",
                        lambda *args, **kwargs: expression)
    assert generator._gen_fresh_receiver(type_fun) is expression


def test_receiver_configuration_knobs_are_independent():
    config = deepcopy(cfg)
    config.json_config({
        "prob": {
            "receiver_ascribe_prob_open_regular": 0.1,
            "receiver_ascribe_prob_interface_abstract": 0.9,
        }
    })
    assert config.prob.receiver_ascribe_prob_open_regular == 0.1
    assert config.prob.receiver_ascribe_prob_interface_abstract == 0.9
