from src.generators.generator import FunctionBodyGeneration, Generator
from src.ir import ast, kotlin_types as kt
from src.ir.context import Context
from src.ir.data_structures import IncrementalDAGTransitiveClosure


def make_generator():
    generator = Generator("kotlin")
    generator.context = Context()
    generator.inline_call_graph = IncrementalDAGTransitiveClosure()
    return generator


def make_function(name, is_inline, ret_type=kt.Integer):
    return ast.FunctionDeclaration(
        name=name,
        params=[],
        ret_type=ret_type,
        body=None,
        func_type=ast.FunctionDeclaration.FUNCTION,
        is_inline=is_inline,
        visibility=ast.Visibilities.PUBLIC,
    )


def make_int_function_signature(generator):
    return generator.bt_factory.get_function_type(0).new([kt.Integer])


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


"""
Kotlin shape:

inline fun sink(noinline p: () -> Int): Int = p()

inline fun f(): Int = sink(::g)
inline fun g(): Int = 1

Direct callable references to known inline declarations are recorded as
f -> g, even when the reference is intended for a noinline parameter.
"""
def test_direct_function_reference_records_inline_edge():
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        ref = generator._gen_func_ref(signature, only_leaves=True)

    assert ref.target_decl is callee
    assert generator.inline_call_graph.reaches(source, callee)


"""
Kotlin shape:

inline fun f() {
    val r = ::g
}

fun g() {
    f()
}

Callable references only create inline graph edges when the resolved target is
inline. A reference to non-inline g inside inline f is accepted by Native.
"""
def test_function_reference_to_non_inline_target_is_not_inline_edge():
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=False)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        ref = generator._gen_func_ref(signature, only_leaves=True)

    assert ref.target_decl is callee
    assert not generator.inline_call_graph.reaches(source, callee)


