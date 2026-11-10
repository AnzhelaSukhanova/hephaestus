from dataclasses import dataclass
from pathlib import Path
import shutil

import pytest

from tests.driver.fixtures import replay
from tests.driver import support


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    before: str
    after: str
    scenario: str
    arguments: tuple[str, ...] = ()


MUTATIONS = [
    Mutation('oracle_inverted', 'src/oracles/compilation.py',
             'if oracle and program in diagnostics:',
             'if oracle and program not in diagnostics:',
             'scenarios_oracle.correct_rejected', ('--disable-metrics',)),
    Mutation('stats_omitted', 'src/modules/worker_reporting.py',
             "self.stats['totals']['passed'] += passed",
             "self.stats['totals']['passed'] += 0", 'scenarios_persistence.accumulated_stats'),
    Mutation('preservation_omitted', 'src/modules/artifacts.py',
             'if os.path.exists(src_dir):\n'
             '            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)',
             'if os.path.exists(src_dir):\n            pass',
             'scenarios_persistence.preservation'),
    Mutation('publication_omitted', 'src/modules/evaluation.py',
             'published[pid] = record', 'pass',
             'scenarios_crossmodule.chain', ('--disable-metrics',)),
    Mutation('friend_closure', 'src/modules/evaluation.py',
             'friend_klibs = manager.dependency_klibs(closure[:1])',
             'friend_klibs = manager.dependency_klibs(closure)',
             'scenarios_crossmodule.chain', ('--disable-metrics',)),
    Mutation('worker_callback_dropped', 'src/modules/scheduler.py',
             'self.reporting.update_stats(result, batch.size, batch_time, escalations)',
             'pass',
             'scenarios_workers.spawn', ('--workers', '2', '--iterations', '3',
                                          '--keep-successful', '--disable-metrics')),
]


@pytest.mark.parametrize('mutation', MUTATIONS, ids=lambda mutation: mutation.name)
def test_mutation_sensitivity(mutation: Mutation, tmp_path: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    original = support.ROOT
    original_source = (original / mutation.path).read_text()
    copied = tmp_path / 'copy'
    copied.mkdir()
    shutil.copy2(original / 'hephaestus.py', copied / 'hephaestus.py')
    shutil.copytree(original / 'src', copied / 'src', ignore=shutil.ignore_patterns('__pycache__'))
    (copied / 'tests').mkdir()
    shutil.copy2(original / 'tests/__init__.py', copied / 'tests/__init__.py')
    shutil.copy2(original / 'tests/kotlin_compilers_fakes.py',
                 copied / 'tests/kotlin_compilers_fakes.py')
    shutil.copytree(original / 'tests/driver', copied / 'tests/driver',
                    ignore=shutil.ignore_patterns('__pycache__'))
    target = copied / mutation.path
    assert target.read_text() == original_source
    monkeypatch.setattr(support, 'ROOT', copied)
    monkeypatch.delenv('DRIVER_COVERAGE_DIR', raising=False)
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    source = tmp_path / 'input.bin'
    replay(source)
    arguments = [*mutation.arguments, '--replay', str(source)]
    scenario = 'tests.driver.' + mutation.scenario
    support.assert_success(support.launch(scenario, tmp_path / 'baseline', arguments))
    assert original_source.count(mutation.before) == 1, 'Mutation anchor must be unambiguous'
    target.write_text(original_source.replace(mutation.before, mutation.after))
    result = support.launch(scenario, tmp_path / 'mutated', arguments)
    assert result.returncode == 1, f'Mutation survived: {mutation.name}\n{result.stdout}\n{result.stderr}'
    assert 'AssertionError' in result.stderr or 'FileNotFoundError' in result.stderr
    assert 'SyntaxError' not in result.stderr and 'ImportError' not in result.stderr
    assert (original / mutation.path).read_text() == original_source