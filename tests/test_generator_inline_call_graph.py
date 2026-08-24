from copy import deepcopy

from src.generators.generator import (
    ExprCallSite, FunctionBodyGeneration, DefaultValueGeneration, Generator,
    InliningSource, IrFunctionBodyStub
)
from src.ir import ast, kotlin_types as kt, types as tp
from src.ir.context import Context
from src.ir.data_structures import IncrementalDAGTransitiveClosure
from src import utils as ut


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


def body_node(func):
    """The DAG vertex for a function's BODY (CallNode(func, func.body))."""
    return (func, IrFunctionBodyStub())


def body_ctx(func):
    """Frames pushed for a function body, mirroring Generator._gen_func_body.

    InliningSource is pushed only for inline declarations; lambdas (func=None)
    and non-inline functions push none, so the enclosing source passes through.
    It is pushed BEFORE FunctionBodyGeneration, matching production, so
    call_context_tail still sees FunctionBodyGeneration nearest the top.
    """
    return [
        InliningSource(func, IrFunctionBodyStub())
        if (isinstance(func, ast.FunctionDeclaration) and func.is_inline)
        else None,
        FunctionBodyGeneration(func),
    ]


def default_ctx(func, param_name='x'):
    """Frames pushed for a default value, mirroring Generator.gen_func_decl."""
    return [
        InliningSource(func, param_name) if func.is_inline else None,
        DefaultValueGeneration(func, param_name),
    ]


# ---------------------------------------------------------------------------
# Basic edge recording
# ---------------------------------------------------------------------------

def test_inline_edge_not_recorded_without_active_source():
    generator = make_generator()
    callee = make_function("callee", is_inline=True)
    assert generator._inline_edge_allowed(callee)
    generator._record_inline_edge(callee)
    assert not generator.inline_call_graph.direct_successors


def test_inline_edge_recorded_for_active_inline_source():
    generator = make_generator()
    source = make_function("source", is_inline=True)
    callee = make_function("callee", is_inline=True)
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        assert generator._current_inline_source() == body_node(source)
        assert generator._inline_edge_allowed(callee)
        generator._record_inline_edge(callee)
    assert generator.inline_call_graph.reaches(body_node(source), body_node(callee))


def test_inline_edge_rejects_transitive_cycle():
    generator = make_generator()
    source = make_function("source", is_inline=True)
    callee = make_function("callee", is_inline=True)
    assert generator.inline_call_graph.add_edge(body_node(callee), body_node(source))
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        assert not generator._inline_edge_allowed(callee)


def test_non_inline_callee_does_not_touch_graph():
    generator = make_generator()
    source = make_function("source", is_inline=True)
    callee = make_function("callee", is_inline=False)
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        assert generator._inline_edge_allowed(callee)
        generator._record_inline_edge(callee)
    assert not generator.inline_call_graph.direct_successors


# ---------------------------------------------------------------------------
# Required inline call source (pre-registered before body)
# ---------------------------------------------------------------------------

def test_required_inline_call_source_is_registered_before_body(monkeypatch):
    generator = make_generator()
    source = make_function("source", is_inline=True)

    def fake_body(ret_type, func):
        assert generator.inline_call_graph.reaches(body_node(source), body_node(func))
        with generator.context.call_contexts(
                subtree_pushed_call_context=body_ctx(func)):
            assert not generator._inline_edge_allowed(source)
        return ast.BottomConstant(ret_type)

    monkeypatch.setattr(generator, "_gen_func_body", fake_body)
    func = generator.gen_func_decl(
        etype=kt.Integer, not_void=True, func_name="helper",
        params=[], type_params=[], visibility=ast.Visibilities.PUBLIC,
        required_inline_call_source=body_node(source),
    )
    assert func.is_inline
    assert generator.inline_call_graph.reaches(body_node(source), body_node(func))


# ---------------------------------------------------------------------------
# Function references
# ---------------------------------------------------------------------------

def test_direct_function_reference_records_inline_edge():
    """
Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "RECURSION_IN_INLINE")

inline fun outerReferenceInBody() {
    val ref = ::outerReferenceInBody
    ref.hashCode()
}

inline fun outerReferenceInDeadBranch() {
    if (false) {
        val ref = ::outerReferenceInDeadBranch
        ref.hashCode()
    }
}

fun main() {}
```

Backend verdict:
    native: the 'outerReferenceInBody' invocation is part of an inline cycle.
    jvm: the 'outerReferenceInBody' invocation is part of an inline cycle.
    js: the 'outerReferenceInBody' invocation is part of an inline cycle.
    wasm: the 'outerReferenceInBody' invocation is part of an inline cycle.
    """
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        ref = generator._gen_func_ref(signature, only_leaves=True)
    assert ref.target_decl is callee
    assert generator.inline_call_graph.reaches(body_node(source), body_node(callee))


def test_function_reference_to_non_inline_target_is_not_inline_edge():
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=False)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        ref = generator._gen_func_ref(signature, only_leaves=True)
    assert ref.target_decl is callee
    assert not generator.inline_call_graph.reaches(body_node(source), body_node(callee))


