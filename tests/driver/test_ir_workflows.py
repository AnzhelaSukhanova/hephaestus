from pathlib import Path

import pytest

from tests.driver.support import assert_success, launch


@pytest.mark.parametrize('scenario', ['summaries', 'setup', 'merge', 'merge_failure',
                                     'report_failure', 'live_compile'])
@pytest.mark.parametrize('backend', ['native', 'js', 'wasm'])
def test_ir_workflow(tmp_path: Path, scenario: str, backend: str) -> None:
    assert_success(launch('tests.driver.scenarios_ir.' + scenario, tmp_path,
                          ['--backend', backend]))