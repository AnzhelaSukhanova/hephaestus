import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from src.compilers.kotlin import KotlinCompiler
from src.generators.config import cfg
from tests.test_backend_comparison import application, configure_variants, fixture_tool
from tests.driver.fixtures import replay
from src.modules.scheduler import check_oracle_mul, initialize_scheduler_worker
from src.modules.models import ComparisonDependencies, ProgramRes, VariantDependencies
from src.modules.worker_reporting import _json_default
from tests.test_crossmodule_backend_comparison import (
    CROSSMODULE_STATS_FIELDS, case, commit, configured_application,
)


def rejecting_tool(app, target='js'):
    tool = Path(app.compilation.settings_by_variant['js'].executable)
    tool.write_text(tool.read_text().replace('a = sys.argv[1:]\n',
        'a = sys.argv[1:]\n'
        "backend = ('native' if '-produce' in a else 'jvm' if '-d' in a else 'wasm' if any(\n"
        "    'kotlin-stdlib-wasm-' in item for item in a) else 'js')\n"
        f"if backend == {target!r}:\n"
        "    print(a[0] + ':1:1: error: controlled rejection'); sys.exit(1)\n"))


def dependency_checking_tool(app):
    tool = Path(app.compilation.settings_by_variant['js'].executable)
    tool.write_text(tool.read_text().replace('a = sys.argv[1:]\n',
        'a = sys.argv[1:]\n'
        'import os\n'
        'for index, arg in enumerate(a):\n'
        "    if index and a[index - 1] == '-d': continue  # The JVM output.\n"
        "    for item in arg.split('=')[-1].split(os.pathsep):\n"
        "        if (item.endswith(('.klib', '.jar')) and 'stdlib' not in item\n"
        '                and not os.path.exists(item)):\n'
        "            print('missing library ' + item); sys.exit(3)\n", 1))


def referenced_klibs(argv):
    """Dependency artifacts on a command line, excluding a JVM `-d` output."""
    return [item for index, argument in enumerate(argv)
            if not (index and argv[index - 1] == '-d')
            for item in argument.split('=')[-1].split(os.pathsep)
            if item.endswith(('.klib', '.jar')) and 'stdlib' not in item]


def test_reporting_json_default_serializes_comparison_dependencies():
    dependencies = ComparisonDependencies(
        direct=7,
        closure=(7, 3, 1),
        kept=(7, 1),
        dropped=(3,),
        variants={
            'js': VariantDependencies(
                dependencies=('/run/d7-js.klib', '/run/d3-js.klib'),
                friends=('/run/d7-js.klib',)),
            'native': VariantDependencies(
                dependencies=('/run/d7-native.klib',),
                friends=('/run/d7-native.klib',)),
        },
    )

    serialized = json.loads(json.dumps(
        {'dependencies': dependencies}, default=_json_default))

    assert serialized == {'dependencies': {
        'direct': 7,
        'closure': [7, 3, 1],
        'kept': [7, 1],
        'dropped': [3],
        'variants': {
            'js': {
                'dependencies': ['/run/d7-js.klib', '/run/d3-js.klib'],
                'friends': ['/run/d7-js.klib'],
            },
            'native': {
                'dependencies': ['/run/d7-native.klib'],
                'friends': ['/run/d7-native.klib'],
            },
        },
    }}
    with pytest.raises(TypeError, match='Object of type object is not JSON serializable'):
        json.dumps(object(), default=_json_default)


@pytest.mark.parametrize('keep_everything,disable_metrics', [(False, False), (True, True)])
def test_fault_evidence_includes_all_attempts_even_without_metrics(
        tmp_path, monkeypatch, keep_everything, disable_metrics):
    app = configured_application(tmp_path, monkeypatch)
    app.options.keep_everything = keep_everything
    app.options.disable_metrics = disable_metrics
    rejecting_tool(app)
    directory, program = case(app, tmp_path, 1)
    result = app.evaluation.check_oracle(directory, {1: program})
    app.reporting.update_stats(result, 1, 0)
    retained = Path(app.options.test_directory) / '1'
    manifest = json.loads((retained / 'comparison.json').read_text())
    assert manifest['source'] == 'program.kt'
    assert manifest['comparison']['findings']
    for backend in app.compilation.settings_by_variant:
        evidence = json.loads((retained / 'variants' / backend / 'result.json').read_text())
        assert evidence['settings']['backend'] == backend
        assert evidence['compiler_version'] == 'full compiler version'
        assert evidence['elapsed'] >= 0
        assert (retained / 'variants' / backend / 'output.txt').exists()
        assert evidence['reproduce_argv'][1] == 'program.kt'
        assert not any(directory in arg for arg in evidence['reproduce_argv'])
        if backend != 'js':
            artifact = 'd1.jar' if backend == 'jvm' else 'd1.klib'
            assert (retained / 'variants' / backend / artifact).exists()
    assert json.loads((Path(app.options.test_directory) / 'faults.json').read_text())['1']['error']
    fault_dependencies = json.loads(
        (Path(app.options.test_directory) / 'faults.json').read_text()
    )['1']['comparison_dependencies']
    assert fault_dependencies == {
        'direct': None, 'closure': [], 'kept': [], 'dropped': [],
        'variants': {backend: {'dependencies': [], 'friends': []}
                     for backend in app.compilation.settings_by_variant},
    }
    assert app.reporting.stats['totals'] == {'passed': 0, 'failed': 1, 'inconclusive': 0}
    assert '## 1' in (Path(app.options.test_directory) / 'divergences.md').read_text()


