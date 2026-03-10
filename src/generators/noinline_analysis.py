from typing import Tuple, Optional
from src.ir import ast, visitors, context

class NoInlineAnalysis(visitors.DefaultVisitor):
    def __init__(self):
        super().__init__()
        self.program: Optional[ast.Program] = None

    def result(self):
        return None

    def visit_program(self, node: ast.Program):
        self.program = node
        super().visit_program(node)

    def visit_func_decl(self, node: ast.FunctionDeclaration):
        if node.is_inline:
            self._analyze_inline_function(node)
        super().visit_func_decl(node)

    def _analyze_inline_function(self, func: ast.FunctionDeclaration):
        func_params = [p for p in func.params if p.get_type().is_function_type()]
        if not func_params:
            return

        namespace = self.program.context.get_namespace(func)
        if namespace is None:
            namespace = ('global',)

        for param in func_params:
            param.noinline = False
            if self._escapes(param, func.body, namespace):
                param.noinline = True

    def _escapes(self, param: ast.ParameterDeclaration, body: ast.Node, namespace: Tuple[str]) -> bool:
        if body is None:
            return False
        visitor = EscapeVisitor(param, self.program.context, namespace)
        visitor.visit(body)
        return visitor.escapes

class EscapeVisitor(visitors.DefaultVisitor):
    def __init__(self, param: ast.ParameterDeclaration, ctx: context.Context, namespace: Tuple[str]):
        super().__init__()
        self.param = param
        self.context = ctx
        self.namespace = namespace
        self.escapes = False
        self.in_non_inlined_lambda = False

    def result(self):
        return self.escapes

    def visit_variable(self, node: ast.Variable):
        if node.name == self.param.name:
            self.escapes = True
        super().visit_variable(node)

    def visit_func_call(self, node: ast.FunctionCall):
        if node.func == self.param.name:
            if self.in_non_inlined_lambda:
                self.escapes = True

        func_decl = self._resolve_function(node)

        if node.receiver:
            if isinstance(node.receiver, ast.Variable) and node.receiver.name == self.param.name:
                if node.func != "invoke":
                    self.escapes = True
            else:
                self.visit(node.receiver)

        # Check arguments
        for i, arg in enumerate(node.args):
            if isinstance(arg.expr, ast.Variable) and arg.expr.name == self.param.name:
                is_safe = False
                if func_decl and func_decl.is_inline:
                    if i < len(func_decl.params) and not func_decl.params[i].noinline:
                        is_safe = True
                
                if not is_safe:
                    self.escapes = True
            elif isinstance(arg.expr, ast.Lambda):
                is_lambda_inlined = False
                if func_decl and func_decl.is_inline:
                    if i < len(func_decl.params) and not func_decl.params[i].noinline:
                        is_lambda_inlined = True
                
                if is_lambda_inlined:
                    for child in arg.expr.children():
                        self.visit(child)
                else:
                    self.visit(arg.expr)
            else:
                self.visit(arg)


    def visit_call_argument(self, node: ast.CallArgument):
        super().visit_call_argument(node)

    def visit_lambda(self, node: ast.Lambda):
        old_in_lambda = self.in_non_inlined_lambda
        self.in_non_inlined_lambda = True
        super().visit_lambda(node)
        self.in_non_inlined_lambda = old_in_lambda

    def visit_func_decl(self, node: ast.FunctionDeclaration):
        # Nested function
        old = self.in_non_inlined_lambda
        self.in_non_inlined_lambda = True
        super().visit_func_decl(node)
        self.in_non_inlined_lambda = old

    def visit_class_decl(self, node: ast.ClassDeclaration):
        old = self.in_non_inlined_lambda
        self.in_non_inlined_lambda = True
        super().visit_class_decl(node)
        self.in_non_inlined_lambda = old

    def _resolve_function(self, node: ast.FunctionCall) -> Optional[ast.FunctionDeclaration]:
        ns = self.namespace
        while ns:
            funcs = self.context.get_funcs(ns, only_current=True)
            if node.func in funcs:
                decl = funcs[node.func]
                if isinstance(decl, ast.FunctionDeclaration):
                    return decl
                break
            ns = ns[:-1]
        return None
