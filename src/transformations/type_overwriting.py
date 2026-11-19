from collections.abc import Mapping
from typing import List, NamedTuple, Optional

from src import utils
from src.ir import ast, type_utils as tu, types as tp
from src.ir.builtins import BuiltinFactory
from src.ir.context import NamespacePath
from src.modules.logging import Logger
from src.transformations.base import Transformation, change_namespace
from src.analysis import type_dependency_analysis as tda


class CandidateMethod(NamedTuple):
    namespace: NamespacePath
    candidate_nodes: List[tda.TypeGraphNode]
    type_graph: tda.TypeGraph


class TypeOverwriting(Transformation):
    CORRECTNESS_PRESERVING = False

    def __init__(self, program: ast.Program, language: str,
                 logger: Optional[Logger] = None,
                 options: Mapping[str, int] = {}):
        super().__init__(program, language, logger, options)
        self._namespace: NamespacePath = ast.GLOBAL_NAMESPACE
        self.global_type_graph: tda.TypeGraph = {}
        self.types: List[tp.Type] = program.get_types()
        self.bt_factory: BuiltinFactory = program.bt_factory
        self.error_injected: Optional[str] = None
        self._method_selection: bool = True
        self._candidate_methods: List[CandidateMethod] = []
        self._selected_method: Optional[CandidateMethod] = None

    def visit_program(self, node: ast.Program) -> ast.Program:
        super().visit_program(node)
        self._method_selection = False
        if not self._candidate_methods:
            return node
        self._selected_method = utils.random.choice(self._candidate_methods)
        return super().visit_program(node)

    @change_namespace
    def visit_class_decl(self, node: ast.ClassDeclaration) -> ast.ClassDeclaration:
        return super().visit_class_decl(node)

    def visit_var_decl(
            self, node: ast.VariableDeclaration) -> ast.VariableDeclaration:
        if self._namespace != ast.GLOBAL_NAMESPACE:
            return super().visit_var_decl(node)

        # We need this analysis, we have to include type information of global
        # variables.
        t_an = tda.TypeDependencyAnalysis(self.program,
                                          namespace=ast.GLOBAL_NAMESPACE)
        t_an.visit(node)
        self.global_type_graph.update(t_an.result())
        return node

    def _add_candidate_method(
            self, node: ast.FunctionDeclaration) -> ast.FunctionDeclaration:
        t_an = tda.TypeDependencyAnalysis(self.program,
                                          namespace=self._namespace[:-1],
                                          type_graph=None)
        t_an.visit(node)
        type_graph = t_an.result()
        type_graph.update(self.global_type_graph)
        candidate_nodes = [
            n
            for n in type_graph.keys()
            if n.is_omittable() and not (
                isinstance(n, tda.DeclarationNode) and n.decl.name == tda.RET
            )
        ]
        if not candidate_nodes:
            return node
        self._candidate_methods.append(CandidateMethod(
            self._namespace, candidate_nodes, type_graph))
        return node

    @change_namespace
    def visit_func_decl(
            self, node: ast.FunctionDeclaration) -> ast.FunctionDeclaration:
        if self._method_selection:
            return self._add_candidate_method(node)
        # Type checker can't infer that _selected_method : CandidateMethod
        # (not nullable), since 2 pass structure is too hard for it
        # so we have this assert
        assert self._selected_method is not None
        namespace, candidate_nodes, type_graph = self._selected_method
        if namespace != self._namespace:
            return node

        # Generate a type that is irrelevant
        n = utils.random.choice(candidate_nodes)
        type_param: Optional[tda.TypeVarNode] = None
        if isinstance(n, tda.TypeConstructorInstantiationCallNode):
            type_params: List[tda.TypeVarNode] = [
                n.target for n in type_graph[n]
                if any(e.is_inferred() for e in type_graph[n.target])
            ]
            if not type_params:
                return node
            type_param = utils.random.choice(type_params)
            node_type: tp.Type = n.t.get_type_variable_assignments()[type_param.t]
        else:
            node_type = n.decl.get_type()
        old_type = node_type
        if node_type.name in ["Boolean", "String", "BigInteger"]:
            return node
        ir_type = tu.find_irrelevant_type(node_type, self.types,
                                          self.bt_factory)
        if ir_type is None:
            return node

        # Perform the mutation
        if isinstance(n, tda.DeclarationNode):
            if isinstance(n.decl, ast.VariableDeclaration):
                n.decl.var_type = ir_type
            else:
                n.decl.ret_type = ir_type
            n.decl.inferred_type = ir_type
        if isinstance(n, tda.TypeConstructorInstantiationCallNode):
            # Type checker can't infer that type_param : TypeVarNode (not
            # nullable), because its selection occurs in the earlier matching
            # TypeConstructorInstantiationCallNode branch.
            assert type_param is not None
            type_parameters = n.t.t_constructor.type_parameters
            indexes = {
                t_param: i
                for i, t_param in enumerate(type_parameters)
            }
            n.t.type_args[indexes[type_param.t]] = ir_type
        self.is_transformed = True
        self.error_injected = "{} expected but {} found in node {}".format(
            str(old_type), str(ir_type), n.node_id)
        return node
