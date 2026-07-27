from src.ir.data_structures import IncrementalDAGTransitiveClosure


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
