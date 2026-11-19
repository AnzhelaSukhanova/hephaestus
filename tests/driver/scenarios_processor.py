from pathlib import Path
from typing import assert_type
from unittest.mock import patch

import pytest

from src import utils
from src.generators.config import cfg
from src.ir import ast
from src.ir.context import Context
from src.modules.processor import ProcessorArgs, ProgramProcessor
from src.transformations.base import Transformation
from src.transformations.type_overwriting import TypeOverwriting
from src.translators.kotlin import KotlinTranslator
from tests.driver.contracts import program
from tests.driver.fixtures import minimal_program, ProcessorOptions, replay


class Skipped(Transformation):
    def transform(self) -> None:
        self.is_transformed = False


class Changed(Transformation):
    @classmethod
    def preserve_correctness(cls) -> bool:
        return True

    def transform(self) -> None:
        self.is_transformed = True


class Injected(TypeOverwriting):
    def transform(self) -> None:
        self.is_transformed = True
        self.error_injected = 'controlled injection'


def schedules() -> None:
    options = ProcessorOptions(transformations=2, log=True, debug=True)
    boundary: ProcessorArgs = options
    processor = ProgramProcessor(11, boundary)
    assert len(processor.transformation_schedule) == 2
    assert processor.get_transformations() == []
    processor.transformation_schedule = [Skipped, Changed]
    original = minimal_program()
    result = processor.transform_program(original)
    assert_type(result, tuple[ast.Program, bool | None] | None)
    assert result is None
    assert processor.current_transformation == 1
    assert processor.get_transformations() == [Skipped]
    result = processor.transform_program(original)
    assert result == (original, True)
    assert not processor.can_transform()
    assert processor.get_transformations() == [Skipped, Changed]
    processor.ncp_transformations = [Injected]
    fault = processor.inject_fault(original)
    assert_type(fault, tuple[ast.Program, str | None] | None)
    assert fault == (original, 'controlled injection')
    assert processor.current_transformation == 3
    log = (Path(options.test_directory) / 'logs/11').read_text()
    assert 'Transformation name:Skipped' in log
    assert 'Transformation name:Changed' in log
    assert 'Transformation name:Injected' in log
    schedule = Path('schedule.txt')
    schedule.write_text('TypeErasure\nTypeErasure\n')
    options.transformations = None
    options.transformation_schedule = str(schedule)
    assert len(ProgramProcessor(12, options).transformation_schedule) == 2
    schedule.write_text('NotATransformation\n')
    with pytest.raises(SystemExit, match='NotATransformation.*not valid'):
        ProgramProcessor(13, options)


def real_transformations() -> None:
    utils.random.r.seed(41)
    options = ProcessorOptions(transformations=1)
    processor = ProgramProcessor(1, options)
    # An empty program offers no candidate for either real transformation.
    empty = ast.Program(Context(), 'kotlin')
    assert processor.transform_program(empty) is None
    assert processor.inject_fault(empty) is None
    source = Path('replay.bin')
    original = replay(source)
    options.replay = str(source)
    options.debug = True
    loaded, oracle = processor.get_program()
    assert_type(loaded, ast.Program)
    assert_type(oracle, bool)
    assert oracle is True
    assert loaded is not original
    assert len(list(program(loaded).children())) == 1


def generated_context() -> None:
    utils.random.r.seed(27)
    options = ProcessorOptions(log=True, debug=True)
    processor = ProgramProcessor(2, options, 'src.d2')
    provider = ast.Program(Context(), 'kotlin')
    integer = provider.bt_factory.get_integer_type()
    provider.add_declaration(ast.VariableDeclaration(
        'src.d1.answer', ast.IntegerConstant(1, integer), var_type=integer))
    provider.context.target_module = 'src.d1'
    provider.context.prepare_this_context_for_import()
    with patch.object(cfg.limits, 'min_top_level', 0), \
            patch.object(cfg.limits, 'max_top_level', 0), \
            patch.object(cfg.limits, 'max_depth', 0):
        generated, oracle = processor.get_program(provider.context)
    assert oracle is True
    assert program(generated).context is provider.context
    assert generated.context.target_module == 'src.d2'
    assert 'src.d1.answer' in generated.context.get_declarations(ast.GLOBAL_NAMESPACE)
    declarations = list(generated.children())
    assert declarations
    assert all(name.startswith('src.d2.')
               for name in generated.get_current_module_declarations())
    assert 'src.d1.answer' not in generated.get_current_module_declarations()
    translator = KotlinTranslator('src.d2')
    translator.visit(generated)
    source = translator.result()
    assert source.startswith('package src.d2\n')
    assert 'val answer' not in source
    utils.dump_program('generated.bin', generated)
    restored = program(utils.load_program('generated.bin'))
    translator.visit(restored)
    assert translator.result() == source
    assert isinstance(processor.escalations, list)