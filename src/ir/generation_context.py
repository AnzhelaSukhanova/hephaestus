from abc import ABC
from dataclasses import dataclass
from typing import Optional, Tuple, Union

from src.ir import ast


@dataclass(frozen=True)
class CallContext(ABC):
    pass


@dataclass(frozen=True)
class FunctionCallParamGeneration(CallContext):
    callee: ast.FunctionDeclaration
    target_param: ast.ParameterDeclaration


@dataclass(frozen=True)
class FunctionBodyGeneration(CallContext):
    callee: Optional[ast.FunctionDeclaration] = None

    @property
    def is_inline(self) -> bool:
        return bool(self.callee and self.callee.is_inline)


@dataclass(frozen=True)
class ExprCallSite(CallContext):
    pass


@dataclass(frozen=True)
class PublicApiInlineBody(CallContext):
    callee: ast.FunctionDeclaration


@dataclass(frozen=True)
class DefaultValueGeneration(CallContext):
    callee: ast.FunctionDeclaration
    param_name: str

# Token is mainly needed to simulate IR plugs ("raise ...", body_stub, etc)
@dataclass(frozen=True)
class Token(ABC):
    pass


@dataclass(frozen=True)
class IrFunctionBodyStub(Token):
    pass


@dataclass(frozen=True)
class InliningSource(CallContext):
    """Which inline call node the code being generated belongs to.

    Mirrors `org.jetbrains.kotlin.backend.common.lower.inline.CallNode <https://github.com/jetbrains/kotlin/blob/master/compiler/ir/backend.common/src/org/jetbrains/kotlin/backend/common/lower/inline/InlineCallCycleCheckerLowering.kt#L19>`_.
    CallNode(val function: IrFunction, val callLocation: IrBody)

    InliningSource = (func: ast.FunctionDeclaration, location: str | IrFunctionBodyStub)

    Location is either IrFunctionBodyStub or the name of default param we're generating
    """
    func: ast.FunctionDeclaration
    location: Union[str, IrFunctionBodyStub]

    @property
    def node(self) -> Tuple[ast.FunctionDeclaration,
                            Union[str, IrFunctionBodyStub]]:
        return (self.func, self.location)