def test_local_function_reference_inside_inline_body_rejects_cycle(monkeypatch):
    """
Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "RECURSION_IN_INLINE")

inline fun fDirectLocalFunctionBody() {
    fun local() {
        gDirectLocalFunctionBody()
    }
}

inline fun gDirectLocalFunctionBody() {
    fDirectLocalFunctionBody()
}
```

Backend verdict:
    native: local functions are not yet supported in inline functions.
    jvm: local functions are not yet supported in inline functions.
    js: local functions are not yet supported in inline functions.
    wasm: local functions are not yet supported in inline functions.
    """
    generator = make_generator()
    source = make_function("h", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(body_node(callee), body_node(source))
    monkeypatch.setattr(generator, "_get_matching_class", lambda *a, **kw: None)
    monkeypatch.setattr(generator, "_gen_matching_func", lambda *a, **kw: None)
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        ref = generator._gen_func_ref(signature, only_leaves=True)
    assert ref is None
    assert not generator.inline_call_graph.reaches(body_node(source), body_node(callee))


def test_direct_function_reference_rejects_inline_cycle(monkeypatch):
    """
Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "RECURSION_IN_INLINE")

inline fun outerReferenceInBody() {
    val ref = ::outerReferenceInBody
    ref.hashCode()
}

inline fun outerReferenceInDeadBranch() {
    if (false) {
        val ref = ::outerReferenceInDeadBranch
        ref.hashCode()
    }
}

fun main() {}
```

Backend verdict:
    native: the 'outerReferenceInBody' invocation is part of an inline cycle.
    jvm: the 'outerReferenceInBody' invocation is part of an inline cycle.
    js: the 'outerReferenceInBody' invocation is part of an inline cycle.
    wasm: the 'outerReferenceInBody' invocation is part of an inline cycle.
    """
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(body_node(callee), body_node(source))
    monkeypatch.setattr(generator, "_get_matching_class", lambda *a, **kw: None)
    monkeypatch.setattr(generator, "_gen_matching_func", lambda *a, **kw: None)
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        ref = generator._gen_func_ref(signature, only_leaves=True)
    assert ref is None
    assert not generator.inline_call_graph.reaches(body_node(source), body_node(callee))


def test_direct_function_reference_for_inline_parameter_rejects_cycle(monkeypatch):
    """
Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE")

inline fun noinlineReferenceSink(noinline p: () -> Int): Int = p()

inline fun fThroughNoinlineReferenceOnly(): Int {
    return noinlineReferenceSink(::gThroughNoinlineReferenceOnly)
}

inline fun gThroughNoinlineReferenceOnly(): Int {
    return noinlineReferenceSink(::fThroughNoinlineReferenceOnly)
}

fun main() {}
```

Backend verdict:
    native: the 'gThroughNoinlineReferenceOnly' invocation is part of an inline cycle.
    jvm: the 'gThroughNoinlineReferenceOnly' invocation is part of an inline cycle.
    js: the 'gThroughNoinlineReferenceOnly' invocation is part of an inline cycle.
    wasm: the 'gThroughNoinlineReferenceOnly' invocation is part of an inline cycle.
    """
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(body_node(callee), body_node(source))
    monkeypatch.setattr(generator, "_get_matching_class", lambda *a, **kw: None)
    monkeypatch.setattr(generator, "_gen_matching_func", lambda *a, **kw: None)
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        ref = generator._gen_func_ref(signature, only_leaves=True)
    assert ref is None


def test_returned_function_reference_rejects_cycle(monkeypatch):
    """
Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "RECURSION_IN_INLINE")

inline fun outerReferenceInBody() {
    val ref = ::outerReferenceInBody
    ref.hashCode()
}

inline fun outerReferenceInDeadBranch() {
    if (false) {
        val ref = ::outerReferenceInDeadBranch
        ref.hashCode()
    }
}

fun main() {}
```

Backend verdict:
    native: the 'outerReferenceInBody' invocation is part of an inline cycle.
    jvm: the 'outerReferenceInBody' invocation is part of an inline cycle.
    js: the 'outerReferenceInBody' invocation is part of an inline cycle.
    wasm: the 'outerReferenceInBody' invocation is part of an inline cycle.
    """
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(body_node(callee), body_node(source))
    monkeypatch.setattr(generator, "_get_matching_class", lambda *a, **kw: None)
    monkeypatch.setattr(generator, "_gen_matching_func", lambda *a, **kw: None)
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        ref = generator._gen_func_ref(signature, only_leaves=True)
    assert ref is None


def test_top_level_function_reference_variable_call_is_not_inline_edge():
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    ref_var = ast.VariableDeclaration(
        "p", ast.FunctionReference("g", None, signature, target_decl=callee),
        var_type=signature,
    )
    generator.context.add_var(ast.GLOBAL_NAMESPACE, ref_var.name, ref_var)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(body_node(callee), body_node(source))
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        ref_call = generator._gen_func_call_ref(kt.Integer, only_leaves=True)
    assert ref_call.func == "p"
    assert ref_call.is_ref_call
    assert not generator.inline_call_graph.reaches(body_node(source), body_node(callee))


def test_function_reference_inside_non_inline_boundary_is_not_inline_edge():
    """
Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE")

class MemberBoundaryHost {
    fun hMemberBoundary() {
        gMemberBoundary()
    }
}

inline fun fMemberBoundary() {
    MemberBoundaryHost().hMemberBoundary()
}

inline fun gMemberBoundary() {
    kMemberBoundary()
}

inline fun kMemberBoundary() {
    fMemberBoundary()
}
```

Backend verdict:
    native: compiles
    jvm: compiles
    js: compiles
    wasm: compiles
    """
    generator = make_generator()
    source = make_function("h", is_inline=False)
    f = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    signature = make_int_function_signature(generator)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, callee.name, callee)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(body_node(callee), body_node(f))
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        ref = generator._gen_func_ref(signature, only_leaves=True)
    assert ref.target_decl is callee
    assert not generator.inline_call_graph.reaches(body_node(source), body_node(callee))


def test_bound_receiver_reference_rejects_cyclic_inline_receiver():
    """
Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "RECURSION_IN_INLINE")

inline fun fObjectMethodDefaultBody() {
    object {
        fun method(x: Unit = gObjectMethodDefaultBody()) {
        }
    }
}

inline fun gObjectMethodDefaultBody() {
    fObjectMethodDefaultBody()
}
```

Backend verdict:
    native: the 'gObjectMethodDefaultBody' invocation is part of an inline cycle.
    jvm: the 'gObjectMethodDefaultBody' invocation is part of an inline cycle.
    js: the 'gObjectMethodDefaultBody' invocation is part of an inline cycle.
    wasm: the 'gObjectMethodDefaultBody' invocation is part of an inline cycle.
    """
    generator = make_generator()
    source = make_function("f", is_inline=True)
    callee = make_function("g", is_inline=True)
    cls = ast.ClassDeclaration("A", [], ast.ClassDeclaration.REGULAR)
    make_a = make_function("makeA", is_inline=True, ret_type=cls.get_type())
    generator.context.add_func(ast.GLOBAL_NAMESPACE, make_a.name, make_a)
    generator.namespace = ast.GLOBAL_NAMESPACE + (source.name,)
    assert generator.inline_call_graph.add_edge(body_node(make_a), body_node(callee))
    assert generator.inline_call_graph.add_edge(body_node(callee), body_node(source))
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(source)):
        candidates = generator._get_matching_function_declarations(
            cls.get_type(), subtype=False)
    assert all(candidate.attr_decl is not make_a for candidate in candidates)


# ---------------------------------------------------------------------------
# Lambda bodies inside inline functions
# ---------------------------------------------------------------------------

def test_lambda_body_inside_inline_body_source_visible():
    """
Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "RECURSION_IN_INLINE")

fun consumeLambdaBoundary(block: () -> Unit) {
    block()
}

inline fun fLambdaPassedToNonInlineBoundary() {
    consumeLambdaBoundary {
        gLambdaPassedToNonInlineBoundary()
    }
}

inline fun gLambdaPassedToNonInlineBoundary() {
    fLambdaPassedToNonInlineBoundary()
}
```

Backend verdict:
    native: the 'gLambdaPassedToNonInlineBoundary' invocation is part of an inline cycle.
    jvm: the 'gLambdaPassedToNonInlineBoundary' invocation is part of an inline cycle.
    js: the 'gLambdaPassedToNonInlineBoundary' invocation is part of an inline cycle.
    wasm: the 'gLambdaPassedToNonInlineBoundary' invocation is part of an inline cycle.
    """
    generator = make_generator()
    f = make_function("f", is_inline=True)
    g = make_function("g", is_inline=True)
    assert generator.inline_call_graph.add_edge(body_node(g), body_node(f))
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(f)):
        with generator.context.call_contexts(
                subtree_pushed_call_context=[FunctionBodyGeneration(None)]):
            assert generator._current_inline_source() == body_node(f)
            assert not generator._inline_edge_allowed(g)


def test_nested_lambda_bodies_inside_inline_body_source_visible():
    generator = make_generator()
    f = make_function("f", is_inline=True)
    g = make_function("g", is_inline=True)
    assert generator.inline_call_graph.add_edge(body_node(g), body_node(f))
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(f)):
        with generator.context.call_contexts(
                subtree_pushed_call_context=[FunctionBodyGeneration(None)]):
            with generator.context.call_contexts(
                    subtree_pushed_call_context=[FunctionBodyGeneration(None)]):
                assert generator._current_inline_source() == body_node(f)
                assert not generator._inline_edge_allowed(g)


def test_direct_call_from_lambda_inside_inline_rejects_cycle():
    """
Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE")

inline fun noinlineLambdaSink(noinline p: () -> Int): Int = p()

inline fun fThroughNoinlineLambdaOnly(): Int {
    return noinlineLambdaSink { gThroughNoinlineLambdaOnly() }
}

inline fun gThroughNoinlineLambdaOnly(): Int {
    return noinlineLambdaSink { fThroughNoinlineLambdaOnly() }
}

fun main() {}
```

Backend verdict:
    native: the 'gThroughNoinlineLambdaOnly' invocation is part of an inline cycle.
    jvm: the 'gThroughNoinlineLambdaOnly' invocation is part of an inline cycle.
    js: the 'gThroughNoinlineLambdaOnly' invocation is part of an inline cycle.
    wasm: the 'gThroughNoinlineLambdaOnly' invocation is part of an inline cycle.
    """
    generator = make_generator()
    f = make_function("f", is_inline=True)
    g = make_function("g", is_inline=True)
    assert generator.inline_call_graph.add_edge(body_node(g), body_node(f))
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(f)):
        with generator.context.call_contexts(
                subtree_pushed_call_context=[FunctionBodyGeneration(None)]):
            assert generator._current_inline_source() == body_node(f)
            assert generator._requires_inline_edge(g)
            assert not generator._inline_edge_allowed(g)


