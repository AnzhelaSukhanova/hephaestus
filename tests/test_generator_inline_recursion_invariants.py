"""Producer/selection invariants behind the six legacy recursion-shape xfails.

These tests force expression choices, not declaration lookup or inline gates.
Every rejection has a selectable control outside the inline source frame.
"""
from collections.abc import Iterator

import pytest

from src import utils as ut
from src.generators.config import cfg
from src.generators.generator import ExpressionGenerator, Generator
from src.ir import ast, kotlin_types as kt, types as tp
from src.ir.context import Context
from src.ir.generation_context import (
    FunctionBodyGeneration, InliningSource, IrFunctionBodyStub,
)
from src.translators.kotlin import KotlinTranslator


@pytest.fixture(autouse=True)
def random_state() -> Iterator[None]:
    state = ut.random.r.getstate()
    ut.random.r.seed(814)
    try:
        yield
    finally:
        ut.random.r.setstate(state)


def make_generator() -> tuple[Generator, Context]:
    generator = Generator('kotlin')
    context = Context()
    generator.context = context
    return generator, context


def make_function(name: str, *, method: bool = False) -> ast.FunctionDeclaration:
    return ast.FunctionDeclaration(
        name, [], kt.Integer, ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.CLASS_METHOD if method
        else ast.FunctionDeclaration.FUNCTION,
        is_inline=True, visibility=ast.Visibilities.PUBLIC,
    )


def add_class(context: Context, name: str,
              method: ast.FunctionDeclaration) -> ast.ClassDeclaration:
    cls = ast.ClassDeclaration(
        name, [], ast.ClassDeclaration.REGULAR,
        fields=[], functions=[method], is_final=True,
    )
    context.add_class(ast.GLOBAL_NAMESPACE, name, cls)
    context.add_func(ast.GLOBAL_NAMESPACE + (name,), method.name, method)
    return cls


def render(expr: ast.Expr, context: Context | None = None) -> str:
    translator = KotlinTranslator()
    translator.context = context if context is not None else Context()
    expr.accept(translator)
    return translator._children_res[-1]


def select_expression(monkeypatch: pytest.MonkeyPatch, generator: Generator,
                      expression: ExpressionGenerator) -> None:
    def choices(expr_type: tp.Type, only_leaves: bool, subtype: bool,
                exclude_var: bool, sam_coercion: bool = False
                ) -> list[ExpressionGenerator]:
        return [expression]

    monkeypatch.setattr(generator, 'get_generators', choices)


def test_implicit_this_lookup_keeps_identity_and_rejects_self() -> None:
    generator, context = make_generator()
    source = make_function('foo', method=True)
    cls = add_class(context, 'C', source)
    generator.namespace = ast.GLOBAL_NAMESPACE + ('C', 'foo')

    assert generator._get_class_attributes(cls, 'functions') == [source]
    candidates = generator._get_matching_function_declarations(kt.Integer, False)
    assert len(candidates) == 1
    assert candidates[0].attr_decl is source
    # Production represents a current-class call with an implicit, not explicit, this.
    assert candidates[0].receiver_expr is None
    assert render(generator._gen_func_call(kt.Integer)) == 'foo()'

    with context.call_contexts(subtree_pushed_call_context=[
            InliningSource(source, IrFunctionBodyStub()),
            FunctionBodyGeneration(source)]):
        assert generator._get_matching_function_declarations(kt.Integer, False) == []
        helper = make_function('safe', method=True)
        cls.functions.append(helper)
        context.add_func(ast.GLOBAL_NAMESPACE + ('C',), helper.name, helper)
        call = generator._gen_func_call(kt.Integer)
        assert render(call) == 'safe()'
        assert generator.inline_call_graph.reaches(
            (source, IrFunctionBodyStub()), (helper, IrFunctionBodyStub()))
        assert not generator.inline_call_graph.reaches(
            (source, IrFunctionBodyStub()), (source, IrFunctionBodyStub()))


