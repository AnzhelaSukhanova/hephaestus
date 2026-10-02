"""Driver-to-manager integration in fresh, argv-configured subprocesses."""
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import pickle
from typing import assert_type
from unittest.mock import patch

from hephaestus import OracleResult, ProgramRes
from src import utils
from src.compilers.kotlin import KotlinCompiler
from src.ir import ast
from src.ir.context import Context
from src.modules.crossmodule import CrossModuleManager, LedgerRecord
from tests.driver import contracts
from tests.driver.crossmodule_fixtures import (
    Commands, CompileStep, FaultProcessor, KlibLayout, RecordingProcessor,
    assert_command, klib_payload, write_klib,
)
from tests.driver.fixtures import ProcessorOptions, minimal_program
from tests.driver.support import load_driver


driver = load_driver()


@dataclass
class Node:
    pid: int
    directory: Path
    result: ProgramRes
    processor: RecordingProcessor
    source_bytes: bytes
    pickle_bytes: bytes

    @property
    def source(self) -> Path:
        return self.directory / 'src' / f'd{self.pid}' / 'program.kt'

    @property
    def incorrect(self) -> Path:
        return self.directory / 'src' / f'd{self.pid}_incorrect' / 'program.kt'


def _setup() -> CrossModuleManager:
    driver.cfg.prob.crossmodule_probability = 1.0
    driver.cfg.prob.max_module_chain_depth = 0
    driver.cfg.prob.drop_indirect_deps_prob = 0.0
    driver.cfg.limits.min_top_level = 0
    driver.cfg.limits.max_top_level = 0
    driver.cfg.limits.max_depth = 0
    driver.cli_args.transformations = 0
    driver.cli_args.replay = None
    utils.random.r.seed(27)
    manager = driver.setup_crossmodule_manager('hermetic compiler 1.0')
    assert type(manager) is CrossModuleManager
    manager.random = deepcopy(utils.random)
    return manager


def _generate(manager: CrossModuleManager, pid: int, relative: bool = False,
              ncp: bool = False) -> Node:
    directory = Path(f'batch{pid}')
    if not relative:
        directory = directory.resolve()
    args = driver.cli_args
    options = ProcessorOptions(
        transformations=0, test_directory=args.test_directory,
        name=args.name, options=args.options)
    processor_type = FaultProcessor if ncp else RecordingProcessor
    processor = processor_type(pid, options, f'src.d{pid}')
    manager.random.r.seed(0)
    with patch.object(driver, 'ProgramProcessor', return_value=processor), \
            patch.object(args, 'only_correctness_preserving_transformations', not ncp):
        result = driver.gen_program(pid, str(directory / 'src'))
    assert_type(result, ProgramRes)
    contracts.program_result(result)
    assert not result.failed, result.stats
    source = directory / 'src' / f'd{pid}' / 'program.kt'
    temporary = Path(args.test_directory) / 'tmp' / str(pid)
    assert source.read_bytes() == (temporary / 'program.kt').read_bytes()
    assert Path(str(source) + '.bin').read_bytes() == (temporary / 'program.kt.bin').read_bytes()
    assert result.stats['programs'] == {
        str(source): True,
        **({str(directory / 'src' / f'd{pid}_incorrect' / 'program.kt'): False} if ncp else {}),
    }
    return Node(pid, directory, result, processor, source.read_bytes(),
                Path(str(source) + '.bin').read_bytes())


def _check(node: Node) -> OracleResult:
    result = driver.check_oracle(str(node.directory), {node.pid: node.result})
    assert_type(result, OracleResult)
    contracts.oracle_result(result)
    assert not node.directory.exists()
    assert not (Path(driver.cli_args.test_directory) / 'tmp' / str(node.pid)).exists()
    return result