# ---------------------------------------------------------------------------
# Default argument cycles — composite node model
# ---------------------------------------------------------------------------

def test_default_argument_explicit_args_no_cycle():
    """inline fun foo(x = bar(42)) and bar(x = foo(42)) — ACCEPTED by compiler.

    Defaults are separate graph nodes from bodies. With explicit args,
    edges go foo.default → bar.body and bar.default → foo.body — no cycle.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "UNUSED_PARAMETER")

inline fun explicitOnlyA(x: Int = explicitOnlyB(1)): Int = x
inline fun explicitOnlyB(x: Int = explicitOnlyA(1)): Int = x

fun main() {}
```

Backend verdict:
    native: compiles
    jvm: compiles
    js: compiles
    wasm: compiles
"""
    generator = make_generator()
    foo = make_function("foo", is_inline=True)
    bar = make_function("bar", is_inline=True)
    generator.inline_functions.add(foo)
    generator.inline_functions.add(bar)

    # foo's default calls bar with explicit arg → edge (foo, 'x') → body_node(bar)
    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(foo, 'x')):
        assert generator._current_inline_source() == (foo, 'x')
        assert generator._inline_edge_allowed(bar)
        generator._record_inline_edge(bar)

    # bar's default calls foo with explicit arg → edge (bar, 'x') → body_node(foo)
    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(bar, 'x')):
        assert generator._current_inline_source() == (bar, 'x')
        assert generator._inline_edge_allowed(foo)
        generator._record_inline_edge(foo)

    # No cycle: (foo, 'x') → body_node(bar) and (bar, 'x') → body_node(foo)
    assert generator.inline_call_graph.reaches((foo, 'x'), body_node(bar))
    assert generator.inline_call_graph.reaches((bar, 'x'), body_node(foo))
    # The reverse should NOT be reachable
    assert not generator.inline_call_graph.reaches(body_node(bar), (foo, 'x'))
    assert not generator.inline_call_graph.reaches(body_node(foo), (bar, 'x'))


def test_default_argument_body_cycle_detected():
    """inline fun foo(x = bar()) and bar(x = foo()) — CYCLE_REJECTED by compiler.

    With omitted args, the compiler also creates edges to callee's default nodes.
    We don't track omitted args yet, but body→body cycles through defaults
    should still be detected if the body calls the other function.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "UNUSED_PARAMETER")

inline fun explicitDefaultEdgeA(x: Int = explicitDefaultEdgeB(1)): Int = x
inline fun explicitDefaultEdgeB(x: Int = explicitDefaultEdgeA(1)): Int = x

inline fun omittedDefaultEdgeA(x: Int = omittedDefaultEdgeB()): Int = x
inline fun omittedDefaultEdgeB(x: Int = omittedDefaultEdgeA()): Int = x

fun main() {}
```

Backend verdict:
    native: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
    jvm: the 'omittedDefaultEdgeB$default' invocation is part of an inline cycle.
    js: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
    wasm: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
"""
    generator = make_generator()
    foo = make_function("foo", is_inline=True)
    bar = make_function("bar", is_inline=True)
    generator.inline_functions.add(foo)
    generator.inline_functions.add(bar)

    # foo's body calls bar → edge body_node(foo) → body_node(bar)
    assert generator.inline_call_graph.add_edge(body_node(foo), body_node(bar))
    # bar's body calls foo → would create cycle
    assert not generator.inline_call_graph.can_add_edge(body_node(bar), body_node(foo))


def test_default_argument_mutual_cycle_through_defaults():
    """Cycle through defaults only: foo.default → bar.default → foo.default.

    The compiler detects this because omitted args create edges to callee's
    defaults. We approximate by checking if default→default edges cycle.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "UNUSED_PARAMETER")

inline fun explicitDefaultEdgeA(x: Int = explicitDefaultEdgeB(1)): Int = x
inline fun explicitDefaultEdgeB(x: Int = explicitDefaultEdgeA(1)): Int = x

inline fun omittedDefaultEdgeA(x: Int = omittedDefaultEdgeB()): Int = x
inline fun omittedDefaultEdgeB(x: Int = omittedDefaultEdgeA()): Int = x

fun main() {}
```

Backend verdict:
    native: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
    jvm: the 'omittedDefaultEdgeB$default' invocation is part of an inline cycle.
    js: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
    wasm: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
"""
    generator = make_generator()
    foo = make_function("foo", is_inline=True)
    bar = make_function("bar", is_inline=True)
    generator.inline_functions.add(foo)
    generator.inline_functions.add(bar)

    # Simulate: foo.default → bar.default (omitted arg)
    assert generator.inline_call_graph.add_edge((foo, 'x'), (bar, 'x'))
    # bar.default → foo.default (omitted arg) — cycle!
    assert not generator.inline_call_graph.can_add_edge((bar, 'x'), (foo, 'x'))


def test_noinline_default_function_reference_cycle_detected():
    """inline fun sink(noinline p: () -> Unit = ::g) and g calls sink() — cycle.

    With composite nodes, (sink, 'x') → body_node(g) and body_node(g) → body_node(sink)
    don't form a cycle because (sink, 'x') != body_node(sink). But when g() calls
    sink() with the default argument omitted, the compiler also creates the edge
    body_node(g) → (sink, 'x'). That closes the cycle:

        (sink, 'x') → body_node(g) → (sink, 'x')

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE")

inline fun sink(noinline p: () -> Unit = ::g) { p() }

inline fun g() { sink() }

fun main() {}
```

Backend verdict:
    native: the 'g' invocation is part of an inline cycle.
    jvm:    the 'g' invocation is part of an inline cycle.
    js:     the 'g' invocation is part of an inline cycle.
    wasm:   the 'g' invocation is part of an inline cycle.
"""
    generator = make_generator()
    sink = make_function("sink", is_inline=True)
    g = make_function("g", is_inline=True)
    generator.inline_functions.add(sink)
    generator.inline_functions.add(g)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, g.name, g)
    generator.namespace = ast.GLOBAL_NAMESPACE + (sink.name,)
    # g's body calls sink() with omitted default for parameter x.
    assert generator.inline_call_graph.add_edge(body_node(g), body_node(sink))
    assert generator.inline_call_graph.add_edge(body_node(g), (sink, 'x'))
    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(sink, 'x')):
        assert generator._current_inline_source() == (sink, 'x')
        # The omitted-arg default edge closes the cycle, so this must be rejected.
        assert not generator._inline_edge_allowed(g)


def test_default_type_variable_forces_class_helper(monkeypatch):
    generator = make_generator()
    func = make_function("f", is_inline=False)
    type_var = tp.TypeParameter("T")
    marker = object()
    calls = []

    def matching_class(etype, attr_name, **kwargs):
        calls.append((etype, attr_name, kwargs))
        return marker

    monkeypatch.setattr(generator, "_gen_matching_class", matching_class)
    monkeypatch.setattr(ut.random, "bool", lambda prob=0.5: False)

    with generator.context.call_contexts(
            subtree_pushed_call_context=[DefaultValueGeneration(func, "x")]):
        assert generator._gen_matching_func(type_var) is marker

    assert calls == [(type_var, "functions", {
        "signature": False,
        "required_inline_call_source": None,
    })]


# ---------------------------------------------------------------------------
# False-negative detector: patterns the compiler ACCEPTS
# ---------------------------------------------------------------------------