@pytest.mark.parametrize('bounded', [False, True], ids=['expression', 'cast-receiver'])
def test_object_lookup_filters_the_declaration_not_the_receiver(bounded: bool) -> None:
    generator, context = make_generator()
    source = make_function('foo', method=True)
    cls = add_class(context, 'C', source)
    generator.namespace = ast.GLOBAL_NAMESPACE + ('C', 'foo')
    receiver_type = tp.TypeParameter('T', bound=cls.get_type()) if bounded else cls.get_type()
    other = ast.ParameterDeclaration('other', receiver_type)
    context.add_var(generator.namespace, other.name, other)

    candidates = generator._get_matching_objects(kt.Integer, False, 'functions')
    assert len(candidates) == 1
    assert candidates[0].attr_decl is source
    assert render(candidates[0].receiver_expr) == ('(other as C)' if bounded else 'other')

    with context.call_contexts(subtree_pushed_call_context=[
            InliningSource(source, IrFunctionBodyStub()),
            FunctionBodyGeneration(source)]):
        assert generator._get_matching_objects(kt.Integer, False, 'functions') == []
        assert generator._get_matching_function_declarations(kt.Integer, False) == []
        # Same method name on another final class is NOT recursion.
        target = make_function('foo', method=True)
        peer_class = add_class(context, 'D', target)
        peer_type = (tp.TypeParameter('U', bound=peer_class.get_type())
                     if bounded else peer_class.get_type())
        peer = ast.ParameterDeclaration('peer', peer_type)
        context.add_var(generator.namespace, peer.name, peer)
        candidates = generator._get_matching_function_declarations(kt.Integer, False)
        assert len(candidates) == 1 and candidates[0].attr_decl is target
        call = generator._gen_func_call(kt.Integer)
        assert render(call) == ('(peer as D).foo()' if bounded else 'peer.foo()')
        assert generator.inline_call_graph.reaches(
            (source, IrFunctionBodyStub()), (target, IrFunctionBodyStub()))