def _persist(manager: CrossModuleManager, node: Node, result: OracleResult,
             direct: int | None, closure: list[int], command: list[str]) -> None:
    assert result[0] == {}
    assert set(result[3]) == {node.pid}
    record = result[3][node.pid]
    assert record['direct'] == direct and record['closure'] == closure
    before = manager.load_ledger()
    assert str(node.pid) not in before
    assert node.pid not in manager.provider_candidates(999)
    root = Path(manager.test_directory)
    assert (root / str(node.pid) / 'program.kt').read_bytes() == node.source_bytes
    assert (root / str(node.pid) / 'program.kt.bin').read_bytes() == node.pickle_bytes
    assert Path(manager.published_klib_path(node.pid)).exists()
    metadata = json.loads((root / 'klibs' / f'd{node.pid}.meta.json').read_text())
    assert metadata == {
        'pid': node.pid, 'command': command,
        'compiler_version': 'hermetic compiler 1.0', 'backend': driver.cli_args.backend,
        'flags': [argument for argument in command if argument.startswith('-X')],
    }
    driver.update_stats(result, 1, node.result.stats['time'])
    expected = {**before, str(node.pid): {'direct': direct, 'closure': closure}}
    assert manager.load_ledger() == expected
    assert node.pid in manager.provider_candidates(999)
    stats = json.loads((root / 'stats.json').read_text())
    assert stats['crossmodule'] == expected
    assert driver.STATS['crossmodule'] == expected
    ledger_bytes = Path(manager.ledger_path()).read_bytes()
    driver.save_stats()
    assert Path(manager.ledger_path()).read_bytes() == ledger_bytes
    assert not list(root.rglob('*.tmp'))


def _dependencies(manager: CrossModuleManager, pids: list[int]) -> list[str]:
    return [os.path.abspath(manager.published_klib_path(pid)) for pid in pids]


def _tool_calls() -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for line in Path('tool.jsonl').read_text().splitlines():
        value: object = json.loads(line)
        fields = contracts.record(value)
        arguments = fields['arguments']
        assert contracts.is_sequence(arguments)
        assert all(isinstance(argument, str) for argument in arguments)
        calls.append({str(key): value for key, value in fields.items()})
    return calls


def _external_publication(manager: CrossModuleManager, pid: int,
                          closure: list[int]) -> Node:
    node = _generate(manager, pid)
    assert node.result.direct_dependency_pid == (closure[0] if closure else None)
    assert node.result.dep_transitive_closure_klibs == (_dependencies(manager, closure) or None)
    result = _check(node)
    assert pid in result[3], 'Successful provider must be published'
    assert result[3][pid] == {'direct': closure[0] if closure else None, 'closure': closure}
    calls = _tool_calls()[-2:]
    assert len(calls) == 2
    command: list[str] = []
    for call, source in zip(calls, (node.directory / 'src', node.source), strict=True):
        raw = call['arguments']
        assert contracts.is_sequence(raw)
        arguments = []
        for argument in raw:
            assert isinstance(argument, str)
            arguments.append(argument)
        # The executable expands $HOME before the fake tool logs argv.
        command = KotlinCompiler(str(source), dependency_klibs=_dependencies(manager, closure),
                                 friend_klibs=_dependencies(manager, closure[:1]),
                                 module_name=f'd{pid}').get_compiler_cmd()
        assert_command(command, driver.cli_args.backend, source, f'd{pid}',
                       _dependencies(manager, closure))
        assert arguments == [os.path.expandvars(value) for value in command[1:]]
        assert call['cwd'] == str(node.directory / 'src' / f'd{pid}')
        assert call['java_opts'] == '-Xmx8g'
    assert KotlinCompiler.read_manifest_depends(manager.published_klib_path(pid)) == set()
    _persist(manager, node, result, closure[0] if closure else None, closure, command)
    return node


