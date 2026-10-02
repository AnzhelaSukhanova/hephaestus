import io
import json
import sys
import traceback
from collections.abc import Mapping
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from src import utils
from src.generators.config import cfg
from src.ir import ast
from src.ir.context import Context
from src.modules.processor import ProcessorArgs, ProgramProcessor
from src.transformations.type_erasure import TypeErasure
from tests.driver import contracts
from tests.driver.fixtures import minimal_program, ProcessorOptions, replay
from tests.driver.support import load_driver


FILENAMES = {
    'kotlin': ('program.kt', 'incorrect.kt'),
    'groovy': ('Main.groovy', 'incorrect.groovy'),
    'java': ('Main.java', 'Incorrect.java'),
    'scala': ('program.scala', 'incorrect.scala'),
}


def _saved(path: Path, language: str, package: str) -> ast.Program:
    driver = load_driver()
    source = path.read_text(encoding='utf-8')
    assert f'package {package}' in source
    loaded = contracts.program(utils.load_program(str(path) + '.bin'))
    assert loaded.language == language
    translator = driver.TRANSLATORS[language](package, driver.cli_args.options['Translator'])
    assert utils.translate_program(translator, loaded) == source
    return loaded


def _answer(program: ast.Program) -> ast.VariableDeclaration:
    declarations = list(program.children())
    assert len(declarations) == 1
    declaration = declarations[0]
    assert isinstance(declaration, ast.VariableDeclaration)
    assert declaration.name == 'answer'
    assert isinstance(declaration.expr, ast.IntegerConstant)
    return declaration


def _success(result: tuple[bool, object, object, object],
             programs: dict[str, bool], transformations: list[str]
             ) -> Mapping[object, object]:
    contracts.program_result(result)
    failed, stats, dependency, closure = result
    assert failed is False, stats
    assert dependency is None and closure is None
    fields = contracts.record(stats)
    assert fields['programs'] == programs
    assert fields['transformations'] == transformations
    assert 'program' not in fields
    return fields


def _replay() -> Path:
    driver = load_driver()
    source = Path.cwd() / 'input.bin'
    replay(source, driver.cli_args.language)
    driver.cli_args.replay = str(source)
    utils.random.r.seed(41)
    return Path.cwd() / 'sources'


def _processor_options() -> ProcessorOptions:
    args = load_driver().cli_args
    return ProcessorOptions(
        transformation_types=args.transformation_types,
        transformations=args.transformations,
        transformation_schedule=args.transformation_schedule,
        replay=args.replay, log=args.log, debug=args.debug,
        name=args.name, test_directory=args.test_directory,
        language=args.language, options=args.options)


def replay_roundtrip() -> None:
    driver = load_driver()
    args = driver.cli_args
    directory = _replay()
    filename, _ = FILENAMES[args.language]
    destination = directory / 'd7' / filename
    temporary = Path(args.test_directory) / 'tmp' / '7' / filename
    initial = Path(driver.get_generator_dir(7)) / filename
    result = driver.gen_program(7, str(directory))
    fields = _success(result, {str(destination): True}, [])
    assert fields['error'] is None
    for path in (destination, temporary):
        loaded = _saved(path, args.language, 'src.d7')
        declaration = _answer(loaded)
        assert isinstance(declaration.expr, ast.IntegerConstant)
        assert declaration.expr.literal == 1
        assert declaration.var_type == loaded.bt_factory.get_integer_type()
    assert destination.read_bytes() == temporary.read_bytes()
    assert initial.exists() is args.keep_all
    assert Path(str(initial) + '.bin').exists() is args.keep_all
    if args.keep_all:
        _saved(initial, args.language, 'src.d7')
        assert initial.read_bytes() == destination.read_bytes()
    assert not Path(driver.get_transformations_dir(7, 0)).exists()
    metrics = temporary.parent / 'escalations.json'
    assert metrics.exists() is not args.disable_metrics
    if args.disable_metrics:
        assert 'escalations' not in fields
    else:
        assert fields['escalations'] == []
        assert json.loads(metrics.read_text()) == []