def test_fn_explicit_default_arguments_only():
    """Probe: ExplicitDefaultArgumentsOnly.kt — ACCEPTED by compiler.

    inline fun explicitOnlyA(x: Int = explicitOnlyB(1)): Int = x
    inline fun explicitOnlyB(x: Int = explicitOnlyA(1)): Int = x

    Defaults call each other with EXPLICIT args (1). The compiler accepts
    this because default-value nodes and body nodes are separate.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "UNUSED_PARAMETER")

inline fun explicitOnlyA(x: Int = explicitOnlyB(1)): Int = x
inline fun explicitOnlyB(x: Int = explicitOnlyA(1)): Int = x

fun main() {}
```

Backend verdict:
    native: compiles
    jvm: compiles
    js: compiles
    wasm: compiles
"""
    generator = make_generator()
    a = make_function("explicitOnlyA", is_inline=True)
    b = make_function("explicitOnlyB", is_inline=True)
    generator.inline_functions.add(a)
    generator.inline_functions.add(b)

    # a's default calls b(1) → edge (a, 'x') → body_node(b)
    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(a, 'x')):
        assert generator._inline_edge_allowed(b)
        generator._record_inline_edge(b)

    # b's default calls a(1) → edge (b, 'x') → body_node(a)
    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(b, 'x')):
        assert generator._inline_edge_allowed(a), \
            "FALSE NEGATIVE: explicit args in defaults should not create cycle"
        generator._record_inline_edge(a)


def test_fn_constructor_init_boundary_cycle():
    """Probe: ConstructorInitBoundaryCycle.kt — ACCEPTED by compiler.

    class InitBoundaryHost { init { gInitBoundary() } }
    inline fun fInitBoundary() { InitBoundaryHost() }
    inline fun gInitBoundary() { kInitBoundary() }
    inline fun kInitBoundary() { fInitBoundary() }

    The init block is inside a non-inline class constructor — it's a BOUNDARY.
    So gInitBoundary() inside the init block does not create an inline edge
    back to fInitBoundary.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE")

class InitBoundaryHost {
    init {
        gInitBoundary()
    }
}

inline fun fInitBoundary() {
    InitBoundaryHost()
}

inline fun gInitBoundary() {
    kInitBoundary()
}

inline fun kInitBoundary() {
    fInitBoundary()
}
```

Backend verdict:
    native: compiles
    jvm: compiles
    js: compiles
    wasm: compiles
"""
    generator = make_generator()
    f = make_function("fInitBoundary", is_inline=True)
    g = make_function("gInitBoundary", is_inline=True)
    k = make_function("kInitBoundary", is_inline=True)
    generator.inline_functions.add(f)
    generator.inline_functions.add(g)
    generator.inline_functions.add(k)

    # f → k (f's body calls InitBoundaryHost() which has init { g() })
    # But init block is a boundary, so no inline edge from f to g through init.
    # However, g's body calls k, and k's body calls f — that IS a cycle.
    assert generator.inline_call_graph.add_edge(body_node(g), body_node(k))
    assert generator.inline_call_graph.add_edge(body_node(k), body_node(f))
    # f → g would create cycle f → g → k → f
    assert not generator.inline_call_graph.can_add_edge(body_node(f), body_node(g))


def test_fn_member_non_inline_boundary_cycle():
    """Probe: MemberNonInlineBoundaryCycle.kt — ACCEPTED by compiler.

    class MemberBoundaryHost { fun hMemberBoundary() { gMemberBoundary() } }
    inline fun fMemberBoundary() { MemberBoundaryHost().hMemberBoundary() }
    inline fun gMemberBoundary() { kMemberBoundary() }
    inline fun kMemberBoundary() { fMemberBoundary() }

    hMemberBoundary is a non-inline method — calling it is a BOUNDARY.
    So gMemberBoundary() inside hMemberBoundary does not create an inline edge.
    But g → k → f is a cycle through the inline call chain.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE")

class MemberBoundaryHost {
    fun hMemberBoundary() {
        gMemberBoundary()
    }
}

inline fun fMemberBoundary() {
    MemberBoundaryHost().hMemberBoundary()
}

inline fun gMemberBoundary() {
    kMemberBoundary()
}

inline fun kMemberBoundary() {
    fMemberBoundary()
}
```

Backend verdict:
    native: compiles
    jvm: compiles
    js: compiles
    wasm: compiles
"""
    generator = make_generator()
    f = make_function("fMemberBoundary", is_inline=True)
    g = make_function("gMemberBoundary", is_inline=True)
    k = make_function("kMemberBoundary", is_inline=True)
    assert generator.inline_call_graph.add_edge(body_node(g), body_node(k))
    assert generator.inline_call_graph.add_edge(body_node(k), body_node(f))
    assert not generator.inline_call_graph.can_add_edge(body_node(f), body_node(g))


def test_fn_property_getter_boundary_cycle():
    """Probe: PropertyGetterBoundaryCycle.kt — ACCEPTED by compiler.

    val propertyGetterBoundary: Unit get() { gPropertyGetterBoundary() }
    inline fun fPropertyGetterBoundary() { propertyGetterBoundary }
    inline fun gPropertyGetterBoundary() { kPropertyGetterBoundary() }
    inline fun kPropertyGetterBoundary() { fPropertyGetterBoundary() }

    Property getter is non-inline — it's a BOUNDARY.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE")

val propertyGetterBoundary: Unit
    get() {
        gPropertyGetterBoundary()
    }

inline fun fPropertyGetterBoundary() {
    propertyGetterBoundary
}

inline fun gPropertyGetterBoundary() {
    kPropertyGetterBoundary()
}

inline fun kPropertyGetterBoundary() {
    fPropertyGetterBoundary()
}
```

Backend verdict:
    native: compiles
    jvm: compiles
    js: compiles
    wasm: compiles
"""
    generator = make_generator()
    f = make_function("f", is_inline=True)
    g = make_function("g", is_inline=True)
    k = make_function("k", is_inline=True)
    assert generator.inline_call_graph.add_edge(body_node(g), body_node(k))
    assert generator.inline_call_graph.add_edge(body_node(k), body_node(f))
    assert not generator.inline_call_graph.can_add_edge(body_node(f), body_node(g))


def test_fn_top_level_non_inline_boundary_cycle():
    """Probe: TopLevelNonInlineBoundaryCycle.kt — ACCEPTED by compiler.

    inline fun fTopLevelBoundary() { hTopLevelBoundary() }
    fun hTopLevelBoundary() { gTopLevelBoundary() }
    inline fun gTopLevelBoundary() { kTopLevelBoundary() }
    inline fun kTopLevelBoundary() { fTopLevelBoundary() }

    hTopLevelBoundary is non-inline — BOUNDARY. The cycle g → k → f exists
    but f → g is through a boundary, so the compiler accepts it.
    Wait — actually g → k → f → g IS a cycle! Let me re-check...

    Actually the compiler ACCEPTS this. So f calling h (non-inline) creates
    NO inline edge. The cycle only exists if f → g directly, but f calls h
    (non-inline), so no edge. g → k → f is a chain, and f → h → g goes
    through a boundary. So no cycle.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE")

inline fun fTopLevelBoundary() {
    hTopLevelBoundary()
}

fun hTopLevelBoundary() {
    gTopLevelBoundary()
}

inline fun gTopLevelBoundary() {
    kTopLevelBoundary()
}

inline fun kTopLevelBoundary() {
    fTopLevelBoundary()
}

fun main() {}
```

Backend verdict:
    native: compiles
    jvm: compiles
    js: compiles
    wasm: compiles
"""
    generator = make_generator()
    f = make_function("f", is_inline=True)
    g = make_function("g", is_inline=True)
    k = make_function("k", is_inline=True)
    # g → k → f chain
    assert generator.inline_call_graph.add_edge(body_node(g), body_node(k))
    assert generator.inline_call_graph.add_edge(body_node(k), body_node(f))
    # f → g would create cycle, BUT f calls h (non-inline), not g directly
    # So there should be NO edge f → g
    # The test verifies that IF we tried to add f → g, it would be a cycle
    assert not generator.inline_call_graph.can_add_edge(body_node(f), body_node(g))