"""
Kotlin shape:

inline fun h(): Int {
    val ref = <!INLINE_CALL_CYCLE!>::g<!>
    return 1
}

inline fun g(): Int = h()

Even storing an unused callable reference inside an inline body creates an
inline graph edge h -> g.
"""
def test_local_function_reference_inside_inline_body_rejects_cycle(monkeypatch):
    generator = make_generator()
    source = make_function("h", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(callee, source)
    monkeypatch.setattr(generator, "_get_matching_class",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(generator, "_gen_matching_func",
                        lambda *args, **kwargs: None)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        ref = generator._gen_func_ref(signature, only_leaves=True)

    assert ref is None
    assert not generator.inline_call_graph.reaches(source, callee)


"""
Kotlin shape:

inline fun sink(noinline p: () -> Int): Int = p()

inline fun f(): Int = sink(<!INLINE_CALL_CYCLE!>::g<!>)
inline fun g(): Int = sink(::f)

If g already reaches f, creating ::g under f must be rejected.
"""
def test_direct_function_reference_rejects_inline_cycle(monkeypatch):
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(callee, source)
    monkeypatch.setattr(generator, "_get_matching_class",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(generator, "_gen_matching_func",
                        lambda *args, **kwargs: None)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        ref = generator._gen_func_ref(signature, only_leaves=True)

    assert ref is None
    assert not generator.inline_call_graph.reaches(source, callee)


"""
Kotlin shape:

inline fun sink(p: () -> Int): Int = p()

inline fun f(): Int = sink(<!INLINE_CALL_CYCLE!>::g<!>)
inline fun g(): Int = sink(::f)

Callable references to inline declarations are graph edges even when the
target parameter is inline rather than noinline.
"""
def test_direct_function_reference_for_inline_parameter_rejects_cycle(
        monkeypatch):
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(callee, source)
    monkeypatch.setattr(generator, "_get_matching_class",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(generator, "_gen_matching_func",
                        lambda *args, **kwargs: None)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        ref = generator._gen_func_ref(signature, only_leaves=True)

    assert ref is None
    assert not generator.inline_call_graph.reaches(source, callee)


"""
Kotlin shape:

inline fun f(): () -> Int = <!INLINE_CALL_CYCLE!>::g<!>
inline fun g(): Int = f()()

Returning a callable reference from an inline body still creates an inline
graph edge f -> g.
"""
def test_returned_function_reference_rejects_inline_cycle(monkeypatch):
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(callee, source)
    monkeypatch.setattr(generator, "_get_matching_class",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(generator, "_gen_matching_func",
                        lambda *args, **kwargs: None)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        ref = generator._gen_func_ref(signature, only_leaves=True)

    assert ref is None
    assert not generator.inline_call_graph.reaches(source, callee)


"""
Kotlin shape:

val p: () -> Int = ::g
inline fun f(): Int = p()
inline fun g(): Int = f()

Kotlin/Native accepts this. A top-level function-typed variable call does not
preserve callable-reference provenance as an inline edge back to g.
"""
def test_top_level_function_reference_variable_call_is_not_inline_edge():
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    ref_var = ast.VariableDeclaration(
        "p",
        ast.FunctionReference("g", None, signature, target_decl=callee),
        var_type=signature,
    )
    generator.context.add_var(ast.GLOBAL_NAMESPACE, ref_var.name, ref_var)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(callee, source)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        ref_call = generator._gen_func_call_ref(kt.Integer, only_leaves=True)

    assert ref_call.func == "p"
    assert ref_call.args == []
    assert ref_call.is_ref_call
    assert not generator.inline_call_graph.reaches(source, callee)


"""
Kotlin shape:

fun h(): Int {
    val ref = ::g
    return 1
}
inline fun f(): Int = h()
inline fun g(): Int = f()

Kotlin/Native accepts this. A separate non-inline function body is a boundary,
so its callable reference does not inherit f as an inline graph source.
"""
def test_function_reference_inside_non_inline_boundary_is_not_inline_edge():
    generator = make_generator()
    source = make_function("h", is_inline=False)
    f = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(callee, f)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        ref = generator._gen_func_ref(signature, only_leaves=True)

    assert ref.target_decl is callee
    assert not generator.inline_call_graph.reaches(source, callee)


"""
Kotlin shape:

class A { fun m(): Int = 1 }
inline fun makeA(): A { <!INLINE_CALL_CYCLE!>g()<!>; return A() }
inline fun f(): () -> Int = <!INLINE_CALL_CYCLE!>makeA()<!>::m
inline fun g(): Int = f()()

The bound receiver reference is rejected because generating the receiver
expression would require the cyclic inline call f -> makeA.
"""
def test_bound_receiver_reference_rejects_cyclic_inline_receiver():
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    cls = ast.ClassDeclaration("A", [], ast.ClassDeclaration.REGULAR)
    make_a = make_function("makeA", is_inline=True, ret_type=cls.get_type())
    generator.context.add_func(ast.GLOBAL_NAMESPACE, make_a.name, make_a)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(make_a, callee)
    assert generator.inline_call_graph.add_edge(callee, source)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[FunctionBodyGeneration(source)]):
        candidates = generator._get_matching_function_declarations(
            cls.get_type(), subtype=False)

    assert all(candidate.attr_decl is not make_a for candidate in candidates)


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
# inline fun sink(noinline p: () -> Int = <!INLINE_CALL_CYCLE!>::g<!>): Int = p()
# inline fun g(): Int = <!INLINE_CALL_CYCLE!>sink()<!>
#
# Future behavior:
# An omitted default argument should make the default-reference edge
# participate in the inline graph and reject this cycle.
#
# Related accepted shape:
#
# inline fun sink(noinline p: () -> Int = ::g): Int = p()
# inline fun g(): Int = sink { 1 }
#
# Passing an explicit argument must not use the default-reference edge.
# """
# def test_noinline_default_function_reference_cycle_not_modeled_yet():
#     generator = make_generator()
#     sink = make_function("sink", is_inline=True)
#     g = make_function("g", is_inline=True)
#     assert generator.inline_call_graph.add_edge(g, sink)
#
#     # Future omitted-default modeling should reject sink-default -> g here,
#     # but explicit sink { 1 } should not create that edge.
#     assert not generator.inline_call_graph.can_add_edge(sink, g)
#
#
# """
# Kotlin shape:
#
# class A { inline fun m(): Int = <!INLINE_CALL_CYCLE!>g()<!> }
# inline fun f(): (A) -> Int = <!INLINE_CALL_CYCLE!>A::m<!>
# inline fun g(): Int = <!INLINE_CALL_CYCLE!>f()<!>(A())
#
# Future behavior:
# Unbound member callable references should record f -> A.m when Hephaestus
# starts generating the A::m shape.
#
# Current reason this is not represented:
# _gen_func_ref() currently creates references to available functions or to
# methods through generated/available receiver expressions, i.e. bound
# receiver-style references, not unbound A::m references.
# """
# def test_unbound_member_function_reference_not_modeled_yet():
#     generator = make_generator()
#     f = make_function("f", is_inline=True)
#     g = make_function("g", is_inline=True)
#     m = make_function("m", is_inline=True)
#     assert generator.inline_call_graph.add_edge(m, g)
#     assert generator.inline_call_graph.add_edge(g, f)
#
#     # Future unbound-member reference modeling should reject f -> m.
#     with generator.context.call_contexts(
#             subtree_pushed_call_context=[FunctionBodyGeneration(f)]):
#         assert not generator._inline_edge_allowed(m)
#
#
# """
# Kotlin shape:
#
# fun h(p: () -> Unit) {}
#
# inline fun f() {
#     h {
#         val r = <!INLINE_CALL_CYCLE!>::g<!>
#     }
# }
# inline fun g() {
#     f()
# }
#
# Future behavior:
# Lambda body generation physically inside an inline body should keep f as the
# inline graph source when Kotlin's checker traverses that lambda under f.
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
#
#
# """
# Kotlin shape:
#
# inline fun f() {
#     val o = object {
#         fun m() {
#             val r = <!INLINE_CALL_CYCLE!>::g<!>
#         }
#
#         val p: () -> Unit
#             get() = <!INLINE_CALL_CYCLE!>::g<!>
#     }
# }
# inline fun g() {
#     f()
# }
#
# Future behavior:
# Anonymous object members declared physically inside an inline body should not
# be treated as independent non-inline helper bodies. Their function references
# should still inherit f as the inline graph source.
#
# Current reason this is not represented:
# The current unit tests exercise inline source tracking through direct
# FunctionBodyGeneration frames, but do not model anonymous object member
# bodies as physically nested inside an enclosing inline body.
# """
# def test_function_reference_inside_inline_object_member_not_modeled_yet():
#     generator = make_generator()
#     f = make_function("f", is_inline=True)
#     g = make_function("g", is_inline=True)
#     assert generator.inline_call_graph.add_edge(g, f)
#
#     with generator.context.call_contexts(
#             subtree_pushed_call_context=[FunctionBodyGeneration(f)]):
#         # Future anonymous-object member generation should still see f as
#         # source and reject g.
#         assert not generator._inline_edge_allowed(g)
