from copy import deepcopy
import os
from types import SimpleNamespace

import pytest

from src import utils
from src.compilers.kotlin import KotlinSettings
from src.generators.config import cfg
from src.modules.crossmodule import CrossModuleManager
from src.modules.evaluation import Evaluation
from src.modules.generation import Generation
from src.modules.models import ProgramRes
from src.modules.oracle_modes import CompilationMode
from src.utils import RandomUtils
from tests.driver.fixtures import minimal_program


class _Random(RandomUtils):
    def __init__(self):
        pass

    def bool(self, probability):
        return probability == 1.0


def _manager(test_directory, drop_probability=0.0):
    config = deepcopy(cfg)
    config.prob.crossmodule_probability = 1.0
    config.prob.max_module_chain_depth = 0
    config.prob.drop_indirect_deps_prob = drop_probability
    return CrossModuleManager(
        str(test_directory), config, 'native', 'test', lambda _: None,
        _Random(), lambda _: None)


def test_dependency_klibs_resolves_ordered_ids_to_each_artifact_layout(tmp_path):
    manager = _manager(tmp_path)
    manager.settings_by_variant = {
        'js': KotlinSettings(backend='js'),
        'native': KotlinSettings(backend='native'),
        'wasm': KotlinSettings(backend='wasm'),
    }
    ordinary = manager.dependency_klibs((17, 3))
    js = manager.dependency_klibs((17, 3), 'js')

    assert ordinary == [
        str((tmp_path / 'klibs' / 'd17.klib').resolve()),
        str((tmp_path / 'klibs' / 'd3.klib').resolve()),
    ]
    assert js == [
        str((tmp_path / 'klibs' / 'd17-js.klib').resolve()),
        str((tmp_path / 'klibs' / 'd3-js.klib').resolve()),
    ]


class _CompilerFactory:
    def __init__(self):
        self.args = None
        self.kwargs = None

    def make_compiler(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return self


def _prepare_compilation(tmp_path, monkeypatch, closure, drop_probability=0.0):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 1.0)
    manager = _manager('relative-run', drop_probability)
    compiler = _CompilerFactory()
    options = SimpleNamespace(error_filter_patterns='', language='kotlin')
    evaluation = Evaluation(
        options, compiler, object(), object(), object(), manager,
        mode=CompilationMode())
    stats = {'programs': {}}
    program = ProgramRes(
        False, stats, direct_dependency_pid=closure[0] if closure else None,
        dep_transitive_closure_pids=tuple(closure))
    batch = tmp_path / 'batch'
    batch.mkdir()

    request, dependencies, friends, module_pid = evaluation._prepare_compilation(
        str(batch), {9: program})
    return manager, compiler, program, request, dependencies, friends, module_pid


def test_compilation_preparation_resolves_closure_and_direct_friend_absolutely(
        tmp_path, monkeypatch):
    manager, compiler, _, _, dependencies, friends, module_pid = \
        _prepare_compilation(tmp_path, monkeypatch, (17, 3, 1))

    assert dependencies == manager.dependency_klibs((17, 3, 1))
    assert friends == manager.dependency_klibs((17,))
    assert all(os.path.isabs(path) for path in dependencies + friends)
    assert module_pid == 9
    assert compiler.kwargs['dependency_klibs'] == dependencies
    assert compiler.kwargs['friend_klibs'] == friends


def test_compilation_preparation_drops_indirect_pid_before_path_resolution(
        tmp_path, monkeypatch):
    manager, compiler, program, _, dependencies, friends, _ = \
        _prepare_compilation(tmp_path, monkeypatch, (17, 1), 1.0)

    assert dependencies == manager.dependency_klibs((17,))
    assert friends == manager.dependency_klibs((17,))
    assert program.stats['dropped_indirect_deps'] == [1]
    assert compiler.kwargs['dependency_klibs'] == dependencies


class _Artifacts:
    def save_program(self, *_args):
        pass


class _ProgramProcessor:
    fail = False

    def __init__(self, _pid, _options, _module_name):
        self.escalations = []

    def get_program(self, pre_existing_context=None):
        if self.fail:
            raise RuntimeError('controlled generation failure')
        return minimal_program(), True

    def can_transform(self):
        return False

    def get_transformations(self):
        return []


class _GenerationManager:
    def prepare_dependency(self, _pid):
        return {'pid': 17, 'context': minimal_program().context,
                'closure': [17, 3]}


@pytest.mark.parametrize('failed', [False, True])
def test_generation_keeps_logical_closure(tmp_path, monkeypatch, failed):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 1.0)
    monkeypatch.setattr(utils.random, 'bool', lambda _probability: True)
    processor = type('TestProgramProcessor', (_ProgramProcessor,), {'fail': failed})
    monkeypatch.setattr('src.modules.generation.ProgramProcessor', processor)
    options = SimpleNamespace(
        language='kotlin', options={'Translator': {}}, keep_all=False,
        test_directory=str(tmp_path), disable_metrics=True,
        only_correctness_preserving_transformations=True, examine=False,
        print_stacktrace=False, debug=False)
    result = Generation(
        options, _Artifacts(), _GenerationManager()
    ).gen_program(9, str(tmp_path / 'batch'))

    assert result.failed is failed
    assert result.direct_dependency_pid == 17
    assert result.dep_transitive_closure_pids == (17, 3)
    if failed:
        assert result.stats['error'] == 'controlled generation failure'