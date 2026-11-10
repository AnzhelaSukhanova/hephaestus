from __future__ import annotations

from collections.abc import Callable, Mapping

from src.ir import ast
from src.ir import types
from src.ir.node import Node


type VisitorNode = (
    ast.SuperClassInstantiation
    | ast.ClassDeclaration
    | types.TypeParameter
    | ast.CallArgument
    | ast.FieldDeclaration
    | ast.VariableDeclaration
    | ast.ParameterDeclaration
    | ast.FunctionDeclaration
    | ast.Lambda
    | ast.FunctionReference
    | ast.EnforceTypeViaCast
    | ast.BottomConstant
    | ast.IntegerConstant
    | ast.RealConstant
    | ast.CharConstant
    | ast.StringConstant
    | ast.ArrayExpr
    | ast.BooleanConstant
    | ast.Variable
    | ast.LogicalExpr
    | ast.EqualityExpr
    | ast.ComparisonExpr
    | ast.ArithExpr
    | ast.Conditional
    | ast.Is
    | ast.New
    | ast.FieldAccess
    | ast.FunctionCall
    | ast.Assignment
    | ast.Program
    | ast.Block
)
type VisitorCallback[VisitorResult] = (
    Callable[[ast.SuperClassInstantiation], VisitorResult]
    | Callable[[ast.ClassDeclaration], VisitorResult]
    | Callable[[types.TypeParameter], VisitorResult]
    | Callable[[ast.CallArgument], VisitorResult]
    | Callable[[ast.FieldDeclaration], VisitorResult]
    | Callable[[ast.VariableDeclaration], VisitorResult]
    | Callable[[ast.ParameterDeclaration], VisitorResult]
    | Callable[[ast.FunctionDeclaration], VisitorResult]
    | Callable[[ast.Lambda], VisitorResult]
    | Callable[[ast.FunctionReference], VisitorResult]
    | Callable[[ast.EnforceTypeViaCast], VisitorResult]
    | Callable[[ast.BottomConstant], VisitorResult]
    | Callable[[ast.IntegerConstant], VisitorResult]
    | Callable[[ast.RealConstant], VisitorResult]
    | Callable[[ast.CharConstant], VisitorResult]
    | Callable[[ast.StringConstant], VisitorResult]
    | Callable[[ast.ArrayExpr], VisitorResult]
    | Callable[[ast.BooleanConstant], VisitorResult]
    | Callable[[ast.Variable], VisitorResult]
    | Callable[[ast.LogicalExpr], VisitorResult]
    | Callable[[ast.EqualityExpr], VisitorResult]
    | Callable[[ast.ComparisonExpr], VisitorResult]
    | Callable[[ast.ArithExpr], VisitorResult]
    | Callable[[ast.Conditional], VisitorResult]
    | Callable[[ast.Is], VisitorResult]
    | Callable[[ast.New], VisitorResult]
    | Callable[[ast.FieldAccess], VisitorResult]
    | Callable[[ast.FunctionCall], VisitorResult]
    | Callable[[ast.Assignment], VisitorResult]
    | Callable[[ast.Program], VisitorResult]
    | Callable[[ast.Block], VisitorResult]
)


