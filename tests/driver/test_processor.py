from pathlib import Path

import pytest

from tests.driver.support import assert_success, launch


@pytest.mark.parametrize('scenario', ['schedules', 'real_transformations', 'generated_context'])
def test_processor(scenario: str, tmp_path: Path) -> None:
    assert_success(launch('tests.driver.scenarios_processor.' + scenario, tmp_path))