def chain() -> None:
    manager = _setup()
    nodes = [_external_publication(manager, 1, []),
             _external_publication(manager, 2, [1]),
             _external_publication(manager, 3, [2, 1])]
    assert len(_tool_calls()) == 6
    for node, ancestors in zip(nodes, ([], [1], [1, 2]), strict=True):
        imported = node.processor.imported
        if not ancestors:
            assert imported is None
        else:
            assert imported is not None
            assert imported.target_module == f'src.d{ancestors[-1]}'
            for pid in ancestors:
                assert imported.get_decl(ast.GLOBAL_NAMESPACE, f'src.d{pid}.visible') is not None
                assert imported.get_decl(ast.GLOBAL_NAMESPACE, f'src.d{pid}.hidden') is None
        published = contracts.program(utils.load_program(
            str(Path(manager.test_directory) / str(node.pid) / 'program.kt.bin')))
        assert published.context.target_module == f'src.d{node.pid}'
        assert all(name.startswith(f'src.d{node.pid}.')
                   for name in published.get_current_module_declarations())
        assert published.context.get_decl(ast.GLOBAL_NAMESPACE, f'src.d{node.pid}.hidden') is not None
    assert driver.STATS['totals'] == {'passed': 3, 'failed': 0}
    manager.config.prob.max_module_chain_depth = 2
    assert manager.provider_candidates(999) == [1]
    manager.config.prob.max_module_chain_depth = 3
    assert manager.provider_candidates(999) == [1, 2]
    manager.config.prob.max_module_chain_depth = 0
    assert manager.provider_candidates(3) == [1, 2]


def publication_ir() -> None:
    manager = _setup()
    driver.cli_args.keep_everything = True
    driver.cli_args.dump_ir = True
    node = _generate(manager, 1)
    dumps = node.directory / 'src/ir'
    dumps.mkdir()
    (dumps / '15_BEFORE.FunctionInlining.ir').write_text('before\noriginal\n')
    (dumps / '15_AFTER.FunctionInlining.ir').write_text('after\ninlined\n')
    events: list[int] = []
    live = driver.maybe_run_live_jacoco

    def after_publication(pid: int, phases: dict[str, int | None] | None) -> None:
        assert pid == 1
        assert phases is not None and phases['15_FunctionInlining'] == 1
        saved = Path(manager.test_directory) / str(pid)
        assert json.loads((saved / 'ir_changes.json').read_text()) == phases
        assert (saved / 'program.kt').read_bytes() == node.source_bytes
        assert Path(manager.published_klib_path(pid)).exists()
        assert manager.load_ledger() == {}
        assert not dumps.exists()
        events.append(pid)
        live(pid, phases)

    with patch.object(driver, 'maybe_run_live_jacoco', after_publication):
        result = _check(node)
    assert events == [1]
    assert result[3] == {1: {'direct': None, 'closure': []}}
    command = KotlinCompiler(str(node.source), module_name='d1').get_compiler_cmd()
    _persist(manager, node, result, None, [], command)


def dependent_crash() -> None:
    manager = _setup()
    _external_publication(manager, 1, [])
    node = _generate(manager, 2)
    with patch.dict(os.environ, {'DRIVER_COMPILER_MODE': 'crash'}):
        result = _check(node)
    assert set(result[0]) == {2}
    assert result[0][2]['error'] == 'org.jetbrains.kotlin.CompilerException\ncontrolled crash\n'
    assert result[3] == {}
    assert manager.load_ledger() == {'1': {'direct': None, 'closure': []}}
    assert not Path(manager.published_klib_path(2)).exists()
    assert (Path(manager.test_directory) / '2/program.kt').read_bytes() == node.source_bytes


def _custom(manager: CrossModuleManager, node: Node, steps: list[CompileStep],
            dependencies: list[str]) -> tuple[OracleResult, Commands]:
    commands = Commands(node.pid, node.directory, driver.cli_args.backend,
                        dependencies, steps)
    before = manager.load_ledger()
    with patch.object(driver, 'run_command', commands):
        result = _check(node)
    commands.finished()
    assert manager.load_ledger() == before
    return result, commands