@pytest.mark.parametrize('shape', ['new-instance', 'field-access'])
def test_fresh_callee_is_filtered_before_generating_receiver(
        monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    generator, context = make_generator()
    source = make_function('foo', method=True)
    cls = add_class(context, 'C', source)
    generator.namespace = ast.GLOBAL_NAMESPACE + ('C', 'foo')
    candidates = generator._get_matching_class_decls(kt.Integer, False, 'functions')
    assert len(candidates) == 1 and candidates[0][2] is source
    assert candidates[0][0] is cls

    with context.call_contexts(subtree_pushed_call_context=[
            InliningSource(source, IrFunctionBodyStub()),
            FunctionBodyGeneration(source)]):
        assert generator._get_matching_class_decls(kt.Integer, False, 'functions') == []
        target = make_function('foo', method=True)
        peer_class = add_class(context, 'D', target)
        if shape == 'field-access':
            field = ast.FieldDeclaration('c', peer_class.get_type())
            box = ast.ClassDeclaration('Box', [], ast.ClassDeclaration.REGULAR,
                                       fields=[field], functions=[], is_final=True)
            context.add_class(ast.GLOBAL_NAMESPACE, box.name, box)
            param = ast.ParameterDeclaration('box', box.get_type())
            context.add_var(generator.namespace, param.name, param)

        requested_types: list[tp.Type] = []

        def receiver(etype: tp.Type) -> ast.Expr:
            requested_types.append(etype)
            assert etype == peer_class.get_type()
            if shape == 'field-access':
                return generator.gen_field_access(etype, only_leaves=True, subtype=False)
            return generator.gen_new(etype, only_leaves=True, subtype=False)

        select_expression(monkeypatch, generator, receiver)
        # No scoped/object candidate: exercise the actual fresh-class fallback.
        assert generator._get_matching_function_declarations(kt.Integer, False) == []
        candidates = generator._get_matching_class_decls(kt.Integer, False, 'functions')
        assert len(candidates) == 1 and candidates[0][2] is target
        call = generator._gen_func_call(kt.Integer, only_leaves=True)
        assert requested_types == [peer_class.get_type()]
        assert render(call) == ('box.c.foo()' if shape == 'field-access' else 'D().foo()')
        assert generator.inline_call_graph.reaches(
            (source, IrFunctionBodyStub()), (target, IrFunctionBodyStub()))


@pytest.mark.parametrize('nested_scope', [False, True], ids=['body', 'nested-block'])
def test_stored_reference_preserves_target_and_records_without_invocation(
        monkeypatch: pytest.MonkeyPatch, nested_scope: bool) -> None:
    generator, context = make_generator()
    source = make_function('storedReferenceOnly')
    target = make_function('safeReference')
    for func in (source, target):
        context.add_func(ast.GLOBAL_NAMESPACE, func.name, func)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    if nested_scope:
        generator.namespace += ('true_block',)
    signature = generator.bt_factory.get_function_type(0).new([kt.Integer])
    # Choose the real reference generator, not a fabricated FunctionReference.
    monkeypatch.setattr(cfg.prob, 'func_ref', 1.0)
    select_expression(monkeypatch, generator, generator.gen_new)
    with context.call_contexts(subtree_pushed_call_context=[
            InliningSource(source, IrFunctionBodyStub()),
            FunctionBodyGeneration(source)]):
        stored = generator.gen_variable_decl(signature, only_leaves=True)
        assert isinstance(stored.expr, ast.FunctionReference)
        assert stored.expr.target_decl is target
        assert render(stored.expr, context) == '::safeReference'
        assert context.get_vars(generator.namespace)[stored.name] is stored
        assert generator.inline_call_graph.reaches(
            (source, IrFunctionBodyStub()), (target, IrFunctionBodyStub()))
        assert not generator.inline_call_graph.reaches(
            (source, IrFunctionBodyStub()), (source, IrFunctionBodyStub()))


def test_same_name_in_different_scopes_is_not_function_identity() -> None:
    generator, context = make_generator()
    source = make_function('foo')
    target = make_function('foo', method=True)
    context.add_func(ast.GLOBAL_NAMESPACE, source.name, source)
    cls = add_class(context, 'C', target)
    generator.namespace = ast.GLOBAL_NAMESPACE + ('foo',)
    receiver = ast.ParameterDeclaration('other', cls.get_type())
    context.add_var(generator.namespace, receiver.name, receiver)
    with context.call_contexts(subtree_pushed_call_context=[
            InliningSource(source, IrFunctionBodyStub()),
            FunctionBodyGeneration(source)]):
        candidates = generator._get_matching_function_declarations(kt.Integer, False)
        assert len(candidates) == 1 and candidates[0].attr_decl is target
        assert render(generator._gen_func_call(kt.Integer)) == 'other.foo()'


@pytest.mark.parametrize('existing_receiver', [False, True],
                         ids=['fresh-class-reference', 'object-reference'])
def test_self_reference_is_rejected_even_without_the_name_shortcut(
        monkeypatch: pytest.MonkeyPatch, existing_receiver: bool) -> None:
    generator, context = make_generator()
    source = make_function('foo', method=True)
    cls = add_class(context, 'C', source)
    # A nested block's last namespace component no longer equals the method name.
    generator.namespace = ast.GLOBAL_NAMESPACE + ('C', 'foo', 'true_block')
    signature = generator.bt_factory.get_function_type(0).new([kt.Integer])
    if existing_receiver:
        other = ast.ParameterDeclaration('other', cls.get_type())
        context.add_var(generator.namespace, other.name, other)
    select_expression(monkeypatch, generator, generator.gen_new)
    control = generator._gen_func_ref(signature, only_leaves=True)
    assert control is not None and control.target_decl is source
    assert render(control, context) == ('other::foo' if existing_receiver else 'C()::foo')

    with context.call_contexts(subtree_pushed_call_context=[
            InliningSource(source, IrFunctionBodyStub()),
            FunctionBodyGeneration(source)]):
        assert generator._gen_func_ref(signature, only_leaves=True) is None
        assert not generator.inline_call_graph.direct_successors


@pytest.mark.parametrize('method', [False, True], ids=['top-level', 'final-member'])
def test_generated_body_frame_and_registered_declaration_are_the_same_object(
        monkeypatch: pytest.MonkeyPatch, method: bool) -> None:
    generator, context = make_generator()
    if method:
        cls = ast.ClassDeclaration('C', [], ast.ClassDeclaration.REGULAR,
                                   fields=[], functions=[], is_final=True)
        context.add_class(ast.GLOBAL_NAMESPACE, cls.name, cls)
        generator.namespace += (cls.name,)
    initial_namespace = generator.namespace
    seen: list[ast.FunctionDeclaration] = []
    monkeypatch.setattr(cfg.limits, 'max_var_decls', 0)

    def body_probe(etype: tp.Type) -> ast.Expr:
        frame = context.current_call_context(FunctionBodyGeneration)
        assert isinstance(frame, FunctionBodyGeneration) and frame.callee is not None
        source = generator._current_inline_source()
        assert source is not None and source[0] is frame.callee
        assert context.get_funcs(generator.namespace)[frame.callee.name] is frame.callee
        assert generator._get_matching_function_declarations(etype, False) == []
        assert generator._get_matching_class_decls(etype, False, 'functions') == []
        seen.append(frame.callee)
        return ast.IntegerConstant(1, kt.Integer)

    select_expression(monkeypatch, generator, body_probe)
    # Do not replace _gen_func_body: it must establish the real frames itself.
    func = generator.gen_func_decl(
        etype=kt.Integer, not_void=True, func_name='foo', params=[], type_params=[],
        class_is_final=method, visibility=ast.Visibilities.PUBLIC,
    )
    assert seen == [func] and seen[0] is func
    assert func.is_inline and func in generator.inline_functions
    assert context.get_funcs(initial_namespace)[func.name] is func
    assert generator.namespace == initial_namespace
    assert generator._current_inline_source() is None
    assert context.current_call_context(FunctionBodyGeneration) is None
    if method:
        attrs = generator._get_class_attributes(cls, 'functions')
        assert len(attrs) == 1 and attrs[0] is func


@pytest.mark.parametrize('class_final', [False, True])
@pytest.mark.parametrize('override', [False, True])
@pytest.mark.parametrize('inherited_default', [False, True])
@pytest.mark.parametrize('abstract', [False, True])
def test_generated_inline_method_requires_final_class_and_no_inherited_default(
        monkeypatch: pytest.MonkeyPatch, class_final: bool, override: bool,
        inherited_default: bool, abstract: bool) -> None:
    generator, context = make_generator()
    cls = ast.ClassDeclaration('C', [], ast.ClassDeclaration.REGULAR,
                               fields=[], functions=[], is_final=class_final)
    context.add_class(ast.GLOBAL_NAMESPACE, cls.name, cls)
    generator.namespace += (cls.name,)
    monkeypatch.setattr(cfg.limits, 'max_var_decls', 0)
    select_expression(monkeypatch, generator,
                      lambda etype: ast.IntegerConstant(1, kt.Integer))
    func = generator.gen_func_decl(
        etype=kt.Integer, not_void=True, func_name='foo', params=[], type_params=[],
        class_is_final=class_final, declared_as_override=override,
        inherits_param_with_default=inherited_default, abstract=abstract,
        visibility=ast.Visibilities.PUBLIC,
    )
    assert func.is_inline is (class_final and not inherited_default and not abstract)
    # An override in a final class can be inline; "never inline overrides" is false.
    assert func.override is override
    assert (func in generator.inline_functions) is func.is_inline


@pytest.mark.parametrize('class_final', [False, True], ids=['open', 'final'])
def test_class_generation_passes_its_actual_finality_to_method_generation(
        monkeypatch: pytest.MonkeyPatch, class_final: bool) -> None:
    generator, context = make_generator()
    monkeypatch.setattr(cfg.prob.class_type, 'regular', 1.0)
    monkeypatch.setattr(cfg.prob.class_declaration_modality, 'declaration_final',
                        1.0 if class_final else 0.0)
    monkeypatch.setattr(cfg.prob, 'parameterized_functions', 0.0)
    monkeypatch.setattr(cfg.limits, 'max_type_params', 0)
    monkeypatch.setattr(cfg.limits, 'max_var_decls', 0)
    monkeypatch.setattr(cfg.limits.cls, 'max_fields', 0)
    monkeypatch.setattr(cfg.limits.cls, 'max_funcs', 1)
    monkeypatch.setattr(cfg.limits.fn, 'max_params', 0)
    select_expression(monkeypatch, generator,
                      lambda etype: ast.IntegerConstant(1, kt.Integer))
    signature = generator.bt_factory.get_function_type(0).new([kt.Integer])
    cls = generator.gen_class_decl(class_name='C', signature=signature)
    assert cls.is_final is class_final
    assert len(cls.functions) == 1
    func = cls.functions[0]
    assert func.is_inline is class_final
    assert context.get_funcs(ast.GLOBAL_NAMESPACE + (cls.name,))[func.name] is func
    assert generator._get_class_attributes(cls, 'functions')[0] is func


def test_final_inline_owner_cannot_enter_the_inheritance_copy_path(
        monkeypatch: pytest.MonkeyPatch) -> None:
    generator, context = make_generator()
    monkeypatch.setattr(cfg.limits, 'max_var_decls', 0)
    select_expression(monkeypatch, generator,
                      lambda etype: ast.IntegerConstant(1, kt.Integer))
    final_class = ast.ClassDeclaration('Final', [], ast.ClassDeclaration.REGULAR,
                                       fields=[], functions=[], is_final=True)
    context.add_class(ast.GLOBAL_NAMESPACE, final_class.name, final_class)
    generator.namespace = ast.GLOBAL_NAMESPACE + (final_class.name,)
    inline = generator.gen_func_decl(
        etype=kt.Integer, not_void=True, func_name='foo', params=[], type_params=[],
        class_is_final=True, visibility=ast.Visibilities.PUBLIC,
    )
    assert inline.is_inline
    generator.namespace = ast.GLOBAL_NAMESPACE + ('Child',)
    assert generator._select_superclass(only_interfaces=False) is None

    open_class = ast.ClassDeclaration('Open', [], ast.ClassDeclaration.REGULAR,
                                      fields=[], functions=[], is_final=False)
    context.add_class(ast.GLOBAL_NAMESPACE, open_class.name, open_class)
    generator.namespace = ast.GLOBAL_NAMESPACE + (open_class.name,)
    ordinary = generator.gen_func_decl(
        etype=kt.Integer, not_void=True, func_name='foo', params=[], type_params=[],
        class_is_final=False, visibility=ast.Visibilities.PUBLIC,
    )
    assert not ordinary.is_inline
    generator.namespace = ast.GLOBAL_NAMESPACE + ('Child',)
    selected = generator._select_superclass(only_interfaces=False)
    assert selected is not None and selected.super_cls is open_class
    child = ast.ClassDeclaration('Child', [selected.super_inst],
                                 ast.ClassDeclaration.REGULAR,
                                 fields=[], functions=[], is_final=True)
    context.add_class(ast.GLOBAL_NAMESPACE, child.name, child)
    inherited = generator._get_class_attributes(child, 'functions')
    assert len(inherited) == 1
    assert inherited[0] is not ordinary
    assert isinstance(inherited[0], ast.FunctionDeclaration)
    assert inherited[0].name == ordinary.name and not inherited[0].is_inline
    assert all(attr is not inline for attr in inherited)