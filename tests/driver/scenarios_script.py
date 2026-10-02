import os
import runpy
from unittest.mock import patch

from src.ir import ast
from src.modules.processor import ProgramProcessor
from src.transformations.type_overwriting import TypeOverwriting
from tests.driver.support import ROOT, load_driver


class RejectedProgram(TypeOverwriting):
    def transform(self) -> None:
        declaration, = self.program.children()
        assert isinstance(declaration, ast.VariableDeclaration)
        declaration.var_type = self.program.bt_factory.get_string_type()
        declaration.inferred_type = declaration.var_type
        self.error_injected = 'String expected but Integer found'
        self.is_transformed = True


def script() -> None:
    runpy.run_path(str(ROOT / 'hephaestus.py'), run_name='__main__')


def reject() -> None:
    os.environ['DRIVER_COMPILER_MODE'] = 'reject'
    script()


def crash() -> None:
    os.environ['DRIVER_COMPILER_MODE'] = 'crash'
    script()


def expected_rejection() -> None:
    driver = load_driver()
    assert driver.cli_args.language == 'java'
    driver.cli_args.only_correctness_preserving_transformations = False
    os.environ['DRIVER_COMPILER_MODE'] = 'reject-incorrect'
    with patch.dict(ProgramProcessor.NCP_TRANSFORMATIONS,
                    {'RejectedProgram': RejectedProgram}, clear=True):
        driver.cli_args.options['RejectedProgram'] = {'timeout': 10}
        script()