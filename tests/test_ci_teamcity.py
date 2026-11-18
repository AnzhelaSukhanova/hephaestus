"""The TeamCity step scripts and Kotlin DSL in ``ci/teamcity`` (run with fake docker/curl)."""
import shutil
import subprocess

import pytest

from tests.driver.support import ROOT


DRY_RUN = ROOT / 'ci' / 'teamcity' / 'tests' / 'dry-run.sh'


@pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')
def test_teamcity_scripts_dry_run():
    done = subprocess.run(['bash', str(DRY_RUN)], capture_output=True, text=True, timeout=300)
    failures = [line for line in done.stdout.splitlines() if line.startswith('FAIL')]
    assert done.returncode == 0 and not failures, done.stdout + done.stderr
    assert done.stdout.rstrip().endswith('All checks passed.')