class _RecordingProcessor(ProgramProcessor):
    generated: ast.Program | None = None

    def get_program(self, pre_existing_context: Context | None = None
                    ) -> tuple[ast.Program, bool]:
        program, oracle = super().get_program(pre_existing_context)
        self.generated = program
        return program, oracle


def generated_roundtrip() -> None:
    driver = load_driver()
    args = driver.cli_args
    utils.random.r.seed(27)
    directory = Path.cwd() / 'sources'
    filename, _ = FILENAMES[args.language]
    destination = directory / 'd8' / filename
    target_module = 'src.d8' if args.language == 'kotlin' else None
    processor = _RecordingProcessor(8, _processor_options(), target_module)
    with patch.object(cfg.limits, 'min_top_level', 0), \
            patch.object(cfg.limits, 'max_top_level', 0), \
            patch.object(cfg.limits, 'max_depth', 0), \
            patch.object(driver, 'ProgramProcessor', return_value=processor) as constructor:
        result = driver.gen_program(8, str(directory))
    constructor.assert_called_once_with(8, args, target_module)
    fields = _success(result, {str(destination): True}, [])
    assert fields['error'] is None
    loaded = _saved(destination, args.language, 'src.d8')
    assert loaded.context.target_module == target_module
    generated = contracts.program(processor.generated)
    assert loaded is not generated
    declarations = list(loaded.children())
    original_declarations = list(generated.children())
    assert len(declarations) == len(original_declarations)
    assert loaded.get_current_module_declarations().keys() == (
        generated.get_current_module_declarations().keys())
    for declaration, original in zip(declarations, original_declarations, strict=True):
        assert type(declaration) is type(original)
        assert str(declaration) == str(original)
    assert str(loaded) == str(generated)
    driver.cli_args.replay = str(destination) + '.bin'
    replay_directory = Path.cwd() / 'replayed'
    replay_destination = replay_directory / 'd8' / filename
    replay_result = driver.gen_program(8, str(replay_directory))
    replay_fields = _success(replay_result, {str(replay_destination): True}, [])
    assert replay_fields['error'] is None
    _saved(replay_destination, args.language, 'src.d8')
    assert replay_destination.read_bytes() == destination.read_bytes()


class _MetricsProcessor(ProgramProcessor):
    def get_program(self, pre_existing_context: Context | None = None
                    ) -> tuple[ast.Program, bool]:
        assert pre_existing_context is None
        assert self.target_module == 'src.d9'
        program, oracle = super().get_program(pre_existing_context)
        self.escalations = [{'reason': 'controlled escalation',
                             'namespace': ('global', 'answer'),
                             'stack': ['generation']}]
        return program, oracle


def escalations() -> None:
    driver = load_driver()
    directory = _replay()
    destination = directory / 'd9' / 'program.kt'
    with patch.object(driver, 'ProgramProcessor', _MetricsProcessor):
        result = driver.gen_program(9, str(directory))
    fields = _success(result, {str(destination): True}, [])
    assert fields['error'] is None
    metrics = Path(driver.cli_args.test_directory) / 'tmp' / '9' / 'escalations.json'
    if driver.cli_args.disable_metrics:
        assert 'escalations' not in fields
        assert not metrics.exists()
    else:
        expected = [{'reason': 'controlled escalation',
                     'namespace': ('global', 'answer'), 'stack': ['generation']}]
        assert fields['escalations'] == expected
        assert json.loads(metrics.read_text()) == json.loads(json.dumps(expected))


def cp_skipped() -> None:
    driver = load_driver()
    args = driver.cli_args
    processor = ProgramProcessor(10, _processor_options())
    translator = driver.TRANSLATORS[args.language]('src.d10')
    directory = Path.cwd() / 'sources'
    original = minimal_program(args.language)
    destination = directory / 'd10' / FILENAMES[args.language][0]
    result = driver.process_cp_transformations(
        10, str(directory), translator, processor, original, 'd10')
    assert result == str(destination)
    assert processor.current_transformation == 2
    assert processor.get_transformations() == [TypeErasure, TypeErasure]
    assert not processor.can_transform()
    assert not (Path(args.test_directory) / 'transformations').exists()
    loaded = _saved(destination, args.language, 'src.d10')
    assert _answer(loaded).var_type == original.bt_factory.get_integer_type()
    temporary = Path(args.test_directory) / 'tmp' / '10' / destination.name
    _saved(temporary, args.language, 'src.d10')
    assert destination.read_bytes() == temporary.read_bytes()


