import json
import os
from pathlib import Path

from src.modules.models import ProgramRes
from src.modules.scheduler import gen_program_mul, initialize_scheduler_worker
from src.modules.worker import WorkerInputs, get_worker_resources


def initialize_worker[S](inputs: WorkerInputs[S]) -> None:
    import src.compilers.kotlin as kotlin
    kotlinc = str(Path('tools/kotlinc').resolve())
    resolve_executable = kotlin._compiler_executable

    def hermetic_executable(settings: kotlin.KotlinSettings) -> str:
        # Spawned workers re-import the adapter, so the runner's substitution is repeated.
        executable = resolve_executable(settings)
        return kotlinc if executable.startswith('$HOME/kotlin/') else executable

    kotlin._compiler_executable = hermetic_executable
    initialize_scheduler_worker(inputs)
    resources = get_worker_resources()
    assert resources.scheduler is not None
    assert resources.scheduler.generation.manager is resources.manager
    assert resources.scheduler.evaluation.manager is resources.manager
    Path(f'worker_{os.getpid()}.json').write_text(json.dumps({
        'pid': os.getpid(), 'version': resources.manager.compiler_version}))


def probe() -> tuple[int, ProgramRes]:
    scheduler = get_worker_resources().scheduler
    assert scheduler is not None
    # This body is run only in a worker. The parent sentinel uses a different helper.
    scheduler.generation.artifacts.save_escalations(4321, [])
    result = gen_program_mul(4321, str(Path('worker_source').resolve()))
    assert result is not None
    return os.getpid(), result