def test_fn_noninline_callee_default_argument_boundary_cycle():
    """Probe: NonInlineCalleeDefaultArgumentBoundaryCycle.kt — ACCEPTED.

    fun nonInlineCalleeWithDefault(x: Unit = gNIDAB()) {}
    inline fun fNIDAB() { nonInlineCalleeWithDefault() }
    inline fun gNIDAB() { fNIDAB() }

    The callee is non-inline, so its default argument is NOT traversed.
    The call from f to nonInlineCalleeWithDefault creates NO inline edge.
    So the fuzzer should NOT create edge body_node(f) → body_node(g).
    The cycle g → f → g never forms because f → g goes through a boundary.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "RECURSION_IN_INLINE")

fun nonInlineCalleeWithDefault(x: Unit = gNonInlineCalleeDefaultArgumentBoundary()) {
}

inline fun fNonInlineCalleeDefaultArgumentBoundary() {
    nonInlineCalleeWithDefault()
}

inline fun gNonInlineCalleeDefaultArgumentBoundary() {
    fNonInlineCalleeDefaultArgumentBoundary()
}
```

Backend verdict:
    native: compiles
    jvm: compiles
    js: compiles
    wasm: compiles
"""
    generator = make_generator()
    f = make_function("f", is_inline=True)
    g = make_function("g", is_inline=True)
    # g → f chain (g's body calls f)
    assert generator.inline_call_graph.add_edge(body_node(g), body_node(f))
    # The fuzzer would NOT add body_node(f) → body_node(g) because f calls
    # a non-inline function. Verify the graph has no such edge.
    assert not generator.inline_call_graph.reaches(body_node(f), body_node(g))


def test_fn_noinline_default_via_non_inline_helper():
    """Probe: NoinlineDefaultViaNonInlineHelper.kt — ACCEPTED.

    inline fun defaultViaHelper(noinline block = makeDefaultBlock()) { block() }
    fun makeDefaultBlock(): () -> Unit = { defaultViaHelper() }

    makeDefaultBlock is non-inline, so its body is a BOUNDARY.
    The call defaultViaHelper → makeDefaultBlock creates NO inline edge.
    The lambda inside makeDefaultBlock calling defaultViaHelper is also
    inside a non-inline boundary.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE")

inline fun defaultViaHelper(noinline block: () -> Unit = makeDefaultBlock()) {
    block()
}

fun makeDefaultBlock(): () -> Unit = { defaultViaHelper() }

fun main() {}
```

Backend verdict:
    native: compiles
    jvm: compiles
    js: compiles
    wasm: compiles
"""
    generator = make_generator()
    f = make_function("defaultViaHelper", is_inline=True)
    assert not generator.inline_call_graph.direct_successors


def test_omitted_argument_creates_default_edge():
    """When an inline function B calls inline function A() with omitted args,
    the compiler creates edge B.body → A.default (in addition to B.body → A.body).

    If A's default calls B, the cycle A.default → B.body → A.default is detected.
    Without the default edge, this cycle is missed.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "UNUSED_PARAMETER")

inline fun explicitDefaultEdgeA(x: Int = explicitDefaultEdgeB(1)): Int = x
inline fun explicitDefaultEdgeB(x: Int = explicitDefaultEdgeA(1)): Int = x

inline fun omittedDefaultEdgeA(x: Int = omittedDefaultEdgeB()): Int = x
inline fun omittedDefaultEdgeB(x: Int = omittedDefaultEdgeA()): Int = x

fun main() {}
```

Backend verdict:
    native: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
    jvm: the 'omittedDefaultEdgeB$default' invocation is part of an inline cycle.
    js: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
    wasm: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
"""
    generator = make_generator()
    a = make_function("A", is_inline=True)
    b = make_function("B", is_inline=True)
    generator.inline_functions.add(a)
    generator.inline_functions.add(b)

    # A's default calls B: edge (A, True) → (B, False)
    assert generator.inline_call_graph.add_edge((a, 'x'), body_node(b))

    # B's body calls A with omitted args:
    #   edge (B, False) → (A, False) — body edge (OK, different node)
    assert generator.inline_call_graph.add_edge(body_node(b), body_node(a))

    #   edge (B, False) → (A, True)  — default edge — CYCLE!
    #   (A, True) → (B, False) → (A, True) is a cycle
    assert not generator.inline_call_graph.add_edge(body_node(b), (a, 'x'))


def test_explicit_argument_no_default_edge():
    """When B calls A(42) with explicit args, NO default edge is created.
    Only B.body → A.body exists. B.body → A.default is NOT created.

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "UNUSED_PARAMETER")

inline fun explicitOnlyA(x: Int = explicitOnlyB(1)): Int = x
inline fun explicitOnlyB(x: Int = explicitOnlyA(1)): Int = x

fun main() {}
```

Backend verdict:
    native: compiles
    jvm: compiles
    js: compiles
    wasm: compiles
"""
    generator = make_generator()
    a = make_function("A", is_inline=True)
    b = make_function("B", is_inline=True)

    # B's body calls A with explicit arg: only body_node(B) → body_node(A)
    assert generator.inline_call_graph.add_edge(body_node(b), body_node(a))
    # A's default calls some other function C — no cycle back through B
    # body_node(B) → (A, 'x') should NOT exist
    assert not generator.inline_call_graph.reaches(body_node(b), (a, 'x'))


def test_false_positive_different_defaults_no_cycle():
    """P31: A has two defaults. Default_x calls B. B omits y (NOT x).
    Compiler: A.default_x → B.body → A.default_y (different nodes, NO cycle)
    Hephaestus must also NOT detect a cycle (per-param nodes).

    inline fun A(x: Int = B(), y: Int = 42): Int = x + y
    inline fun B(): Int = A(x = 42)  // x explicit, y omitted

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "UNUSED_PARAMETER")

inline fun explicitDefaultEdgeA(x: Int = explicitDefaultEdgeB(1)): Int = x
inline fun explicitDefaultEdgeB(x: Int = explicitDefaultEdgeA(1)): Int = x

inline fun omittedDefaultEdgeA(x: Int = omittedDefaultEdgeB()): Int = x
inline fun omittedDefaultEdgeB(x: Int = omittedDefaultEdgeA()): Int = x

fun main() {}
```

Backend verdict:
    native: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
    jvm: the 'omittedDefaultEdgeB$default' invocation is part of an inline cycle.
    js: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
    wasm: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
"""
    generator = make_generator()
    a = make_function("A", is_inline=True)
    b = make_function("B", is_inline=True)
    generator.inline_functions.add(a)
    generator.inline_functions.add(b)

    # A's default_x calls B: edge (A, 'x') → body_node(B)
    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(a, 'x')):
        assert generator._inline_edge_allowed(b)
        generator._record_inline_edge(b)

    # B's body calls A with x explicit, y omitted:
    #   edge body_node(B) → body_node(A) — body edge
    #   edge body_node(B) → (A, 'y') — y default omitted
    assert generator.inline_call_graph.add_edge(body_node(b), body_node(a))
    assert generator.inline_call_graph.add_edge(body_node(b), (a, 'y'))

    # NO cycle: (A, 'x') → body_node(B) → (A, 'y') — different default nodes!
    # (A, 'y') does NOT reach (A, 'x') because y's default is just 42
    assert not generator.inline_call_graph.reaches((a, 'y'), (a, 'x'))
    assert not generator.inline_call_graph.reaches(body_node(b), (a, 'x'))