def _erasure_program() -> ast.Program:
    original = minimal_program()
    integer = original.bt_factory.get_integer_type()
    original.add_declaration(ast.FunctionDeclaration(
        'compute', [], integer, ast.IntegerConstant(2, integer),
        ast.FunctionDeclaration.FUNCTION))
    return original


def cp_type_erasure() -> None:
    driver = load_driver()
    args = driver.cli_args
    original = _erasure_program()
    integer = original.bt_factory.get_integer_type()
    processor = ProgramProcessor(11, _processor_options())
    translator = driver.TRANSLATORS['kotlin']('src.d11')
    directory = Path.cwd() / 'sources'
    result = driver.process_cp_transformations(
        11, str(directory), translator, processor, original, 'd11')
    destination = directory / 'd11' / 'program.kt'
    assert result == str(destination)
    assert processor.current_transformation == 2
    assert processor.get_transformations() == [TypeErasure, TypeErasure]
    loaded = _saved(destination, 'kotlin', 'src.d11')
    function = loaded.get_current_module_declarations()['compute']
    assert isinstance(function, ast.FunctionDeclaration)
    assert function.ret_type is None
    assert function.inferred_type == integer
    assert 'fun compute() =\n  2' in destination.read_text()
    for index in (0, 1):
        path = Path(driver.get_transformations_dir(11, index)) / 'program.kt'
        assert path.exists() is args.keep_all
        if args.keep_all:
            _saved(path, 'kotlin', 'src.d11')
            assert path.read_bytes() == destination.read_bytes()


def gen_cp_changed() -> None:
    driver = load_driver()
    args = driver.cli_args
    directory = _replay()
    original = _erasure_program()
    utils.dump_program(args.replay, original)
    destination = directory / 'd18' / 'program.kt'
    result = driver.gen_program(18, str(directory))
    fields = _success(result, {str(destination): True}, ['TypeErasure', 'TypeErasure'])
    assert fields['error'] is None
    for path in (destination, Path(args.test_directory) / 'tmp' / '18' / 'program.kt'):
        loaded = _saved(path, 'kotlin', 'src.d18')
        declarations = loaded.get_current_module_declarations()
        variable = declarations['answer']
        function = declarations['compute']
        assert isinstance(variable, ast.VariableDeclaration)
        assert isinstance(function, ast.FunctionDeclaration)
        assert variable.var_type is None
        assert function.ret_type is None
    initial = Path(driver.get_generator_dir(18)) / 'program.kt'
    assert initial.exists() is args.keep_all
    if args.keep_all:
        saved = _saved(initial, 'kotlin', 'src.d18')
        assert str(saved) == str(original)
        assert initial.read_bytes() != destination.read_bytes()
    for index in (0, 1):
        transformed = Path(driver.get_transformations_dir(18, index)) / 'program.kt'
        assert transformed.exists() is args.keep_all
        if args.keep_all:
            _saved(transformed, 'kotlin', 'src.d18')
            assert transformed.read_bytes() == destination.read_bytes()


class _ReplacementProcessor(ProgramProcessor):
    def __init__(self, proc_id: int, args: ProcessorArgs,
                 target_module: str | None = None) -> None:
        super().__init__(proc_id, args, target_module)
        self.seen: list[int] = []

    def transform_program(self, program: ast.Program
                          ) -> tuple[ast.Program, bool | None] | None:
        expression = _answer(program).expr
        assert isinstance(expression, ast.IntegerConstant)
        assert isinstance(expression.literal, int)
        self.seen.append(expression.literal)
        self.current_transformation += 1
        if self.current_transformation in (1, 3):
            return None
        replacement = minimal_program(self.args.language)
        declaration = _answer(replacement)
        declaration.expr = ast.IntegerConstant(
            expression.literal + 1, replacement.bt_factory.get_integer_type())
        return replacement, True