def test_inconclusive_is_not_counted_as_pass_and_references_published_dependencies(
        tmp_path, monkeypatch):
    app = configured_application(tmp_path, monkeypatch)
    root, program = case(app, tmp_path, 1)
    commit(app, app.evaluation.check_oracle(root, {1: program}))
    directory, consumer = case(app, tmp_path, 2, 1, [1])
    Path(app.manager.published_klib_path(1, 'native')).unlink()
    result = app.evaluation.check_oracle(directory, {2: consumer})
    app.reporting.update_stats(result, 1, 0)
    retained = Path(app.options.test_directory) / '2'
    manifest = json.loads((retained / 'comparison.json').read_text())
    variants = {}
    for backend in app.compilation.settings_by_variant:
        paths = app.manager.dependency_klibs([1], backend)
        variants[backend] = {'dependencies': paths, 'friends': paths}
    assert manifest['dependencies'] == {
        'direct': 1,
        'closure': [1],
        'kept': [1],
        'dropped': [],
        'variants': variants,
    }
    assert manifest['available_dependencies'] == {
        '1': {'js': True, 'native': False, 'wasm': True, 'jvm': True}}
    assert not (retained / 'dependencies').exists()
    assert Path(app.manager.published_klib_path(1, 'js')).exists()
    assert (Path(app.options.test_directory) / '1' / 'program.kt.bin').exists()
    native = json.loads((retained / 'variants' / 'native' / 'result.json').read_text())
    assert native['status'] is None
    assert referenced_klibs(native['reproduce_argv']) == [
        os.path.join('..', 'klibs', 'd1-native.klib')] * 2
    assert json.loads((Path(app.options.test_directory) / 'inconclusive.json').read_text())['2']
    assert app.reporting.stats['totals'] == {'passed': 0, 'failed': 0, 'inconclusive': 1}


def test_generation_failure_keeps_available_source_and_is_inconclusive(tmp_path, monkeypatch):
    app = configured_application(tmp_path, monkeypatch)
    directory, program = case(app, tmp_path, 1)
    result = app.evaluation.check_oracle(directory, {1: ProgramRes(True, program.stats)})
    app.reporting.update_stats(result, 1, 0)
    assert not result.faults
    assert result.inconclusive[1]['comparison']['incomplete_reasons'] == ['Generation failed']
    assert (Path(app.options.test_directory) / '1' / 'program.kt').exists()
    assert app.reporting.stats['totals']['inconclusive'] == 1


