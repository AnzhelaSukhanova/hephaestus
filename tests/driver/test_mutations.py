from dataclasses import dataclass
from pathlib import Path
import shutil

import pytest

from tests.driver.fixtures import replay
from tests.driver import support


@dataclass(frozen=True)
class Mutation:
    name: str
    before: str
    after: str
    scenario: str
    arguments: tuple[str, ...] = ()


MUTATIONS = [
    Mutation('oracle_inverted', 'if oracle and program in failed:',
             'if oracle and program not in failed:',
             'scenarios_oracle.correct_rejected', ('--disable-metrics',)),
    Mutation('stats_omitted', "STATS['totals']['passed'] += passed",
             "STATS['totals']['passed'] += 0", 'scenarios_persistence.accumulated_stats'),
    Mutation('preservation_omitted', 'shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)',
             'pass', 'scenarios_persistence.preservation'),
    Mutation('publication_omitted', 'published[pid] = record', 'pass',
             'scenarios_crossmodule.chain', ('--disable-metrics',)),
    Mutation('friend_closure', 'friend_paths.append(klibs[0])',
             'friend_paths.extend(klibs)', 'scenarios_crossmodule.chain', ('--disable-metrics',)),
    Mutation('worker_callback_dropped', 'callback=update)', 'callback=None)',
             'scenarios_workers.spawn', ('--workers', '2', '--iterations', '3',
                                          '--keep-everything', '--disable-metrics')),
]


@pytest.mark.parametrize('mutation', MUTATIONS, ids=lambda mutation: mutation.name)
def test_mutation_sensitivity(mutation: Mutation, tmp_path: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    original = support.ROOT
    original_source = (original / 'hephaestus.py').read_text()
    copied = tmp_path / 'copy'
    copied.mkdir()
    shutil.copytree(original / 'src', copied / 'src', ignore=shutil.ignore_patterns('__pycache__'))
    (copied / 'tests').mkdir()
    shutil.copy2(original / 'tests/__init__.py', copied / 'tests/__init__.py')
    shutil.copytree(original / 'tests/driver', copied / 'tests/driver',
                    ignore=shutil.ignore_patterns('__pycache__'))
    target = copied / 'hephaestus.py'
    target.write_text(original_source)
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
    assert (original / 'hephaestus.py').read_text() == original_source