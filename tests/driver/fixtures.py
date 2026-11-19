from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from src import utils
from src.ir import ast, BUILTIN_FACTORIES
from src.ir.context import Context


def minimal_program(language: str = 'kotlin') -> ast.Program:
    program = ast.Program(Context(), language)
    integer = BUILTIN_FACTORIES[language].get_integer_type()
    program.add_declaration(ast.VariableDeclaration(
        'answer', ast.IntegerConstant(1, integer), var_type=integer))
    return program


def replay(path: Path, language: str = 'kotlin') -> ast.Program:
    program = minimal_program(language)
    utils.dump_program(str(path), program)
    return program


@dataclass
class ProcessorOptions:
    transformation_types: list[str] = field(default_factory=lambda: ['TypeErasure'])
    transformations: int | None = 0
    transformation_schedule: str = ''
    replay: str | None = None
    log: bool = False
    debug: bool = False
    name: str = 'processor'
    test_directory: str = 'processor'
    language: str = 'kotlin'
    options: Mapping[str, Mapping[str, int]] = field(default_factory=lambda: {
        'Generator': {}, 'TypeErasure': {'timeout': 10},
        'TypeOverwriting': {'timeout': 10}, 'Skipped': {}, 'Changed': {},
        'Injected': {},
    })