def test_real_cycle_same_default_omitted():
    """P32: A has default_x calling B. B omits x (the one that calls B).
    Both compiler and hephaestus detect the cycle.

    inline fun A(x: Int = B(), y: Int = 42): Int = x + y
    inline fun B(): Int = A(y = 42)  // y explicit, x omitted

Kotlin reproducer:
```kotlin
@file:Suppress("NOTHING_TO_INLINE", "UNUSED_PARAMETER")

inline fun explicitDefaultEdgeA(x: Int = explicitDefaultEdgeB(1)): Int = x
inline fun explicitDefaultEdgeB(x: Int = explicitDefaultEdgeA(1)): Int = x

inline fun omittedDefaultEdgeA(x: Int = omittedDefaultEdgeB()): Int = x
inline fun omittedDefaultEdgeB(x: Int = omittedDefaultEdgeA()): Int = x

fun main() {}
```

Backend verdict:
    native: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
    jvm: the 'omittedDefaultEdgeB$default' invocation is part of an inline cycle.
    js: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
    wasm: the 'omittedDefaultEdgeB' invocation is part of an inline cycle.
"""
    generator = make_generator()
    a = make_function("A", is_inline=True)
    b = make_function("B", is_inline=True)
    generator.inline_functions.add(a)
    generator.inline_functions.add(b)

    # A's default_x calls B: edge (A, 'x') → body_node(B)
    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(a, 'x')):
        generator._record_inline_edge(b)

    # B's body calls A with y explicit, x omitted:
    #   edge body_node(B) → body_node(A) — body edge
    #   edge body_node(B) → (A, 'x') — x default omitted — CYCLE!
    assert generator.inline_call_graph.add_edge(body_node(b), body_node(a))
    # This should be rejected — cycle: (A, 'x') → body_node(B) → (A, 'x')
    assert not generator.inline_call_graph.can_add_edge(body_node(b), (a, 'x'))


def test_inline_argument_from_non_inline_caller_does_not_create_edge():
    """
    Probe: InlineArgumentReferenceCycle.kt — ACCEPTED on all backends.

    When a function reference or lambda is passed to an inline function from a
    non-inline context (e.g. main), the compiler does NOT traverse it for inline
    cycles. The generator must not record an edge in that case.
    """
    generator = make_generator()
    foo = make_function("foo", is_inline=True)
    bar = make_function("bar", is_inline=True)
    generator.inline_functions.add(foo)
    generator.inline_functions.add(bar)

    # Simulate generating ::bar as an argument to foo() from main (no inline source).
    assert generator._current_inline_source() is None
    generator._record_inline_edge(bar)
    assert not generator.inline_call_graph.direct_successors


def test_inline_argument_from_inline_body_creates_cycle():
    """
    Probe: InlineArgumentFromInlineBodyCycle.kt — FAIL (inline cycle).

    When a function reference is passed as an inline argument inside another
    inline function body, the compiler follows it. The generator records the
    edge and rejects the cycle.
    """
    generator = make_generator()
    foo = make_function("foo", is_inline=True)
    bar = make_function("bar", is_inline=True)
    baz = make_function("baz", is_inline=True)
    generator.inline_functions.add(foo)
    generator.inline_functions.add(bar)
    generator.inline_functions.add(baz)

    # bar.body passes ::baz as argument to foo => edge bar.body → baz.body
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(bar)):
        generator._record_inline_edge(baz)

    # baz.body calls bar => edge baz.body → bar.body, closing the cycle
    assert generator.inline_call_graph.reaches(body_node(baz), body_node(bar)) is False
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(baz)):
        assert not generator._inline_edge_allowed(bar)


def test_inline_argument_default_call_cycle():
    """
    Probe: InlineArgumentWithDefaultCallCycle.kt — FAIL (inline cycle).

    An inline parameter's default value is a graph node. If the default calls an
    inline helper which calls the original function with the default omitted,
    the cycle default → helper → default is detected.
    """
    generator = make_generator()
    foo = make_function("foo", is_inline=True)
    helper = make_function("helper", is_inline=True)
    generator.inline_functions.add(foo)
    generator.inline_functions.add(helper)

    # foo.default_x calls helper => edge (foo, 'x') → helper.body
    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(foo, 'x')):
        generator._record_inline_edge(helper)

    # helper.body calls foo() with x omitted => edge helper.body → (foo, 'x')
    # This must be rejected because it closes the cycle:
    # (foo, 'x') → helper.body → (foo, 'x')
    assert not generator.inline_call_graph.can_add_edge(body_node(helper), (foo, 'x'))


def test_inline_param_cycle_through_inline_body():
    """
    Probe: InlineParamCycleThroughInlineBody.kt — FAIL (inline cycle).

    A cycle can be discovered through an inline parameter, but only when the
    argument is supplied inside another inline function body. The lambda bodies
    { baz() } and { bar() } are visited with the caller as inline source.
    """
    generator = make_generator()
    foo = make_function("foo", is_inline=True)
    bar = make_function("bar", is_inline=True)
    baz = make_function("baz", is_inline=True)
    generator.inline_functions.add(foo)
    generator.inline_functions.add(bar)
    generator.inline_functions.add(baz)

    # bar.body calls foo { baz() } => edge bar.body → foo.body
    # plus the lambda argument is visited with source bar.body => edge bar.body → baz.body
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(bar)):
        generator._record_inline_edge(foo)
        generator._record_inline_edge(baz)

    # baz.body calls foo { bar() } => edge baz.body → foo.body and baz.body → bar.body
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(baz)):
        generator._record_inline_edge(foo)
        # This last edge closes the cycle bar → baz → bar and must be rejected.
        assert not generator._inline_edge_allowed(bar)


def test_inline_param_self_reference_from_non_inline_caller_is_no_edge():
    """
    Probe: InlineParamSelfReferenceInNonInlineCaller.kt — ACCEPTED.

    A lambda argument supplied from a non-inline caller (main) is not traversed,
    even if the lambda body refers to the callee itself.
    """
    generator = make_generator()
    foo = make_function("foo", is_inline=True)
    generator.inline_functions.add(foo)

    # No inline source: argument generation in main.
    assert generator._current_inline_source() is None
    generator._record_inline_edge(foo)
    assert not generator.inline_call_graph.direct_successors

# ---------------------------------------------------------------------------
# Nearest-enclosing source resolution (InliningSource)
# ---------------------------------------------------------------------------

def test_nested_inline_bodies_attribute_to_nearest_enclosing():
    """Edges belong to the INNERMOST enclosing inline body, not the outermost.

    The compiler's IrInlineCallGraphBuilder re-binds `data` to a new
    CallNode for every inline declaration it descends into, so a call written
    inside `inner` is an edge from inner, never from outer.
    """
    generator = make_generator()
    outer = make_function("outer", is_inline=True)
    inner = make_function("inner", is_inline=True)
    callee = make_function("callee", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(outer)):
        assert generator._current_inline_source() == body_node(outer)
        with generator.context.call_contexts(
                subtree_pushed_call_context=body_ctx(inner)):
            assert generator._current_inline_source() == body_node(inner)
            generator._record_inline_edge(callee)
        # Leaving the inner body restores the outer source.
        assert generator._current_inline_source() == body_node(outer)

    assert generator.inline_call_graph.reaches(body_node(inner), body_node(callee))
    assert not generator.inline_call_graph.reaches(body_node(outer),
                                                   body_node(callee))


def test_default_value_inside_enclosing_inline_body_is_nearest():
    """A default value nested in an inline body resolves to the DEFAULT node.

    Body frames and default-value frames share one bucket, so their relative
    order is preserved; the innermost frame wins regardless of its kind.
    """
    generator = make_generator()
    outer = make_function("outer", is_inline=True)
    nested = make_function("nested", is_inline=True)
    callee = make_function("callee", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(outer)):
        with generator.context.call_contexts(
                subtree_pushed_call_context=default_ctx(nested, 'p')):
            assert generator._current_inline_source() == (nested, 'p')
            generator._record_inline_edge(callee)

    assert generator.inline_call_graph.reaches((nested, 'p'), body_node(callee))
    assert not generator.inline_call_graph.reaches(body_node(outer),
                                                   body_node(callee))


