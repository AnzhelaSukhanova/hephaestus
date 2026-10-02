import json
import os
from pathlib import Path
import shlex
import sys
from typing import TYPE_CHECKING

from tests.driver.support import ROOT, load_driver

if TYPE_CHECKING:
    from hephaestus import ProgramRes


def initialize_worker(version: str) -> None:
    import src.compilers.kotlin as kotlin
    kotlin.compiler = (shlex.quote(sys.executable) + ' ' +
                       shlex.quote(str(ROOT / 'tests/driver/fake_compiler.py')))
    driver = load_driver()
    manager = driver.setup_crossmodule_manager(version)
    assert driver.setup_crossmodule_manager(version) is manager
    Path(f'worker_{os.getpid()}.json').write_text(json.dumps({
        'pid': driver.CROSSMODULE_MANAGER_PID, 'version': manager.compiler_version}))


def probe() -> tuple[int, 'ProgramRes']:
    driver = load_driver()
    # This body is run only in a worker. The parent sentinel uses a different helper.
    driver.save_escalations(4321, [])
    result = driver.gen_program_mul(4321, str(Path('worker_source').resolve()))
    assert result is not None
    return os.getpid(), result