def _ncp(layout: KlibLayout, provider_error: bool = False) -> None:
    manager = _setup()
    _external_publication(manager, 1, [])
    node = _generate(manager, 2, relative=True, ncp=True)
    assert node.result.direct_dependency_pid == 1
    dependencies = _dependencies(manager, [1])
    result, commands = _custom(manager, node, [
        CompileStep(node.directory / 'src', layout, 'depends=stdlib stale\n', False,
                    f'{node.incorrect}:1:1: error: controlled type mismatch'),
        CompileStep(node.source, layout, 'depends=stdlib d1\n', not provider_error),
    ], dependencies)
    assert result[0] == {}
    assert result[1] > 0
    assert result[2][2]['compilation_with_ir_dumps'] == result[1]
    if provider_error:
        assert result[3] == {}
        assert not Path(manager.published_klib_path(2)).exists()
        assert not (Path(manager.test_directory) / '2').exists()
        driver.update_stats(result, 1, node.result.stats['time'])
        assert manager.load_ledger() == {'1': {'direct': None, 'closure': []}}
    else:
        published_klib = Path(manager.published_klib_path(2))
        assert published_klib.is_dir() == (layout == 'directory')
        assert klib_payload(published_klib) == 'isolated'
        assert KotlinCompiler.read_manifest_depends(str(published_klib)) == {'d1'}
        _persist(manager, node, result, 1, [1], commands.calls[-1])
        published = Path(manager.test_directory) / '2'
        assert (published / 'incorrect.kt').exists()
        correct = contracts.program(utils.load_program(str(published / 'program.kt.bin')))
        assert 'src.d2.mismatch' not in correct.get_current_module_declarations()
        dependency = manager.prepare_dependency(3)
        assert dependency is not None and dependency['pid'] == 2
        contracts.dependency(dependency)
        assert dependency['context'].get_decl(ast.GLOBAL_NAMESPACE, 'src.d2.mismatch') is None
        _external_publication(manager, 3, [2, 1])
        assert klib_payload(published_klib) == 'isolated'


def ncp_file() -> None:
    _ncp('file')


def ncp_directory() -> None:
    _ncp('directory')


def provider_error_file() -> None:
    _ncp('file', True)


def provider_error_directory() -> None:
    _ncp('directory', True)


def _drop(rejected: bool) -> None:
    manager = _setup()
    _external_publication(manager, 1, [])
    _external_publication(manager, 2, [1])
    node = _generate(manager, 3, relative=True)
    assert node.result.dep_transitive_closure_klibs == _dependencies(manager, [2, 1])
    before = Path(manager.ledger_path()).read_bytes()
    manager.config.prob.drop_indirect_deps_prob = 1.0
    steps = [CompileStep(node.directory / 'src', status=not rejected,
                         output=f'{node.source}:1:1: error: missing indirect d1' if rejected else '')]
    if not rejected:
        steps.append(CompileStep(node.source, manifest='depends=stdlib d2\n'))
    result, commands = _custom(manager, node, steps, _dependencies(manager, [2]))
    assert node.result.stats['dropped_indirect_deps'] == [1]
    assert node.result.dep_transitive_closure_klibs == _dependencies(manager, [2, 1])
    assert Path(manager.ledger_path()).read_bytes() == before
    assert Path(manager.published_klib_path(1)).exists()
    if rejected:
        assert result[3] == {}
        assert set(result[0]) == {3}
        assert result[0][3]['error'] == '1:1: missing indirect d1'
        driver.update_stats(result, 1, node.result.stats['time'])
        assert Path(manager.ledger_path()).read_bytes() == before
        faults = json.loads((Path(manager.test_directory) / 'faults.json').read_text())
        assert faults['3']['dropped_indirect_deps'] == [1]
        assert (Path(manager.test_directory) / '3/program.kt').read_bytes() == node.source_bytes
        assert not Path(manager.published_klib_path(3)).exists()
    else:
        assert 'manifest_diff' not in result[3][3]
        _persist(manager, node, result, 2, [2, 1], commands.calls[-1])


def dropped_published() -> None:
    _drop(False)


def dropped_rejected() -> None:
    _drop(True)


