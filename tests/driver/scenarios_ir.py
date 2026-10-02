from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch
import zipfile

import pytest

from src.tools import ir_coverage
from src.tools.ir_csv import PHASES
from tests.driver.support import load_driver


CLASS_FILE = 'org/jetbrains/kotlin/backend/common/lower/FunctionInlining.class'
COLUMN = '15_FunctionInlining'


@contextmanager
def tools() -> Iterator[None]:
    root = Path('jacoco_tools').resolve()
    (root / 'coverage').mkdir(parents=True)
    (root / 'coverage/jacocoagent.jar').touch()
    (root / 'coverage/jacococli.jar').touch()
    compiler = root / 'compiler.jar'
    with zipfile.ZipFile(compiler, 'w') as archive:
        archive.writestr(CLASS_FILE, b'class bytes')
        archive.writestr('not_selected.class', b'ignored')
    with patch.object(ir_coverage, 'repo_root', return_value=root), \
            patch.object(ir_coverage, 'compiler_jar', return_value=compiler), \
            patch.object(ir_coverage, 'sourcefile_args', return_value=[]):
        yield


def summaries() -> None:
    driver = load_driver()
    driver.cli_args.dump_ir = True
    root = Path('batch')
    dumps = root / 'src/ir/module'
    dumps.mkdir(parents=True)
    for number, name in PHASES[:2]:
        (dumps / f'{number}_BEFORE.{name}.ir').write_text('before header\nsame body\n')
        (dumps / f'{number}_AFTER.{name}.ir').write_text('after header\nsame body\n')
    number, name = PHASES[1]
    (dumps / f'{number}_AFTER.{name}.ir').write_text('after header\nchanged body\n')
    expected: dict[str, int | None] = {f'{number}_{name}': None for number, name in PHASES}
    expected[f'{PHASES[0][0]}_{PHASES[0][1]}'] = 0
    expected[f'{PHASES[1][0]}_{PHASES[1][1]}'] = 1
    summary = Path(driver.cli_args.test_directory) / 'tmp/7/ir_changes.json'
    remove = shutil.rmtree

    def checked_remove(path: str) -> None:
        assert Path(path) == root / 'src/ir'
        assert json.loads(summary.read_text()) == expected
        remove(path)

    with patch.object(driver.shutil, 'rmtree', checked_remove):
        assert driver.preserve_ir_changes_to_tmp(str(root), 7) == expected
    assert not dumps.exists()
    assert driver.preserve_ir_changes_to_tmp('absent', 8) == {
        f'{number}_{name}': None for number, name in PHASES}
    driver.cli_args.dump_ir = False
    assert driver.preserve_ir_changes_to_tmp(str(root), 9) is None
    assert not (summary.parent.parent / '9').exists()


def setup() -> None:
    driver = load_driver()
    root = driver.jacoco_output_dir()
    driver.setup_live_jacoco()
    assert not root.exists()
    root.mkdir(parents=True)
    (root / 'jacoco.exec').write_text('stale')
    (root / 'logs').mkdir()
    (root / 'logs/old.log').write_text('stale')
    driver.cli_args.jacoco_always = True
    with tools():
        driver.setup_live_jacoco()
    assert not (root / 'jacoco.exec').exists()
    assert not (root / 'logs/old.log').exists()
    assert (root / 'classes' / CLASS_FILE).read_bytes() == b'class bytes'
    assert not (root / 'classes/not_selected.class').exists()
    assert (root / 'execs').is_dir()
    assert (root / '.merge.lock').is_file()


class JavaExecution:
    def __init__(self, fail: str = '') -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def run(self, command: list[str], *, stdout: int, stderr: int,
            text: bool, check: bool) -> subprocess.CompletedProcess[str]:
        assert stdout == subprocess.PIPE and stderr == subprocess.STDOUT
        assert text is True and check is False
        assert command[0:2] == ['java', '-jar']
        self.calls.append(command)
        action = command[3]
        if action == self.fail:
            return subprocess.CompletedProcess(command, 1, f'controlled {action} failure')
        if action == 'merge':
            end = command.index('--destfile')
            inputs = command[4:end]
            Path(command[end + 1]).write_bytes(b''.join(Path(path).read_bytes() for path in inputs))
        else:
            assert action == 'report'
            for flag in ('--xml', '--csv'):
                Path(command[command.index(flag) + 1]).write_text('controlled report')
            Path(command[command.index('--html') + 1]).mkdir(exist_ok=True)
        return subprocess.CompletedProcess(command, 0, f'controlled {action} success')


