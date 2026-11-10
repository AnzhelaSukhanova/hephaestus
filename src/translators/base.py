from src.ir import types as tp
from src.ir.visitors import ASTVisitor


class BaseTranslator(ASTVisitor[None, str]):

    def __init__(self, package: str | None = None,
                 options: dict[str, str | bool] = {}) -> None:
        self.program: str | None = None
        self.package: str | None = package

    def result(self) -> str:
        if self.program is None:
            raise Exception('You have to translate the program first')
        return self.program

    def get_type_name(self, t: tp.Type) -> str:
        raise NotImplementedError('get_type_name() must be implemented')
