import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import runpy
from unittest.mock import patch

from src import args
from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.ir import ast
from src.modules.compilation import Compilation
from src.modules.oracle_modes import BackendComparisonMode
from src.modules.processor import ProgramProcessor
from src.transformations.type_overwriting import TypeOverwriting
from tests.driver.support import ROOT


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


def _comparison_crash() -> None:
    # The hermetic distribution needs a standard library for the KLIB preflight.
    libraries = {
        'js': Path('hermetic-kotlin-stdlib-js-2.4.255-SNAPSHOT.klib').resolve(),
        'native': Path('hermetic-kotlin-stdlib-native.klib').resolve(),
        'wasm': Path('hermetic-kotlin-stdlib-wasm-js-2.4.255-SNAPSHOT.klib').resolve(),
    }
    for library in libraries.values():
        library.write_text('')
    preflight = BackendComparisonMode.preflight

    def preflight_then_crash(mode: BackendComparisonMode,
                             compilation: Compilation[KotlinSettings]) -> None:
        preflight(mode, compilation)
        # The preflight fixture must build; only the generated program crashes.
        os.environ['DRIVER_COMPILER_MODE'] = 'crash'

    with (patch.object(KotlinCompiler, 'stdlib_path',
                       lambda self: str(libraries[self.settings.backend])),
          patch.object(BackendComparisonMode, 'preflight', preflight_then_crash)):
        script()


def comparison_crash() -> None:
    _comparison_crash()


def comparison_backend_crash() -> None:
    # As with a disabled frontend restriction: JS accepts, Native and Wasm crash.
    os.environ['DRIVER_CRASH_TARGETS'] = 'native,wasm'
    _comparison_crash()


def expected_rejection() -> None:
    parse_args = args.parse_args

    def parse_rejecting_options(argv: Sequence[str] | None = None) -> argparse.Namespace:
        # The script parses its own options, so the scenario adjusts them there.
        options = parse_args(argv)
        assert options.language == 'java'
        options.only_correctness_preserving_transformations = False
        options.options['RejectedProgram'] = {'timeout': 10}
        return options

    os.environ['DRIVER_COMPILER_MODE'] = 'reject-incorrect'
    with (patch.dict(ProgramProcessor.NCP_TRANSFORMATIONS,
                     {'RejectedProgram': RejectedProgram}, clear=True),
          patch.object(args, 'parse_args', parse_rejecting_options)):
        script()