def assert_unlocked(root: Path) -> None:
    with (root / '.merge.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_UN)


def merge() -> None:
    driver = load_driver()
    driver.cli_args.jacoco_always = True
    java = JavaExecution()
    with tools(), patch.object(ir_coverage.subprocess, 'run', java.run):
        driver.setup_live_jacoco()
        root = driver.jacoco_output_dir()
        driver._refresh_jacoco_report(root)
        assert java.calls == []
        assert_unlocked(root)
        (root / 'execs/2.exec').write_bytes(b'2')
        (root / 'execs/1.exec').write_bytes(b'1')
        driver._refresh_jacoco_report(root)
        assert (root / 'jacoco.exec').read_bytes() == b'12'
        assert not list((root / 'execs').iterdir())
        assert not (root / 'jacoco.exec.new').exists()
        assert (root / 'report.xml').read_text() == 'controlled report'
        (root / 'execs/3.exec').write_bytes(b'3')
        driver._refresh_jacoco_report(root)
        assert (root / 'jacoco.exec').read_bytes() == b'123'
        assert java.calls[2][4:6] == [str(root / 'jacoco.exec'), str(root / 'execs/3.exec')]
        assert 'controlled report success' in (root / 'jacoco-report.log').read_text()
        assert_unlocked(root)


def merge_failure() -> None:
    failed_refresh('merge')


def report_failure() -> None:
    failed_refresh('report')


def failed_refresh(action: str) -> None:
    driver = load_driver()
    driver.cli_args.jacoco_always = True
    java = JavaExecution(action)
    with tools(), patch.object(ir_coverage.subprocess, 'run', java.run):
        driver.setup_live_jacoco()
        root = driver.jacoco_output_dir()
        (root / 'jacoco.exec').write_bytes(b'old')
        (root / 'execs/1.exec').write_bytes(b'new')
        with pytest.raises(SystemExit, match='JaCoCo.*failed'):
            driver._refresh_jacoco_report(root)
        if action == 'merge':
            assert (root / 'jacoco.exec').read_bytes() == b'old'
            assert (root / 'execs/1.exec').read_bytes() == b'new'
        else:
            assert (root / 'jacoco.exec').read_bytes() == b'oldnew'
            assert not (root / 'execs/1.exec').exists()
            assert 'controlled report failure' in (root / 'jacoco-report.log').read_text()
        assert_unlocked(root)


def live_compile() -> None:
    driver = load_driver()
    driver.cli_args.jacoco_always = True
    stages: list[Path] = []
    java = JavaExecution()
    root = driver.jacoco_output_dir()

    def compile_command(arguments: list[str], get_stdout: bool = True,
                        cwd: str | None = None) -> tuple[bool, str]:
        assert get_stdout is True and cwd is None
        agent, = [arg for arg in arguments if arg.startswith('-J-javaagent:')]
        assert 'append=true' in agent
        stage = Path(arguments[1])
        assert (stage / 'program.kt').read_text() == 'val answer = 1'
        stages.append(stage)
        destination = agent.split('destfile=', 1)[1].split(',', 1)[0]
        # First compile exercises missing agent output; second produces coverage.
        if len(stages) == 2:
            Path(destination).write_bytes(b'execution')
        return False, 'controlled compiler diagnostics'

    with tools(), patch.object(driver, 'run_command', compile_command), \
            patch.object(ir_coverage.subprocess, 'run', java.run):
        driver.setup_live_jacoco()
        driver.maybe_run_live_jacoco(1, None)
        assert stages == []  # Source must have been preserved first.
        source = root.parent / '1/program.kt'
        source.parent.mkdir()
        source.write_text('val answer = 1')
        driver.cli_args.jacoco_always = False
        driver.maybe_run_live_jacoco(1, {COLUMN: 1})
        driver.cli_args.jacoco_lowerings = ['FunctionInlining']
        for changes in (None, {}, {COLUMN: 0}, {COLUMN: None}, {'other': 1}):
            driver.maybe_run_live_jacoco(1, changes)
        assert stages == []
        driver.maybe_run_live_jacoco(1, {COLUMN: 1})
        assert not (root / 'jacoco.exec').exists()
        assert java.calls == []
        driver.cli_args.jacoco_always = True
        driver.maybe_run_live_jacoco(1, None)
        assert (root / 'jacoco.exec').read_bytes() == b'execution'
        assert (root / 'logs/1.log').read_text() == 'controlled compiler diagnostics'
        assert all(not stage.exists() for stage in stages)
        assert not (root / 'execs/1.exec').exists()
        assert_unlocked(root)