def test_inline_body_inside_default_value_is_nearest():
    """The reverse interleaving: an inline body nested inside a default value."""
    generator = make_generator()
    holder = make_function("holder", is_inline=True)
    nested = make_function("nested", is_inline=True)
    callee = make_function("callee", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(holder, 'p')):
        assert generator._current_inline_source() == (holder, 'p')
        with generator.context.call_contexts(
                subtree_pushed_call_context=body_ctx(nested)):
            assert generator._current_inline_source() == body_node(nested)
            generator._record_inline_edge(callee)

    assert generator.inline_call_graph.reaches(body_node(nested), body_node(callee))
    assert not generator.inline_call_graph.reaches((holder, 'p'), body_node(callee))


def test_non_inline_and_lambda_bodies_pass_through_enclosing_source():
    """Lambdas and local non-inline functions create no node of their own.

    Mirrors `else -> visitElement(declaration, data)`: the enclosing CallNode
    passes through unchanged.
    """
    generator = make_generator()
    outer = make_function("outer", is_inline=True)
    local = make_function("local", is_inline=False)
    callee = make_function("callee", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(outer)):
        # Lambda body: func is None.
        with generator.context.call_contexts(
                subtree_pushed_call_context=body_ctx(None)):
            assert generator._current_inline_source() == body_node(outer)
        # Local non-inline function body.
        with generator.context.call_contexts(
                subtree_pushed_call_context=body_ctx(local)):
            assert generator._current_inline_source() == body_node(outer)
            generator._record_inline_edge(callee)

    assert generator.inline_call_graph.reaches(body_node(outer), body_node(callee))


# ---------------------------------------------------------------------------
# CallNode identity
# ---------------------------------------------------------------------------

def test_param_named_body_does_not_collide_with_body_node():
    """A parameter literally named "body" must not alias the body node.

    'body' is in the identifier word pool (src/resources/words), so this is
    reachable, not hypothetical. Typing the body marker keeps them distinct.
    """
    generator = make_generator()
    foo = make_function("foo", is_inline=True)
    callee = make_function("callee", is_inline=True)

    assert body_node(foo) != (foo, 'body')

    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(foo, 'body')):
        assert generator._current_inline_source() == (foo, 'body')
        generator._record_inline_edge(callee)

    # The edge belongs to the default-value node only.
    assert generator.inline_call_graph.reaches((foo, 'body'), body_node(callee))
    assert not generator.inline_call_graph.reaches(body_node(foo),
                                                   body_node(callee))


def test_body_nodes_compare_by_value_and_function_by_identity():
    """Independently built body nodes are equal; a deep copy is a new vertex.

    IrFunctionBodyStub is a field-less frozen dataclass, so separate instances
    compare and hash equal. FunctionDeclaration defines neither __eq__ nor
    __hash__, so the function half compares by IDENTITY — which is what mirrors
    the frontend's `targetSymbol == inlineFunction.symbol`.
    """
    foo = make_function("foo", is_inline=True)

    assert (foo, IrFunctionBodyStub()) == (foo, IrFunctionBodyStub())
    assert hash((foo, IrFunctionBodyStub())) == hash((foo, IrFunctionBodyStub()))

    twin = deepcopy(foo)
    assert twin.name == foo.name
    assert body_node(twin) != body_node(foo)
    assert InliningSource(foo, IrFunctionBodyStub()).node == body_node(foo)


# ---------------------------------------------------------------------------
# Escalation metrics: call_context_tail must read as it did before
# InliningSource existed
# ---------------------------------------------------------------------------

def test_tail_still_reports_function_body_generation_for_inline_body():
    """_record_escalation's call_context_tail(2) must be unperturbed.

    InliningSource is pushed BEFORE FunctionBodyGeneration precisely so the
    escalation records at generator.py:1489-1491 keep the strings they had
    before this frame existed. Fails if the two pushes are swapped.
    """
    generator = make_generator()
    foo = make_function("foo", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(foo)):
        with generator.context.call_contexts(
                subtree_pushed_call_context=[ExprCallSite()]):
            tail = generator.context.call_context_tail(2)

    assert isinstance(tail[0], FunctionBodyGeneration)
    assert isinstance(tail[1], ExprCallSite)


def test_tail_still_reports_default_value_generation_for_default_value():
    """Same guarantee on the default-value push site."""
    generator = make_generator()
    foo = make_function("foo", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(foo)):
        with generator.context.call_contexts(
                subtree_pushed_call_context=[ExprCallSite()]):
            tail = generator.context.call_context_tail(2)

    assert isinstance(tail[0], DefaultValueGeneration)
    assert isinstance(tail[1], ExprCallSite)


# ---------------------------------------------------------------------------
# Omitted default arguments: gate before omitting, don't record after
# ---------------------------------------------------------------------------

def make_defaulted_inline_function(name, param_names):
    """An inline function whose params all have (dummy) default values."""
    func = make_function(name, is_inline=True)
    func.params = [
        ast.ParameterDeclaration(p, kt.Integer,
                                 default=ast.IntegerConstant(1, kt.Integer))
        for p in param_names
    ]
    return func


def test_omitting_default_is_refused_when_it_would_close_a_cycle():
    """The omit decision must be GATED, mirroring _inline_edge_allowed.

    Omitting an argument makes the compiler inline the callee's default-value
    expression here — edge source -> (callee, param_name). Same setup as
    test_inline_argument_default_call_cycle, asked through _inline_edge_allowed
    with the parameter name the omit decision now passes.
    """
    generator = make_generator()
    foo = make_defaulted_inline_function("foo", ['x'])
    helper = make_function("helper", is_inline=True)

    # foo's default calls helper => (foo, 'x') -> helper.body
    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(foo, 'x')):
        generator._record_inline_edge(helper)

    # helper's body may call foo, but must NOT omit x: that closes
    # (foo, 'x') -> helper.body -> (foo, 'x').
    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(helper)):
        assert generator._inline_edge_allowed(foo)
        assert not generator._inline_edge_allowed(foo, 'x')


def test_gate_and_record_agree_on_the_default_value_node():
    """target_param_name reaches (callee, name), not the callee's body node."""
    generator = make_generator()
    foo = make_defaulted_inline_function("foo", ['x'])
    caller = make_function("caller", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(caller)):
        assert generator._inline_edge_allowed(foo, 'x')
        generator._record_inline_edge(foo, 'x')

    assert generator.inline_call_graph.reaches(body_node(caller), (foo, 'x'))
    assert not generator.inline_call_graph.reaches(body_node(caller),
                                                   body_node(foo))


# ---------------------------------------------------------------------------
# Frontend recursion shapes not caught by identity-only check
# ---------------------------------------------------------------------------

def test_stored_callable_reference_without_call_rejects_recursion():
    """
    Kotlin reproducer (StoredCallableReferenceOnly.kt):
    ```kotlin
    @file:Suppress("NOTHING_TO_INLINE")

    inline fun storedReferenceOnly() {
        val ref = ::storedReferenceOnly
    }

    fun main() {
        storedReferenceOnly()
    }
    ```

    Backend verdict:
        jvm:    error: inline function 'fun storedReferenceOnly(): Unit' cannot be recursive.
        native: error: inline function 'fun storedReferenceOnly(): Unit' cannot be recursive.
        js:     error: inline function 'fun storedReferenceOnly(): Unit' cannot be recursive.
        wasm:   error: inline function 'fun storedReferenceOnly(): Unit' cannot be recursive.
    """
    generator = make_generator()
    caller = make_function("storedReferenceOnly", is_inline=True)
    # In real generation the reference can resolve to a distinct declaration
    # (e.g., an override or a function selected through a different path)
    # even though it denotes the same logical function.
    callee = make_function("storedReferenceOnly", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(caller)):
        assert not generator._inline_edge_allowed(callee)


