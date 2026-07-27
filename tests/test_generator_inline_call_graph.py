from src.generators.generator import FunctionBodyGeneration, Generator
from src.ir import ast, kotlin_types as kt
from src.ir.context import Context
from src.ir.data_structures import IncrementalDAGTransitiveClosure


def make_generator():
    generator = Generator("kotlin")
    generator.context = Context()
    generator.inline_call_graph = IncrementalDAGTransitiveClosure()
    return generator


def make_function(name, is_inline):
    return ast.FunctionDeclaration(
        name=name,
        params=[],
        ret_type=kt.Integer,
        body=None,
        func_type=ast.FunctionDeclaration.FUNCTION,
        is_inline=is_inline,
        visibility=ast.Visibilities.PUBLIC,
    )


"""
Kotlin shape:

inline fun callee(): Int = 1
fun ordinary(): Int = callee()

There is no active inline source while generating ordinary(), so callee()
does not create an inline graph edge.
"""
def test_inline_edge_not_recorded_without_active_source():
    generator = make_generator()
    callee = make_function("callee", is_inline=True)

    assert generator._inline_edge_allowed(callee)
    generator._record_inline_edge(callee)

    assert not generator.inline_call_graph.direct_successors


"""
Kotlin shape:

inline fun source(): Int = callee()
inline fun callee(): Int = 1

Direct inline calls from an inline body are recorded as source -> callee.
"""
def test_inline_edge_recorded_for_active_inline_source():
    generator = make_generator()
    source = make_function("source", is_inline=True)
    callee = make_function("callee", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        assert generator._current_inline_source() is source
        assert generator._inline_edge_allowed(callee)
        generator._record_inline_edge(callee)

    assert generator.inline_call_graph.reaches(source, callee)


"""
Kotlin shape:

inline fun source(): Int = <!INLINE_CALL_CYCLE!>callee()<!>
inline fun callee(): Int = source()

If callee already reaches source, source -> callee must be rejected.
"""
def test_inline_edge_rejects_transitive_cycle():
    generator = make_generator()
    source = make_function("source", is_inline=True)
    callee = make_function("callee", is_inline=True)
    assert generator.inline_call_graph.add_edge(callee, source)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        assert not generator._inline_edge_allowed(callee)


"""
Kotlin shape:

inline fun source(): Int = callee()
fun callee(): Int = 1

Non-inline callees do not create inline graph edges.
"""
def test_non_inline_callee_does_not_touch_graph():
    generator = make_generator()
    source = make_function("source", is_inline=True)
    callee = make_function("callee", is_inline=False)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        assert generator._inline_edge_allowed(callee)
        generator._record_inline_edge(callee)

    assert not generator.inline_call_graph.direct_successors


"""
Kotlin shape:

inline fun source(): Int = helper()
inline fun helper(): Int = <!INLINE_CALL_CYCLE!>source()<!>

The source -> helper edge is pre-registered before helper body generation,
so helper body generation can reject helper -> source.
"""
def test_required_inline_call_source_is_registered_before_body(monkeypatch):
    generator = make_generator()
    source = make_function("source", is_inline=True)

    def fake_body(ret_type, func):
        assert generator.inline_call_graph.reaches(source, func)
        with generator.context.call_contexts(
                subtree_pushed_call_context=[FunctionBodyGeneration(func)]):
            assert not generator._inline_edge_allowed(source)
        return ast.BottomConstant(ret_type)

    monkeypatch.setattr(generator, "_gen_func_body", fake_body)

    func = generator.gen_func_decl(
        etype=kt.Integer,
        not_void=True,
        func_name="helper",
        params=[],
        type_params=[],
        visibility=ast.Visibilities.PUBLIC,
        required_inline_call_source=source,
    )

    assert func.is_inline
    assert generator.inline_call_graph.reaches(source, func)


# TODO(inline-call-graph): Uncomment these tests when the corresponding currently-unmodeled edge kinds are implemented.

# """
# Kotlin shape:
#
# inline fun foo(x: Int = <!INLINE_CALL_CYCLE!>bar()<!>): Int = x
# inline fun bar(x: Int = <!INLINE_CALL_CYCLE!>foo()<!>): Int = x
# inline fun qux(x: Int = quz(42)): Int = x
# inline fun quz(x: Int = qux(42)): Int = x
#
# Future behavior:
# Default-value graph sources should catch the foo <-> bar default cycle.
# The qux/quz pair should remain allowed because explicit 42 avoids default
# expansion.
#
# Current reason this is missed:
# Default expressions are generated before the FunctionDeclaration object is
# available as a FunctionBodyGeneration source frame.
# """
# def test_default_argument_inline_cycle_not_modeled_yet():
#     generator = make_generator()
#     foo = make_function("foo", is_inline=True)
#     bar = make_function("bar", is_inline=True)
#     qux = make_function("qux", is_inline=True)
#     quz = make_function("quz", is_inline=True)
#
#     assert generator.inline_call_graph.add_edge(foo, bar)
#     assert not generator.inline_call_graph.can_add_edge(bar, foo)
#
#     assert generator.inline_call_graph.add_edge(qux, quz)
#     # Future default-argument modeling should distinguish quz(42) from quz().
#     assert generator.inline_call_graph.can_add_edge(quz, qux)
#
#
# """
# Kotlin shape:
#
# inline fun sink(noinline p: () -> Int): Int = p()
#
# inline fun f(): Int = sink(::g)
# inline fun g(): Int = sink(::f)
#
# Future behavior:
# Callable references passed through noinline parameters should still be
# modeled if Kotlin reports an inline call cycle for this shape.
#
# Current reason this is missed:
# signature=True paths deliberately bypass inline graph filtering in v1.
# """
# def test_noinline_callable_reference_cycle_not_modeled_yet():
#     generator = make_generator()
#     f = make_function("f", is_inline=True)
#     g = make_function("g", is_inline=True)
#
#     assert generator.inline_call_graph.add_edge(g, f)
#     with generator.context.call_contexts(
#             subtree_pushed_call_context=[FunctionBodyGeneration(f)]):
#         # Future behavior should reject ::g as an inline graph edge f -> g.
#         assert not generator._inline_edge_allowed(g)
#
#
# """
# Kotlin shape:
#
# val p: () -> Int = ::g
# inline fun f(): Int = p()
# inline fun g(): Int = f()
#
# Future behavior:
# Function-typed variable calls should preserve provenance from p back to g.
#
# Current reason this is missed:
# FunctionCall(..., is_ref_call=True) only stores the variable name/signature,
# not the original FunctionDeclaration referenced by ::g.
# """
# def test_function_typed_variable_provenance_not_modeled_yet():
#     generator = make_generator()
#     f = make_function("f", is_inline=True)
#     g = make_function("g", is_inline=True)
#     signature = kt.KotlinBuiltinFactory().get_function_type(0).new(
#         [kt.Integer])
#     p = ast.VariableDeclaration(
#         "p",
#         ast.FunctionReference("g", None, signature),
#         var_type=signature,
#     )
#     generator.context.add_var(ast.GLOBAL_NAMESPACE, "p", p)
#     assert generator.inline_call_graph.add_edge(g, f)
#
#     with generator.context.call_contexts(
#             subtree_pushed_call_context=[FunctionBodyGeneration(f)]):
#         # Future behavior should reject p(), because p points to g.
#         assert not generator._inline_edge_allowed(g)
#
#
# """
# Kotlin shape:
#
# inline fun f(): Int {
#     val l = { g() }
#     return 1
# }
# inline fun g(): Int = f()
#
# Future behavior:
# Lambda body generation inside an inline body should keep f as the inline
# graph source when Kotlin's checker traverses that lambda under f.
#
# Current reason this is missed:
# gen_lambda() calls _gen_func_body(ret_type) with func=None, which pushes
# FunctionBodyGeneration(None) and hides the outer inline source.
# """
# def test_lambda_body_inside_inline_body_source_not_modeled_yet():
#     generator = make_generator()
#     f = make_function("f", is_inline=True)
#     g = make_function("g", is_inline=True)
#     assert generator.inline_call_graph.add_edge(g, f)
#
#     with generator.context.call_contexts(
#             subtree_pushed_call_context=[FunctionBodyGeneration(f)]):
#         with generator.context.call_contexts(
#                 subtree_pushed_call_context=[FunctionBodyGeneration(None)]):
#             # Future behavior should still see f as source and reject g.
#             assert not generator._inline_edge_allowed(g)