def cp_replacements() -> None:
    driver = load_driver()
    args = driver.cli_args
    processor = _ReplacementProcessor(12, _processor_options())
    original = minimal_program()
    directory = Path.cwd() / 'sources'
    result = driver.process_cp_transformations(
        12, str(directory), driver.TRANSLATORS['kotlin']('src.d12'),
        processor, original, 'd12')
    destination = directory / 'd12' / 'program.kt'
    assert result == str(destination)
    assert processor.seen == [1, 1, 2, 2]
    assert not processor.can_transform()
    original_expression = _answer(original).expr
    assert isinstance(original_expression, ast.IntegerConstant)
    assert original_expression.literal == 1
    for path in (destination, Path(args.test_directory) / 'tmp' / '12' / 'program.kt'):
        expression = _answer(_saved(path, 'kotlin', 'src.d12')).expr
        assert isinstance(expression, ast.IntegerConstant)
        assert expression.literal == 3
    for index, expected_value in ((1, 2), (3, 3)):
        path = Path(driver.get_transformations_dir(12, index)) / 'program.kt'
        assert path.exists() is args.keep_all
        if args.keep_all:
            expression = _answer(_saved(path, 'kotlin', 'src.d12')).expr
            assert isinstance(expression, ast.IntegerConstant)
            assert expression.literal == expected_value
    for index in (0, 2):
        assert not Path(driver.get_transformations_dir(12, index)).exists()


class _FaultProcessor(ProgramProcessor):
    def inject_fault(self, program: ast.Program
                     ) -> tuple[ast.Program, str | None] | None:
        declaration = _answer(program)
        declaration.var_type = program.bt_factory.get_string_type()
        self.current_transformation += 1
        return program, 'controlled type mismatch'


def ncp_skipped() -> None:
    driver = load_driver()
    args = driver.cli_args
    processor = ProgramProcessor(13, _processor_options())
    translator = driver.TRANSLATORS[args.language]('src.d13')
    directory = Path.cwd() / 'sources'
    result = driver.process_ncp_transformations(
        13, str(directory), translator, processor, minimal_program(args.language),
        'd13_incorrect')
    assert result is None
    assert translator.package == 'src.d13_incorrect'
    assert processor.current_transformation == 1
    assert not directory.exists()
    assert not Path(args.test_directory).exists()


def ncp_changed() -> None:
    driver = load_driver()
    args = driver.cli_args
    processor = _FaultProcessor(14, _processor_options())
    translator = driver.TRANSLATORS[args.language]('src.d14')
    directory = Path.cwd() / 'sources'
    filename, incorrect_filename = FILENAMES[args.language]
    destination = directory / 'd14_incorrect' / filename
    result = driver.process_ncp_transformations(
        14, str(directory), translator, processor, minimal_program(args.language),
        'd14_incorrect')
    assert result == (str(destination), 'controlled type mismatch')
    assert translator.package == 'src.d14_incorrect'
    temporary = Path(args.test_directory) / 'tmp' / '14' / incorrect_filename
    initial = Path(driver.get_generator_dir(14)) / incorrect_filename
    for path in (destination, temporary):
        loaded = _saved(path, args.language, 'src.d14_incorrect')
        assert _answer(loaded).var_type == loaded.bt_factory.get_string_type()
    assert destination.read_bytes() == temporary.read_bytes()
    assert initial.exists() is args.keep_all
    if args.keep_all:
        _saved(initial, args.language, 'src.d14_incorrect')
        assert initial.read_bytes() == destination.read_bytes()