class ASTVisitor[VisitorResult, AnalysisResult]:
    """Contract that dispatches visit(Node) to (not implemented) node type-dependent visitors

    For example:
    visit(ast.ClassDeclaration <: Node) ---> self.visit_class_decl
    """

    def result(self) -> AnalysisResult:
        raise NotImplementedError('result() must be implemented')

    def visit(self, node: Node) -> VisitorResult:
        # key: type[T], handler: Callable[[T], R]
        visitors: Mapping[type[VisitorNode], VisitorCallback[VisitorResult]] = {
            ast.SuperClassInstantiation: self.visit_super_instantiation,
            ast.ClassDeclaration: self.visit_class_decl,
            types.TypeParameter: self.visit_type_param,
            ast.CallArgument: self.visit_call_argument,
            ast.FieldDeclaration: self.visit_field_decl,
            ast.VariableDeclaration: self.visit_var_decl,
            ast.ParameterDeclaration: self.visit_param_decl,
            ast.FunctionDeclaration: self.visit_func_decl,
            ast.Lambda: self.visit_lambda,
            ast.FunctionReference: self.visit_func_ref,
            ast.EnforceTypeViaCast: self.visit_enforce_type_via_cast,
            ast.BottomConstant: self.visit_bottom_constant,
            ast.IntegerConstant: self.visit_integer_constant,
            ast.RealConstant: self.visit_real_constant,
            ast.CharConstant: self.visit_char_constant,
            ast.StringConstant: self.visit_string_constant,
            ast.ArrayExpr: self.visit_array_expr,
            ast.BooleanConstant: self.visit_boolean_constant,
            ast.Variable: self.visit_variable,
            ast.LogicalExpr: self.visit_logical_expr,
            ast.EqualityExpr: self.visit_equality_expr,
            ast.ComparisonExpr: self.visit_comparison_expr,
            ast.ArithExpr: self.visit_arith_expr,
            ast.Conditional: self.visit_conditional,
            ast.Is: self.visit_is,
            ast.New: self.visit_new,
            ast.FieldAccess: self.visit_field_access,
            ast.FunctionCall: self.visit_func_call,
            ast.Assignment: self.visit_assign,
            ast.Program: self.visit_program,
            ast.Block: self.visit_block,
        }
        visitor = visitors.get(node.__class__)
        if visitor is None:
            raise Exception(
                "Cannot find visitor for instance node " + str(node.__class__))
        return visitor(node)

    def visit_program(self, node: ast.Program) -> VisitorResult:
        raise NotImplementedError('visit_program() must be implemented')

    def visit_block(self, node: ast.Block) -> VisitorResult:
        raise NotImplementedError('visit_block() must be implemented')

    def visit_super_instantiation(
            self, node: ast.SuperClassInstantiation) -> VisitorResult:
        raise NotImplementedError(
            'visit_super_instantiation() must be implemented')

    def visit_class_decl(self, node: ast.ClassDeclaration) -> VisitorResult:
        raise NotImplementedError('visit_class_decl() must be implemented')

    def visit_type_param(self, node: types.TypeParameter) -> VisitorResult:
        raise NotImplementedError('visit_type_param() must be implemented')

    def visit_var_decl(self, node: ast.VariableDeclaration) -> VisitorResult:
        raise NotImplementedError('visit_var_decl() must be implemented')

    def visit_call_argument(self, node: ast.CallArgument) -> VisitorResult:
        raise NotImplementedError('visit_call_argument() must be implemented')

    def visit_field_decl(self, node: ast.FieldDeclaration) -> VisitorResult:
        raise NotImplementedError('visit_field_decl() must be implemented')

    def visit_param_decl(self, node: ast.ParameterDeclaration) -> VisitorResult:
        raise NotImplementedError('visit_param_decl() must be implemented')

    def visit_func_decl(self, node: ast.FunctionDeclaration) -> VisitorResult:
        raise NotImplementedError('visit_func_decl() must be implemented')

    def visit_lambda(self, node: ast.Lambda) -> VisitorResult:
        raise NotImplementedError('visit_lambda() must be implemented')

    def visit_func_ref(self, node: ast.FunctionReference) -> VisitorResult:
        raise NotImplementedError('visit_func_ref() must be implemented')

    def visit_enforce_type_via_cast(
            self, node: ast.EnforceTypeViaCast) -> VisitorResult:
        raise NotImplementedError(
            'visit_enforce_type_via_cast() must be implemented')

    def visit_bottom_constant(self, node: ast.BottomConstant) -> VisitorResult:
        raise NotImplementedError("visit_bottom_constant() must be implemented")

    def visit_integer_constant(self, node: ast.IntegerConstant) -> VisitorResult:
        raise NotImplementedError(
            'visit_integer_constant() must be implemented')

    def visit_real_constant(self, node: ast.RealConstant) -> VisitorResult:
        raise NotImplementedError('visit_real_constant() must be implemented')

    def visit_char_constant(self, node: ast.CharConstant) -> VisitorResult:
        raise NotImplementedError('visit_char_constant() must be implemented')

    def visit_string_constant(self, node: ast.StringConstant) -> VisitorResult:
        raise NotImplementedError(
            'visit_string_constant() must be implemented')

    def visit_array_expr(self, node: ast.ArrayExpr) -> VisitorResult:
        raise NotImplementedError(
            'visit_array_expr() must be implemented')

    def visit_boolean_constant(self, node: ast.BooleanConstant) -> VisitorResult:
        raise NotImplementedError(
            'visit_boolean_constant() must be implemented')

    def visit_variable(self, node: ast.Variable) -> VisitorResult:
        raise NotImplementedError('visit_variable() must be implemented')

    def visit_logical_expr(self, node: ast.LogicalExpr) -> VisitorResult:
        raise NotImplementedError('visit_logical_expr() must be implemented')

    def visit_equality_expr(self, node: ast.EqualityExpr) -> VisitorResult:
        raise NotImplementedError('visit_equality_expr() must be implemented')

    def visit_comparison_expr(self, node: ast.ComparisonExpr) -> VisitorResult:
        raise NotImplementedError(
            'visit_comparison_expr() must be implemented')

    def visit_arith_expr(self, node: ast.ArithExpr) -> VisitorResult:
        raise NotImplementedError('visit_arith_expr() must be implemented')

    def visit_conditional(self, node: ast.Conditional) -> VisitorResult:
        raise NotImplementedError('visit_conditional() must be implemented')

    def visit_is(self, node: ast.Is) -> VisitorResult:
        raise NotImplementedError('visit_is() must be implemented')

    def visit_new(self, node: ast.New) -> VisitorResult:
        raise NotImplementedError('visit_new() must be implemented')

    def visit_field_access(self, node: ast.FieldAccess) -> VisitorResult:
        raise NotImplementedError('visit_field_access() must be implemented')

    def visit_func_call(self, node: ast.FunctionCall) -> VisitorResult:
        raise NotImplementedError('visit_func_call() must be implemented')

    def visit_assign(self, node: ast.Assignment) -> VisitorResult:
        raise NotImplementedError('visit_assign() must be implemented')