def _manifest(layout: KlibLayout, dropped: bool = False) -> None:
    manager = _setup()
    _external_publication(manager, 1, [])
    _external_publication(manager, 2, [1])
    node = _generate(manager, 3)
    manager.config.prob.drop_indirect_deps_prob = 1.0 if dropped else 0.0
    expected = [2] if dropped else [2, 1]
    result, commands = _custom(manager, node, [
        CompileStep(node.directory / 'src', layout),
        CompileStep(node.source, layout, 'unique_name=d3\ndepends=stdlib d2 d1 ghost\n'),
    ], _dependencies(manager, expected))
    diff = {'manifest_depends': ['d1', 'd2', 'ghost'],
            'closure': sorted(f'd{pid}' for pid in expected),
            'unexpected': ['d1', 'ghost'] if dropped else ['ghost']}
    assert result[3][3].get('manifest_diff') == diff
    assert klib_payload(Path(manager.published_klib_path(3))) == 'isolated'
    _persist(manager, node, result, 2, [2, 1], commands.calls[-1])
    stats = json.loads((Path(manager.test_directory) / 'stats.json').read_text())
    assert stats['crossmodule_manifest_diff'] == {'3': diff}
    assert driver.STATS['crossmodule_manifest_diff'] == {'3': diff}
    assert 'manifest_diff' not in contracts.record(manager.load_ledger()['3'])


def manifest_file() -> None:
    _manifest('file')


def manifest_directory() -> None:
    _manifest('directory')


def manifest_dropped() -> None:
    _manifest('directory', True)


def _incomplete_publication(kind: str) -> None:
    manager = _setup()
    node = _generate(manager, 1)
    temporary = Path(manager.test_directory) / 'tmp/1'
    if kind == 'source':
        (temporary / 'program.kt').unlink()
    elif kind == 'pickle':
        (temporary / 'program.kt.bin').unlink()
    elif kind == 'corrupt':
        (temporary / 'program.kt.bin').write_bytes(b'not a pickle')
    elif kind == 'none':
        (temporary / 'program.kt.bin').write_bytes(pickle.dumps(None))
    else:
        assert kind == 'klib'
    result, _ = _custom(manager, node, [
        CompileStep(node.directory / 'src'),
        CompileStep(node.source, 'missing' if kind == 'klib' else 'file'),
    ], [])
    assert result[0] == result[3] == {}
    assert manager.provider_candidates(2) == []
    driver.update_stats(result, 1, node.result.stats['time'])
    assert manager.load_ledger() == {}
    assert manager.prepare_dependency(2) is None
    assert Path(manager.published_klib_path(1)).exists() == (kind != 'klib')
    assert (Path(manager.test_directory) / '1').exists() == (kind in ('corrupt', 'none'))


def missing_source() -> None:
    _incomplete_publication('source')


def missing_pickle() -> None:
    _incomplete_publication('pickle')


def corrupt_pickle() -> None:
    _incomplete_publication('corrupt')


def none_pickle() -> None:
    _incomplete_publication('none')


def missing_klib() -> None:
    _incomplete_publication('klib')


def stale_candidates() -> None:
    manager = _setup()
    root = Path(manager.test_directory)
    ledger: dict[str, LedgerRecord] = {}
    for pid in range(1, 8):
        directory = root / str(pid)
        directory.mkdir(parents=True)
        utils.dump_program(str(directory / 'program.kt.bin'), minimal_program())
        write_klib(Path(manager.published_klib_path(pid)), 'file', 'depends=stdlib\n', 'provider')
        ledger[str(pid)] = {'direct': None, 'closure': []}
    (root / '2/program.kt.bin').unlink()
    Path(manager.published_klib_path(3)).unlink()
    (root / '4/program.kt.bin').write_bytes(b'invalid pickle')
    (root / '5/program.kt.bin').write_bytes(pickle.dumps(None))
    utils.dump_program(str(root / '6/program.kt.bin'), ast.Program(Context(), 'kotlin'))
    ledger['7'] = {'direct': 99, 'closure': [99]}
    manager.save_ledger(ledger)
    original = Path(manager.ledger_path()).read_bytes()
    assert manager.provider_candidates(99) == [1, 4, 5, 6, 7]
    # Excluding the only complete candidate forces every stale entry through
    # prepare_dependency, including a missing transitive KLIB.
    assert manager.prepare_dependency(1) is None
    dependency = manager.prepare_dependency(99)
    assert dependency is not None and dependency['pid'] == 1
    contracts.dependency(dependency)
    assert dependency['closure'] == [1]
    assert dependency['klibs'] == _dependencies(manager, [1])
    assert dependency['context'].get_decl(ast.GLOBAL_NAMESPACE, 'answer') is not None
    assert Path(manager.ledger_path()).read_bytes() == original
    Path(manager.published_klib_path(1)).unlink()
    node = _generate(manager, 8)
    assert node.result.direct_dependency_pid is None
    assert node.result.dep_transitive_closure_klibs is None
    assert node.processor.imported is None
    assert Path(manager.ledger_path()).read_bytes() == original