def test_reproductions_reference_published_direct_and_transitive_dependencies(
        tmp_path, monkeypatch):
    app = configured_application(tmp_path, monkeypatch)
    for pid, direct, closure in ((1, None, []), (2, 1, [1])):
        directory, program = case(app, tmp_path, pid, direct, closure)
        commit(app, app.evaluation.check_oracle(directory, {pid: program}))
    directory, consumer = case(app, tmp_path, 3, 2, [2, 1])
    rejecting_tool(app)
    dependency_checking_tool(app)
    result = app.evaluation.check_oracle(directory, {3: consumer})
    assert result.faults
    run = Path(app.options.test_directory)
    retained = run / '3'
    assert not (retained / 'dependencies').exists()
    manifest = json.loads((retained / 'comparison.json').read_text())
    assert manifest['dependencies']['closure'] == [2, 1]
    assert manifest['available_dependencies'] == {
        str(pid): {backend: True for backend in app.compilation.settings_by_variant}
        for pid in (2, 1)}
    for backend in app.compilation.settings_by_variant:
        evidence = json.loads((retained / 'variants' / backend / 'result.json').read_text())
        argv = evidence['reproduce_argv']
        assert str(run) not in ' '.join(argv)
        suffix = '.jar' if backend == 'jvm' else '.klib'
        direct, transitive = (os.path.join('..', 'klibs', f'd{pid}-{backend}{suffix}')
                              for pid in (2, 1))
        assert referenced_klibs(argv) == [direct, transitive, direct]
        for pid, path in ((2, direct), (1, transitive)):
            assert ((retained / path).resolve()
                    == Path(app.manager.published_klib_path(pid, backend)).resolve())

    # Retained cases depend on their run directory, which may move as a whole.
    moved = tmp_path / 'elsewhere' / 'run'
    moved.parent.mkdir()
    shutil.move(str(run), moved)
    for backend in app.compilation.settings_by_variant:
        evidence = json.loads((moved / '3' / 'variants' / backend / 'result.json').read_text())
        attempt = subprocess.run(evidence['reproduce_argv'], cwd=moved / '3',
                                 capture_output=True, text=True)
        assert attempt.returncode == (1 if backend == 'js' else 0), attempt.stdout
    shutil.rmtree(moved / 'klibs')
    evidence = json.loads((moved / '3' / 'variants' / 'native' / 'result.json').read_text())
    attempt = subprocess.run(evidence['reproduce_argv'], cwd=moved / '3',
                             capture_output=True, text=True)
    assert attempt.returncode == 3
    assert 'missing library ' + os.path.join('..', 'klibs', 'd2-native.klib') in attempt.stdout


@pytest.mark.parametrize('start_method', [method for method in ('spawn', 'fork')
                                          if method in mp.get_all_start_methods()])
def test_worker_comparison_returns_reproducible_case_and_parent_commits_bundle(
        tmp_path, monkeypatch, start_method):
    app = configured_application(tmp_path, monkeypatch)
    directory, program = case(app, tmp_path, 1)
    context = mp.get_context(start_method)
    with context.Pool(1, initializer=initialize_scheduler_worker,
                      initargs=(app.scheduler.worker_inputs('versions'),)) as pool:
        result = pool.apply(check_oracle_mul, (directory, {1: program}))
    assert not result.faults and not result.inconclusive
    # Workers publish artifacts; only the parent advertises them in the ledger.
    assert app.manager.load_ledger() == {}
    assert not Path(app.manager.ledger_path()).exists()
    app.reporting.update_stats(result, 1, 0)
    assert app.manager.provider_candidates(2) == [1]
    assert set(app.manager.load_ledger()['1']['variants']) == set(
        app.compilation.settings_by_variant)
    consumer_dir, consumer = case(app, tmp_path, 2, 1, [1])
    rejecting_tool(app)
    with context.Pool(1, initializer=initialize_scheduler_worker,
                      initargs=(app.scheduler.worker_inputs('versions'),)) as pool:
        result = pool.apply(check_oracle_mul, (consumer_dir, {2: consumer}))
    assert result.faults and not result.publications
    app.reporting.update_stats(result, 1, 0)
    assert (Path(app.options.test_directory) / '2' / 'comparison.json').exists()
    assert app.reporting.stats['totals'] == {'passed': 1, 'failed': 1, 'inconclusive': 0}
    assert set(app.manager.load_ledger()) == {'1'}
    stats = json.loads((Path(app.options.test_directory) / 'stats.json').read_text())
    assert not CROSSMODULE_STATS_FIELDS & stats.keys()
    assert not CROSSMODULE_STATS_FIELDS & app.reporting.stats.keys()


def test_replayed_program_runs_through_preflight_and_four_variants(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, 'crossmodule_probability', 0)
    source = tmp_path / 'input.bin'
    replay(source)
    app = application(tmp_path, ('-R', str(source), '--keep-everything',
                                '--time-metrics', '--disable-metrics'))
    tool = fixture_tool(tmp_path)
    library = tmp_path / 'stdlib'
    library.mkdir()
    monkeypatch.setattr(KotlinCompiler, 'stdlib_path', lambda self: str(library))
    configure_variants(app, tool)
    app.start()
    destination = Path(app.options.test_directory)
    assert app.reporting.stats['totals'] == {'passed': 1, 'failed': 0, 'inconclusive': 0}
    assert set(json.loads((destination / '1' / 'comparison.json').read_text())['compiler_versions']) == {
        'js', 'native', 'wasm', 'jvm'}
    assert set((destination / '1' / 'variants' / backend / 'result.json').exists()
               for backend in app.compilation.settings_by_variant) == {True}
    assert app.reporting.stats['compilation_time'] > 0
    assert len(app.reporting.stats['time_metrics'][1]['variants']) == 4