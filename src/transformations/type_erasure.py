from copy import copy
import itertools
from collections.abc import Mapping
from typing import Iterable, Optional, Tuple

from src.ir import ast
from src.ir.context import NamespacePath
from src.modules.logging import Logger
from src.transformations.base import Transformation, change_namespace
from src.analysis import type_dependency_analysis as tda


class TypeErasure(Transformation):
    CORRECTNESS_PRESERVING = True

    def __init__(self, program: ast.Program, language: str,
                 logger: Optional[Logger] = None,
                 options: Mapping[str, int] = {}):
        super().__init__(program, language, logger, options)
        self._namespace: NamespacePath = ast.GLOBAL_NAMESPACE
        self.max_combinations: int = options.get(
            'max_combinations', 500000
        )
        self.global_type_graph: tda.TypeGraph = {}

    @change_namespace
    def visit_class_decl(self, node: ast.ClassDeclaration) -> ast.ClassDeclaration:
        super().visit_class_decl(node)
        return node

    def visit_var_decl(
            self, node: ast.VariableDeclaration) -> ast.VariableDeclaration:
        if self._namespace != ast.GLOBAL_NAMESPACE:
            super().visit_var_decl(node)
            return node

        # We need this analysis, we have to include type information of global
        # variables.
        t_an = tda.TypeDependencyAnalysis(self.program,
                                          namespace=ast.GLOBAL_NAMESPACE)
        t_an.visit(node)
        self.global_type_graph.update(t_an.result())
        return node

    @change_namespace
    def visit_func_decl(
            self, node: ast.FunctionDeclaration) -> ast.FunctionDeclaration:
        t_an = tda.TypeDependencyAnalysis(self.program,
                                          namespace=self._namespace[:-1],
                                          type_graph=None)
        t_an.visit(node)
        type_graph: tda.TypeGraph = t_an.result()
        type_graph.update(self.global_type_graph)
        omittable_nodes = [n for n in type_graph.keys()
                           if n.is_omittable()]
        omittable_nodes = [
            n
            for n in omittable_nodes
            if tda.is_combination_feasible(type_graph, (n,))
        ]
        # We compute the powerset of omittable nodes.
        combinations: Iterable[Tuple[tda.TypeGraphNode, ...]] = (
            itertools.chain.from_iterable(
                itertools.combinations(omittable_nodes, r)
                for r in range(len(omittable_nodes), 0, -1)
            )
        )
        for i, combination in enumerate(combinations):
            if self.max_combinations and i > self.max_combinations:
                break
            c_type_graph = copy(type_graph)
            # We are trying to find the maximal combination that is feasible.
            if tda.is_combination_feasible(c_type_graph, combination):
                for g_node in combination:
                    self.is_transformed = True
                    if isinstance(g_node, tda.DeclarationNode):
                        g_node.decl.omit_type()
                    if isinstance(g_node,
                                  tda.TypeConstructorInstantiationCallNode):
                        g_node.t.can_infer_type_args = True
                break
        return node

