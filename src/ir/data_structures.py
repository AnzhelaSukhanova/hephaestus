from collections import Counter, defaultdict


class StackWithCounter(list):
    def __init__(self):
        super().__init__()
        self._counter = Counter()  # O(1) amortized contains(element) checker

    def append(self, object):
        super().append(object)
        self._counter[object] += 1

    def pop(self, index=-1):
        object = super().pop(index)
        self._counter[object] -= 1
        if self._counter[object] == 0:
            del self._counter[object]
        return object

    def contains(self, object):
        return self._counter[object] > 0

    def _unsupported_mutation(self, *args, **kwargs):
        raise TypeError("StackWithCounter: unsupported mutation")

    extend = _unsupported_mutation
    insert = _unsupported_mutation
    remove = _unsupported_mutation
    clear = _unsupported_mutation
    reverse = _unsupported_mutation
    sort = _unsupported_mutation
    __setitem__ = _unsupported_mutation
    __delitem__ = _unsupported_mutation
    __iadd__ = _unsupported_mutation
    __imul__ = _unsupported_mutation

class IncrementalDAGTransitiveClosure:
    def __init__(self):
        self.direct_successors = defaultdict(set)
        self.reachable = defaultdict(set)
        self.predecessors = defaultdict(set)

    def add_vertex(self, vertex):
        self.direct_successors[vertex]
        self.reachable[vertex]
        self.predecessors[vertex]

    def reaches(self, source, target):
        return target in self.reachable[source]

    def would_create_cycle(self, source, target):
        return source == target or self.reaches(target, source)

    def can_add_edge(self, source, target):
        return not self.would_create_cycle(source, target)

    def add_edge(self, source, target):
        self.add_vertex(source)
        self.add_vertex(target)

        if self.would_create_cycle(source, target):
            return False

        if target in self.direct_successors[source]:
            return True

        self.direct_successors[source].add(target)
        self._update_transitive_closure(source, target)
        return True

    def _update_transitive_closure(self, source, target):
        new_predecessors = self.predecessors[source] | {source}
        new_successors = self.reachable[target] | {target}

        for predecessor in new_predecessors:
            self.reachable[predecessor].update(new_successors)

        for successor in new_successors:
            self.predecessors[successor].update(new_predecessors)