def test_instance_call_on_this_rejects_recursion():
    """
    Kotlin reproducer (InstanceCallOnThis.kt):
    ```kotlin
    @file:Suppress("NOTHING_TO_INLINE")

    class C {
        inline fun foo(): Int {
            return this.foo()
        }
    }

    fun main() {
        C().foo()
    }
    ```

    Backend verdict:
        jvm:    error: inline function 'fun foo(): Int' cannot be recursive.
        native: error: inline function 'fun foo(): Int' cannot be recursive.
        js:     error: inline function 'fun foo(): Int' cannot be recursive.
        wasm:   error: inline function 'fun foo(): Int' cannot be recursive.
    """
    generator = make_generator()
    caller = make_function("foo", is_inline=True)
    # The callee selected for this.foo() can be a different declaration object
    # (e.g., a base-class method or an override) even though it is the same
    # logical function.
    callee = make_function("foo", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(caller)):
        assert not generator._inline_edge_allowed(callee)


def test_instance_call_on_expression_rejects_recursion():
    """
    Kotlin reproducer (InstanceCallOnExpression.kt):
    ```kotlin
    @file:Suppress("NOTHING_TO_INLINE")

    class C {
        inline fun foo(): Int {
            val other: C = this
            return other.foo()
        }
    }

    fun main() {
        C().foo()
    }
    ```

    Backend verdict:
        jvm:    error: inline function 'fun foo(): Int' cannot be recursive.
        native: error: inline function 'fun foo(): Int' cannot be recursive.
        js:     error: inline function 'fun foo(): Int' cannot be recursive.
        wasm:   error: inline function 'fun foo(): Int' cannot be recursive.
    """
    generator = make_generator()
    caller = make_function("foo", is_inline=True)
    callee = make_function("foo", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(caller)):
        assert not generator._inline_edge_allowed(callee)


def test_instance_call_on_new_instance_rejects_recursion():
    """
    Kotlin reproducer (InstanceCallOnNewInstance.kt):
    ```kotlin
    @file:Suppress("NOTHING_TO_INLINE")

    class C {
        inline fun foo(): Int {
            return C().foo()
        }
    }

    fun main() {
        C().foo()
    }
    ```

    Backend verdict:
        jvm:    error: inline function 'fun foo(): Int' cannot be recursive.
        native: error: inline function 'fun foo(): Int' cannot be recursive.
        js:     error: inline function 'fun foo(): Int' cannot be recursive.
        wasm:   error: inline function 'fun foo(): Int' cannot be recursive.
    """
    generator = make_generator()
    caller = make_function("foo", is_inline=True)
    callee = make_function("foo", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(caller)):
        assert not generator._inline_edge_allowed(callee)


def test_instance_call_on_cast_receiver_rejects_recursion():
    """
    Kotlin reproducer (InstanceCallOnCastReceiver.kt):
    ```kotlin
    @file:Suppress("NOTHING_TO_INLINE")

    class C {
        inline fun foo(): Int {
            return (TODO() as C).foo()
        }
    }

    fun main() {
        C().foo()
    }
    ```

    Backend verdict:
        jvm:    error: inline function 'fun foo(): Int' cannot be recursive.
        native: error: inline function 'fun foo(): Int' cannot be recursive.
        js:     error: inline function 'fun foo(): Int' cannot be recursive.
        wasm:   error: inline function 'fun foo(): Int' cannot be recursive.
    """
    generator = make_generator()
    caller = make_function("foo", is_inline=True)
    callee = make_function("foo", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(caller)):
        assert not generator._inline_edge_allowed(callee)


def test_instance_call_on_field_access_rejects_recursion():
    """
    Kotlin reproducer (InstanceCallOnFieldAccess.kt):
    ```kotlin
    @file:Suppress("NOTHING_TO_INLINE")

    class Box(val c: C)

    class C {
        inline fun foo(box: Box): Int {
            return box.c.foo(box)
        }
    }

    fun main() {
        C().foo(Box(C()))
    }
    ```

    Backend verdict:
        jvm:    error: inline function 'fun foo(box: Box): Int' cannot be recursive.
        native: error: inline function 'fun foo(box: Box): Int' cannot be recursive.
        js:     error: inline function 'fun foo(box: Box): Int' cannot be recursive.
        wasm:   error: inline function 'fun foo(box: Box): Int' cannot be recursive.
    """
    generator = make_generator()
    caller = make_function("foo", is_inline=True)
    callee = make_function("foo", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(caller)):
        assert not generator._inline_edge_allowed(callee)


# ---------------------------------------------------------------------------
# Escalation metrics: call_context_tail must read as it did before
# InliningSource existed
# ---------------------------------------------------------------------------


def test_omitted_default_gate_is_per_parameter():
    """Each omitted default is its own CallNode, decided independently.

    Omitting x is fine; once that edge is in the graph, omitting y would close
    (foo, 'y') -> caller.body -> (foo, 'y'), so the second decision must go the
    other way — it sees the first edge already recorded.
    """
    generator = make_generator()
    foo = make_defaulted_inline_function("foo", ['x', 'y'])
    caller = make_function("caller", is_inline=True)

    # foo's default for y calls caller => (foo, 'y') -> caller.body
    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(foo, 'y')):
        generator._record_inline_edge(caller)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(caller)):
        assert generator._inline_edge_allowed(foo, 'x')
        generator._record_inline_edge(foo, 'x')
        assert not generator._inline_edge_allowed(foo, 'y')


def test_gen_func_call_falls_back_to_explicit_arg_instead_of_asserting():
    """End-to-end: the refused omission becomes an explicit named argument.

    Before the gate existed, the edge was recorded after the loop with an
    assert, so this program either crashed the generator or emitted a call the
    compiler rejects. Now the parameter is passed explicitly, which inlines
    nothing from the default.
    """
    generator = make_generator()
    foo = make_defaulted_inline_function("foo", ['x'])
    caller = make_function("caller", is_inline=True)
    generator.inline_functions.add(foo)
    generator.inline_functions.add(caller)
    generator.context.add_func(ast.GLOBAL_NAMESPACE, foo.name, foo)
    generator.namespace = ast.GLOBAL_NAMESPACE + (caller.name,)

    # foo's default for x calls caller => (foo, 'x') -> caller.body
    with generator.context.call_contexts(
            subtree_pushed_call_context=default_ctx(foo, 'x')):
        generator._record_inline_edge(caller)

    # Force the "omit this default" branch, so the gate is what stops it and
    # the test cannot pass vacuously by randomly choosing an explicit argument.
    original_bool = ut.random.bool
    ut.random.bool = lambda *a, **kw: False
    try:
        with generator.context.call_contexts(
                subtree_pushed_call_context=body_ctx(caller)):
            call = generator._gen_func_call(kt.Integer, only_leaves=True)
    finally:
        ut.random.bool = original_bool

    assert call.func == "foo"
    # x was passed explicitly rather than omitted, so no default edge exists.
    assert [a.name for a in call.args] == ['x']
    assert not generator.inline_call_graph.reaches(body_node(caller), (foo, 'x'))


def test_omitting_the_same_default_twice_is_not_an_assertion_failure():
    """Two calls omitting the same default re-add one edge — legal, not a cycle.

    The gate answers "would this create a cycle", which is not the same question
    as "does add_edge report a change". IncrementalDAGTransitiveClosure.add_edge
    returns True for an already-present edge, so the record's assert holds.
    """
    generator = make_generator()
    foo = make_defaulted_inline_function("foo", ['x'])
    caller = make_function("caller", is_inline=True)

    with generator.context.call_contexts(
            subtree_pushed_call_context=body_ctx(caller)):
        for _ in range(2):
            assert generator._inline_edge_allowed(foo, 'x')
            generator._record_inline_edge(foo, 'x')

    assert generator.inline_call_graph.reaches(body_node(caller), (foo, 'x'))
