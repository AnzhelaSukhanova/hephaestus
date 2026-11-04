import ast
from pathlib import Path

from coverage import CoverageData
import pytest

from tests.driver.support import ROOT, assert_success, launch

ARTIFACTS = ROOT / 'src/modules/artifacts.py'


def method_lines(path: Path, owner: str, name: str) -> set[int]:
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == owner)
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == name)
    return {node.lineno for node in method.body}


def measured_shards(directory: Path, path: Path) -> list[set[int]]:
    shards: list[set[int]] = []
    for shard in directory.glob('.coverage.*'):
        data = CoverageData(basename=str(shard))
        data.read()
        lines = data.lines(str(path))
        if lines:
            assert data.has_arcs(), 'Child coverage must include branches'
            shards.append(set(lines))
    return shards


def test_child_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / 'coverage'
    monkeypatch.setenv('DRIVER_COVERAGE_DIR', str(destination))
    assert_success(launch('tests.driver.coverage_probe.child_marker', tmp_path / 'child'))
    assert any(method_lines(ARTIFACTS, 'Artifacts', 'get_generator_dir') <= lines
               for lines in measured_shards(destination, ARTIFACTS)), 'No child contribution'


def test_worker_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / 'coverage'
    monkeypatch.setenv('DRIVER_COVERAGE_DIR', str(destination))
    assert_success(launch('tests.driver.scenarios_workers.coverage_worker', tmp_path / 'child'))
    shards = measured_shards(destination, ARTIFACTS)
    child = method_lines(ARTIFACTS, 'Artifacts', 'get_generator_dir')
    worker = method_lines(ARTIFACTS, 'Artifacts', 'save_escalations')
    assert any(child <= lines and not worker <= lines for lines in shards), 'No distinct child data'
    assert any(worker <= lines and not child <= lines for lines in shards), 'No distinct worker data'
