import pytest

from src.ir.data_structures import IncrementalDAGTransitiveClosure, StackWithCounter


def test_stack_with_counter_duplicate_items():
    stack = StackWithCounter[int]()
    stack.append(1)
    stack.append(2)
    stack.append(1)

    assert stack == [1, 2, 1]
    assert stack.pop() == 1
    assert stack.contains(1)
    assert stack.pop(0) == 1
    assert not stack.contains(1)
    assert stack.pop() == 2
    assert not stack.contains(2)
    assert stack == []


def test_stack_with_counter_empty_pop():
    stack = StackWithCounter[int]()

    with pytest.raises(IndexError):
        stack.pop()

    stack.append(1)
    assert stack.contains(1)
    assert stack.pop() == 1


@pytest.mark.parametrize('method,args', [
    ('extend', ([2],)),
    ('insert', (0, 2)),
    ('remove', (1,)),
    ('clear', ()),
    ('reverse', ()),
    ('sort', ()),
    ('__setitem__', (0, 2)),
    ('__delitem__', (0,)),
    ('__iadd__', ([2],)),
    ('__imul__', (2,)),
])
def test_stack_with_counter_rejects_unsupported_mutation(method, args):
    stack = StackWithCounter[int]()
    stack.append(1)

    with pytest.raises(TypeError, match='StackWithCounter: unsupported mutation'):
        getattr(stack, method)(*args)

    assert stack == [1]
    assert stack.contains(1)
    assert not stack.contains(2)


def test_incremental_dag_transitive_closure_chain():
    graph = IncrementalDAGTransitiveClosure()

    assert graph.add_edge("a", "b")
    assert graph.add_edge("b", "c")

    assert graph.reaches("a", "b")
    assert graph.reaches("b", "c")
    assert graph.reaches("a", "c")
    assert not graph.reaches("c", "a")


def test_incremental_dag_transitive_closure_bridge_update():
    graph = IncrementalDAGTransitiveClosure()

    assert graph.add_edge("a", "b")
    assert graph.add_edge("c", "d")
    assert graph.add_edge("b", "c")

    assert graph.reaches("a", "c")
    assert graph.reaches("a", "d")
    assert graph.reaches("b", "d")
    assert graph.predecessors["d"] == {"a", "b", "c"}


def test_incremental_dag_transitive_closure_rejects_cycles():
    graph = IncrementalDAGTransitiveClosure()

    assert graph.add_edge("a", "b")
    assert graph.add_edge("b", "c")

    assert not graph.add_edge("c", "a")
    assert not graph.add_edge("a", "a")
    assert not graph.reaches("c", "a")


def test_incremental_dag_transitive_closure_duplicate_edge_is_idempotent():
    graph = IncrementalDAGTransitiveClosure()

    assert graph.add_edge("a", "b")
    assert graph.add_edge("a", "b")

    assert graph.direct_successors["a"] == {"b"}
    assert graph.reaches("a", "b")