class DefaultVisitor[VisitorResult, AnalysisResult](
        ASTVisitor[VisitorResult, AnalysisResult]):
    """Simple concrete implementation that dispatches visit(Node) to (not implemented)
     node type-dependent visitors.

    For example:
    visit(ast.ClassDeclaration <: Node) ---> self._visit_node() (recursive do nothing)
    """

    def result(self) -> AnalysisResult:
        raise NotImplementedError('result() must be implemented')

    def _visit_node(self, node: Node) -> VisitorResult:
        children = node.children()
        for c in children:
            c.accept(self)
        return None

    def visit_program(self, node: ast.Program) -> VisitorResult:
        return self._visit_node(node)

    def visit_block(self, node: ast.Block) -> VisitorResult:
        return self._visit_node(node)

    def visit_super_instantiation(
            self, node: ast.SuperClassInstantiation) -> VisitorResult:
        return self._visit_node(node)

    def visit_class_decl(self, node: ast.ClassDeclaration) -> VisitorResult:
        return self._visit_node(node)

    def visit_type_param(self, node: types.TypeParameter) -> VisitorResult:
        return self._visit_node(node)

    def visit_var_decl(self, node: ast.VariableDeclaration) -> VisitorResult:
        return self._visit_node(node)

    def visit_call_argument(self, node: ast.CallArgument) -> VisitorResult:
        return self._visit_node(node)

    def visit_field_decl(self, node: ast.FieldDeclaration) -> VisitorResult:
        return self._visit_node(node)

    def visit_param_decl(self, node: ast.ParameterDeclaration) -> VisitorResult:
        return self._visit_node(node)

    def visit_func_decl(self, node: ast.FunctionDeclaration) -> VisitorResult:
        return self._visit_node(node)

    def visit_lambda(self, node: ast.Lambda) -> VisitorResult:
        return self._visit_node(node)

    def visit_func_ref(self, node: ast.FunctionReference) -> VisitorResult:
        return self._visit_node(node)

    def visit_enforce_type_via_cast(
            self, node: ast.EnforceTypeViaCast) -> VisitorResult:
        return self._visit_node(node)

    def visit_bottom_constant(self, node: ast.BottomConstant) -> VisitorResult:
        return self._visit_node(node)

    def visit_integer_constant(self, node: ast.IntegerConstant) -> VisitorResult:
        return self._visit_node(node)

    def visit_real_constant(self, node: ast.RealConstant) -> VisitorResult:
        return self._visit_node(node)

    def visit_char_constant(self, node: ast.CharConstant) -> VisitorResult:
        return self._visit_node(node)

    def visit_string_constant(self, node: ast.StringConstant) -> VisitorResult:
        return self._visit_node(node)

    def visit_array_expr(self, node: ast.ArrayExpr) -> VisitorResult:
        return self._visit_node(node)

    def visit_boolean_constant(self, node: ast.BooleanConstant) -> VisitorResult:
        return self._visit_node(node)

    def visit_variable(self, node: ast.Variable) -> VisitorResult:
        return self._visit_node(node)

    def visit_logical_expr(self, node: ast.LogicalExpr) -> VisitorResult:
        return self._visit_node(node)

    def visit_equality_expr(self, node: ast.EqualityExpr) -> VisitorResult:
        return self._visit_node(node)

    def visit_comparison_expr(self, node: ast.ComparisonExpr) -> VisitorResult:
        return self._visit_node(node)

    def visit_arith_expr(self, node: ast.ArithExpr) -> VisitorResult:
        return self._visit_node(node)

    def visit_conditional(self, node: ast.Conditional) -> VisitorResult:
        return self._visit_node(node)

    def visit_is(self, node: ast.Is) -> VisitorResult:
        return self._visit_node(node)

    def visit_new(self, node: ast.New) -> VisitorResult:
        return self._visit_node(node)

    def visit_field_access(self, node: ast.FieldAccess) -> VisitorResult:
        return self._visit_node(node)

    def visit_func_call(self, node: ast.FunctionCall) -> VisitorResult:
        return self._visit_node(node)

    def visit_assign(self, node: ast.Assignment) -> VisitorResult:
        return self._visit_node(node)


class DefaultVisitorUpdate(DefaultVisitor[Node, Node]):
    """Transform (via accept) and update children Nodes recursively"""
    def result(self) -> Node:
        raise NotImplementedError('result() must be implemented')

    def _visit_node(self, node: Node) -> Node:
        children = node.children()
        new_children: list[Node] = []
        for c in children:
            new_children.append(c.accept(self))
        node.update_children(new_children)
        return node
