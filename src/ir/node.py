from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.ir.visitors import ASTVisitor


class Node():

    def accept[VisitorResult, AnalysisResult](
            self, visitor: ASTVisitor[VisitorResult, AnalysisResult]
    ) -> VisitorResult:
        return visitor.visit(self)

    def children(self) -> Collection[Node]:
        raise NotImplementedError('children() must be implemented')

    def update_children(self, children: list[Node]) -> None:
        assert len(children) == len(self.children()), (
            'The number of the given children is not compatible'
            ' with the number of the node\'s children.')

    def is_bottom(self) -> bool:
        return False
