from src.generators.generator import Generator
from src.generators import utils as gu
from src.ir import ast
from src.ir import kotlin_types as kt


def _inline_fun(name):
    return ast.FunctionDeclaration(
        name=name,
        params=[],
        ret_type=kt.Integer,
        body=ast.BottomConstant(kt.Integer),
        func_type=ast.FunctionDeclaration.FUNCTION,
        is_inline=True,
    )


def _regular_fun(name):
    return ast.FunctionDeclaration(
        name=name,
        params=[],
        ret_type=kt.Integer,
        body=ast.BottomConstant(kt.Integer),
        func_type=ast.FunctionDeclaration.FUNCTION,
        is_inline=False,
    )


class _FakeFunctionType:
    def __init__(self, ret_type):
        self.type_args = [ret_type]

    def is_function_type(self):
        return True


class _FakeVar:
    def __init__(self, name, var_type, expr=None):
        self.name = name
        self._type = var_type
        self.expr = expr

    def get_type(self):
        return self._type


class _DummyContext:
    def __init__(self, vars_map, funcs_map):
        self._vars = vars_map
        self._funcs = funcs_map

    def get_vars(self, namespace):
        return self._vars

    def get_funcs(self, namespace):
        return self._funcs


def test_inline_cycle_detection_self_and_indirect():
    generator = Generator(language="kotlin")
    inline_a = _inline_fun("A")
    inline_b = _inline_fun("B")
    inline_c = _inline_fun("C")
    inline_d = _inline_fun("D")

    assert generator._would_create_inline_cycle(inline_a, inline_a)
    assert not generator._would_create_inline_cycle(inline_a, inline_b)

    generator._record_inline_call(inline_a, inline_b)
    generator._record_inline_call(inline_b, inline_c)

    assert generator._would_create_inline_cycle(inline_b, inline_a)
    assert generator._would_create_inline_cycle(inline_c, inline_a)
    assert not generator._would_create_inline_cycle(inline_c, inline_d)


def test_inline_graph_ignores_non_inline_edges():
    generator = Generator(language="kotlin")
    inline_fun = _inline_fun("Inline")
    regular_fun = _regular_fun("Regular")

    generator._record_inline_call(inline_fun, regular_fun)
    generator._record_inline_call(regular_fun, inline_fun)

    assert inline_fun not in generator.inline_call_graph
    assert regular_fun not in generator.inline_call_graph


def test_fallback_matching_func_rejects_inline_cycle(monkeypatch):
    generator = Generator(language="kotlin")
    inline_caller = _inline_fun("Caller")
    inline_callee = _inline_fun("Callee")
    safe_callee = _regular_fun("Safe")

    generator._current_inline_function = inline_caller
    generator._record_inline_call(inline_callee, inline_caller)

    monkeypatch.setattr(
        generator,
        "_get_matching_function_declarations",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        generator,
        "_get_matching_class",
        lambda *args, **kwargs: None,
    )
    def _stub_gen_matching_func(*args, **kwargs):
        callee = safe_callee if kwargs.get("force_not_inline") else inline_callee
        return gu.AttrAccessInfo(None, {}, callee, {})

    monkeypatch.setattr(generator, "_gen_matching_func", _stub_gen_matching_func)
    monkeypatch.setattr(
        generator,
        "gen_func_decl",
        lambda *args, **kwargs: safe_callee,
    )

    call = generator._gen_func_call(kt.Integer)

    assert call.func == safe_callee.name
    assert inline_callee not in generator.inline_call_graph.get(
        inline_caller, set())


def test_func_call_ref_rejects_unresolved_inside_inline():
    generator = Generator(language="kotlin")
    inline_caller = _inline_fun("Caller")
    var = _FakeVar("f", _FakeFunctionType(kt.Integer))
    generator.context = _DummyContext({"f": var}, {})
    generator._current_inline_function = inline_caller

    call = generator._gen_func_call_ref(kt.Integer)

    assert call is None


def test_func_call_ref_rejects_resolved_inline_cycle():
    generator = Generator(language="kotlin")
    inline_caller = _inline_fun("Caller")
    inline_callee = _inline_fun("Callee")
    ref_expr = ast.FunctionReference("callee", None, _FakeFunctionType(kt.Integer))
    var = _FakeVar("f", _FakeFunctionType(kt.Integer), expr=ref_expr)
    generator.context = _DummyContext({"f": var}, {"callee": inline_callee})
    generator._current_inline_function = inline_caller
    generator._record_inline_call(inline_callee, inline_caller)

    call = generator._gen_func_call_ref(kt.Integer)

    assert call is None


def test_func_call_ref_records_resolved_inline_edge():
    generator = Generator(language="kotlin")
    inline_caller = _inline_fun("Caller")
    inline_callee = _inline_fun("Callee")
    ref_expr = ast.FunctionReference("callee", None, _FakeFunctionType(kt.Integer))
    var = _FakeVar("f", _FakeFunctionType(kt.Integer), expr=ref_expr)
    generator.context = _DummyContext({"f": var}, {"callee": inline_callee})
    generator._current_inline_function = inline_caller

    call = generator._gen_func_call_ref(kt.Integer)

    assert call is not None
    assert call.func == "f"
    assert inline_callee in generator.inline_call_graph.get(inline_caller, set())


def test_gen_func_ref_rejects_inline_cycle(monkeypatch):
    generator = Generator(language="kotlin")
    inline_caller = _inline_fun("Caller")
    inline_callee = _inline_fun("Callee")
    signature = _FakeFunctionType(kt.Integer)

    generator._current_inline_function = inline_caller
    generator._record_inline_call(inline_callee, inline_caller)

    monkeypatch.setattr(
        generator,
        "_get_matching_function_declarations",
        lambda *args, **kwargs: [
            gu.AttrReceiverInfo(None, {}, inline_callee, {}),
        ],
    )
    monkeypatch.setattr(generator, "_get_matching_class",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(generator, "_gen_matching_func",
                        lambda *args, **kwargs: None)

    ref = generator._gen_func_ref(signature)

    assert ref is None
    assert inline_callee not in generator.inline_call_graph.get(
        inline_caller, set())


def test_gen_func_ref_records_inline_edge(monkeypatch):
    generator = Generator(language="kotlin")
    inline_caller = _inline_fun("Caller")
    inline_callee = _inline_fun("Callee")
    signature = _FakeFunctionType(kt.Integer)

    generator._current_inline_function = inline_caller

    monkeypatch.setattr(
        generator,
        "_get_matching_function_declarations",
        lambda *args, **kwargs: [
            gu.AttrReceiverInfo(None, {}, inline_callee, {}),
        ],
    )

    ref = generator._gen_func_ref(signature)

    assert ref is not None
    assert ref.func == "Callee"
    assert inline_callee in generator.inline_call_graph.get(
        inline_caller, set())


