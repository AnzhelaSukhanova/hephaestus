"""Cross-module integration only; all scenario failures are ordinary failures."""
from pathlib import Path

import pytest

from tests.driver.support import assert_success, launch


@pytest.mark.parametrize('backend', ['native', 'js', 'wasm'])
@pytest.mark.parametrize('scenario', [
    'chain', 'dropped_published', 'dropped_rejected', 'disabled',
    'publication_ir', 'dependent_crash',
])
def test_crossmodule(backend: str, scenario: str, tmp_path: Path) -> None:
    assert_success(launch(f'tests.driver.scenarios_crossmodule.{scenario}', tmp_path,
                          ['--backend', backend, '--disable-metrics']))


@pytest.mark.parametrize('backend', ['native', 'js', 'wasm'])
@pytest.mark.parametrize('scenario', ['ncp_file', 'ncp_directory'])
def test_isolated_provider(backend: str, scenario: str, tmp_path: Path) -> None:
    assert_success(launch(f'tests.driver.scenarios_crossmodule.{scenario}', tmp_path,
                          ['--backend', backend, '--disable-metrics', '--time-metrics']))


@pytest.mark.parametrize('scenario', ['provider_error_file', 'provider_error_directory'])
def test_provider_error(scenario: str, tmp_path: Path) -> None:
    assert_success(launch(f'tests.driver.scenarios_crossmodule.{scenario}', tmp_path,
                          ['--disable-metrics', '--time-metrics']))


@pytest.mark.parametrize('scenario', [
    'manifest_file', 'manifest_directory', 'manifest_dropped',
    'missing_source', 'missing_pickle', 'corrupt_pickle', 'none_pickle', 'missing_klib',
    'stale_candidates', 'malformed_ledger', 'unreadable_manifests', 'pid_cache',
])
def test_publication(scenario: str, tmp_path: Path) -> None:
    assert_success(launch(f'tests.driver.scenarios_crossmodule.{scenario}', tmp_path,
                          ['--disable-metrics']))