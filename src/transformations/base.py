# pylint: disable=protected-access,dangerous-default-value
import sys
import threading
import time
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Protocol

from src.ir import ast, types as tp
from src.ir.context import NamespacePath
from src.ir.node import Node
from src.ir.visitors import DefaultVisitorUpdate
from src.modules.logging import Logger

# Only for static checkers purposes.
class NamespaceAware(Protocol):
    _namespace: NamespacePath

# Only for static checkers purposes.
class DepthAware(Protocol):
    depth: int


# Callback invoked by threading.Timer.
def timeout_function(log: Callable[[str], None], entity: str,
                     timeouted: list[bool]) -> None:
    log("{}: took too long (timeout)".format(entity))
    sys.stderr.flush() # Python 3 stderr is likely buffered.
    timeouted[0] = True


# Accepts a visitor method ``(TransformationT, NodeT) ->
# NodeT`` and returns a logging/timeout wrapper with the same shape.
def visitor_logging_and_timeout_with_args[
        TransformationT: Transformation, NodeT: Node](
        *args: str
) -> Callable[
    [Callable[[TransformationT, NodeT], NodeT]],
    Callable[[TransformationT, NodeT], NodeT]
]:
    def wrap_visitor_func(
            visitor_func: Callable[[TransformationT, NodeT], NodeT]
    ) -> Callable[[TransformationT, NodeT], NodeT]:
        @wraps(visitor_func)
        def wrapped_visitor(self: TransformationT, node: NodeT) -> NodeT:
            if len(args) > 0:
                self.log(*args)
            transformation_name = self.__class__.__name__
            visitor_name = visitor_func.__name__
            entity = transformation_name + "-" + visitor_name
            self.log("{}: Begin".format(entity))

            timeouted = [False]
            start = time.time()
            try:
                timer = threading.Timer(
                    self.timeout, timeout_function,
                    args=[self.log, entity, timeouted])
                timer.start()
                new_node = visitor_func(self, node)
            finally:
                timer.cancel()
            end = time.time()
            if timeouted[0]:
                new_node = node
            self.log("{}: {} elapsed time".format(entity, str(end - start)))
            self.log("{}: End".format(entity))
            return new_node
        return wrapped_visitor
    return wrap_visitor_func


def change_namespace[
        NamespaceVisitorT: NamespaceAware,
        NamespaceNodeT: (ast.ClassDeclaration, ast.FunctionDeclaration, ast.Lambda),
        VisitorResult](
        visit: Callable[[NamespaceVisitorT, NamespaceNodeT], VisitorResult]
) -> Callable[[NamespaceVisitorT, NamespaceNodeT], VisitorResult]:
    @wraps(visit)
    def inner(self: NamespaceVisitorT,
              node: NamespaceNodeT) -> VisitorResult:
        initial_namespace = self._namespace
        self._namespace += (node.name,)
        new_node = visit(self, node)
        self._namespace = initial_namespace
        return new_node

    return inner


def change_depth[DepthVisitorT: DepthAware, NodeT: Node](
        visit: Callable[[DepthVisitorT, NodeT], NodeT]
) -> Callable[[DepthVisitorT, NodeT], NodeT]:
    @wraps(visit)
    def inner(self: DepthVisitorT, node: NodeT) -> NodeT:
        initial_depth = self.depth
        self.depth += 1
        new_node = visit(self, node)
        self.depth = initial_depth
        return new_node

    return inner


class Transformation(DefaultVisitorUpdate):
    CORRECTNESS_PRESERVING = None

    def __init__(self, program: ast.Program, language: str,
                 logger: Logger | None = None,
                 options: Mapping[str, int] = {}):
        assert program is not None, 'The given program must not be None'
        self.is_transformed: bool = False
        self.language: str = language
        self.program: ast.Program = program
        self.types: list[tp.Type] = self.program.get_types()
        self.logger: Logger | None = logger
        self.options: Mapping[str, int] = options
        self.timeout: int = options.get("timeout", 600)
        if self.logger:
            self.logger.log_info()

    def transform(self) -> None:
        self.program = self.visit(self.program)

    def result(self) -> ast.Program:
        return self.program

    def log(self, msg: str) -> None:
        if self.logger is None:
            pass
        else:
            self.logger.log(msg)

    @classmethod
    def get_name(cls) -> str:
        return cls.__name__

    @classmethod
    def preserve_correctness(cls) -> bool | None:
        return cls.CORRECTNESS_PRESERVING

    @visitor_logging_and_timeout_with_args()
    def visit_program(self, node: ast.Program) -> ast.Program:
        return super().visit_program(node)
