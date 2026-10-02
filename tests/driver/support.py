from collections.abc import Sequence
import os
from pathlib import Path
import signal
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 60


def load_driver():
    import hephaestus
    return hephaestus


def launch(scenario: str, workspace: Path,
           arguments: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    workspace.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    temporary = workspace.resolve() / 'temporary'
    temporary.mkdir(exist_ok=True)
    env['TMPDIR'] = str(temporary)
    env['PYTHONPATH'] = os.pathsep.join((str(ROOT), env.get('PYTHONPATH', '')))
    command = [sys.executable]
    coverage_dir = env.get('DRIVER_COVERAGE_DIR')
    if coverage_dir:
        destination = Path(coverage_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        env['COVERAGE_FILE'] = str(destination / '.coverage')
        command += ['-m', 'coverage', 'run',
                    '--rcfile=' + str(ROOT / 'tests/driver/coverage.ini')]
    command += ['-m', 'tests.driver.runner', scenario, str(workspace.resolve()),
                *(arguments or [])]
    with subprocess.Popen(command, cwd=workspace, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            raise AssertionError(f'Scenario timed out: {command}\n{stdout}\n{stderr}')
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f'{result.args}\nexit={result.returncode}\n{result.stdout}\n{result.stderr}')


def assert_arguments(arguments: Sequence[str]) -> None:
    assert arguments
    assert all(type(argument) is str for argument in arguments)