def _gen_ncp(processor_type: type[ProgramProcessor], changed: bool) -> None:
    driver = load_driver()
    directory = _replay()
    destination = directory / 'd15' / 'program.kt'
    incorrect = directory / 'd15_incorrect' / 'program.kt'
    expected = {str(destination): True}
    if changed:
        expected[str(incorrect)] = False
    with patch.object(driver.cli_args, 'only_correctness_preserving_transformations', False), \
            patch.object(driver, 'ProgramProcessor', processor_type):
        result = driver.gen_program(15, str(directory))
    fields = _success(result, expected, [])
    assert fields['error'] == ('controlled type mismatch' if changed else None)
    correct = _saved(destination, 'kotlin', 'src.d15')
    assert _answer(correct).var_type == correct.bt_factory.get_integer_type()
    assert incorrect.exists() is changed
    temporary = Path(driver.cli_args.test_directory) / 'tmp' / '15' / 'incorrect.kt'
    assert temporary.exists() is changed
    if changed:
        for path in (incorrect, temporary):
            loaded = _saved(path, 'kotlin', 'src.d15_incorrect')
            assert _answer(loaded).var_type == loaded.bt_factory.get_string_type()


def gen_ncp_skipped() -> None:
    _gen_ncp(ProgramProcessor, False)


def gen_ncp_changed() -> None:
    _gen_ncp(_FaultProcessor, True)


class _GenerationFailure(ProgramProcessor):
    failure = RuntimeError('controlled generation failure')

    def get_program(self, pre_existing_context: Context | None = None
                    ) -> tuple[ast.Program, bool]:
        raise self.failure


class _CPFailure(ProgramProcessor):
    failure = RuntimeError('controlled CP failure')

    def transform_program(self, program: ast.Program
                          ) -> tuple[ast.Program, bool | None] | None:
        if self.current_transformation == 1:
            raise self.failure
        return super().transform_program(program)


class _NCPFailure(ProgramProcessor):
    failure = RuntimeError('controlled NCP failure')

    def inject_fault(self, program: ast.Program
                     ) -> tuple[ast.Program, str | None] | None:
        raise self.failure


def _failed(processor_type: type[_GenerationFailure | _CPFailure | _NCPFailure],
            transformations: list[str]) -> None:
    driver = load_driver()
    args = driver.cli_args
    directory = _replay()
    processor = processor_type(16, _processor_options(), 'src.d16')
    output = io.StringIO()
    with redirect_stdout(output), \
            patch.object(driver, 'ProgramProcessor', return_value=processor) as constructor, \
            patch.object(args, 'only_correctness_preserving_transformations', False):
        result = driver.gen_program(16, str(directory))
    constructor.assert_called_once_with(16, args, 'src.d16')
    contracts.program_result(result)
    assert result.failed is True
    assert result.direct_dependency_pid is None
    assert result.dep_transitive_closure_klibs is None
    error = str(processor.failure)
    if args.print_stacktrace:
        error = ''.join(traceback.format_exception(processor.failure))
        assert error.startswith('Traceback (most recent call last):\n')
        assert error.endswith(f'RuntimeError: {processor.failure}\n')
    assert result.stats == {
        'transformations': transformations,
        'error': error, 'program': None, 'time': 0,
    }
    if args.debug:
        assert output.getvalue().endswith(error + '\n')
    else:
        assert output.getvalue() == ''


def generation_error() -> None:
    _failed(_GenerationFailure, [])


def cp_error() -> None:
    _failed(_CPFailure, ['TypeErasure'])


def ncp_error() -> None:
    _failed(_NCPFailure, ['TypeErasure', 'TypeErasure'])


class _Debugger(ModuleType):
    def __init__(self) -> None:
        super().__init__('ipdb')
        self.calls = 0

    def set_trace(self) -> None:
        self.calls += 1


def examine() -> None:
    driver = load_driver()
    directory = _replay()
    debugger = _Debugger()
    output = io.StringIO()
    with patch.dict(sys.modules, {'ipdb': debugger}), redirect_stdout(output):
        result = driver.gen_program(17, str(directory))
    fields = _success(result, {str(directory / 'd17' / 'program.kt'): True}, [])
    assert fields['error'] is None
    assert debugger.calls == 1
    assert output.getvalue() == 'pp program.context._context (to print the context)\n'
    _saved(directory / 'd17' / 'program.kt', 'kotlin', 'src.d17')