def malformed_ledger() -> None:
    manager = _setup()
    path = Path(manager.ledger_path())
    assert manager.load_ledger() == {}
    path.parent.mkdir(parents=True, exist_ok=True)
    for content in ('{', '[]', 'null', '"not a ledger"'):
        path.write_text(content)
        assert manager.load_ledger() == {}
        assert manager.prepare_dependency(1) is None
        assert path.read_text() == content


def unreadable_manifests() -> None:
    manager = _setup()
    path = Path('unreadable.klib')
    assert KotlinCompiler.read_manifest_depends(str(path)) is None
    path.write_bytes(b'not a zip')
    assert manager.manifest_closure_diff(str(path), [1]) is None
    path.unlink()
    for layout in ('file', 'directory'):
        artifact = Path(layout + '.klib')
        write_klib(artifact, layout, None, 'missing manifest')
        assert KotlinCompiler.read_manifest_depends(str(artifact)) is None
        assert manager.manifest_closure_diff(str(artifact), [1]) is None
        empty = Path(layout + '_empty.klib')
        write_klib(empty, layout, 'unique_name=d1\n', 'no depends')
        assert KotlinCompiler.read_manifest_depends(str(empty)) == set()
        assert manager.manifest_closure_diff(str(empty), [1]) is None


def pid_cache() -> None:
    manager = _setup()
    pid = os.getpid()
    assert driver.CROSSMODULE_MANAGER_PID == pid
    assert driver.setup_crossmodule_manager('ignored for same process') is manager
    assert manager.compiler_version == 'hermetic compiler 1.0'
    with patch.object(driver.os, 'getpid', return_value=pid + 1):
        worker = driver.setup_crossmodule_manager('worker compiler')
        assert type(worker) is CrossModuleManager and worker is not manager
        assert driver.CROSSMODULE_MANAGER_PID == pid + 1
        assert driver.setup_crossmodule_manager('ignored for worker') is worker
        assert worker.compiler_version == 'worker compiler'
        assert worker.config is driver.cfg
        assert worker.backend == driver.cli_args.backend
        assert worker.test_directory == driver.cli_args.test_directory
        assert worker.read_manifest_depends is KotlinCompiler.read_manifest_depends
        assert worker.load_program is utils.load_program
    restored = driver.setup_crossmodule_manager('parent again')
    assert restored is not worker and restored is not manager
    assert driver.CROSSMODULE_MANAGER_PID == pid


def disabled() -> None:
    manager = _setup()
    driver.cfg.prob.crossmodule_probability = 0.0
    node = _generate(manager, 1)
    calls: list[list[str]] = []

    def compile_only(arguments: list[str], get_stdout: bool = True,
                     cwd: str | None = None) -> tuple[bool, str]:
        assert get_stdout and cwd is None
        compiler = KotlinCompiler(str(node.directory / 'src'))
        assert compiler.get_klib_filename() is None
        assert arguments == compiler.get_compiler_cmd()
        assert '-friend-modules' not in arguments and '-Xfriend-modules' not in arguments
        assert '-Xir-produce-klib-file' not in arguments
        calls.append(arguments)
        return True, ''

    with patch.object(driver, 'run_command', compile_only):
        result = _check(node)
    assert len(calls) == 1
    assert result[0] == result[3] == {}
    driver.update_stats(result, 1, node.result.stats['time'])
    assert not Path(manager.ledger_path()).exists()
    assert not Path(manager.published_klibs_dir()).exists()
    assert manager.publish_program(1) is None
    assert 'crossmodule' not in driver.STATS