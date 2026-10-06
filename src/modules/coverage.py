from collections.abc import Mapping
import fcntl
import os
from pathlib import Path
import shutil
import tempfile

from src import utils
from src.modules.compilation import Compilation
from src.modules.worker import RunOptions
from src.tools.ir_coverage import (
    coverage_compile_cmd,
    extract_report_classes,
    lowering_columns,
    merge_exec_files,
    prepare_output_dir,
    write_jacoco_report,
)


class LiveCoverage[S]:
    def __init__(self, options: RunOptions, compilation: Compilation[S]) -> None:
        self.options = options
        self.compilation = compilation

    def jacoco_output_dir(self) -> Path:
        return Path(self.options.test_directory) / 'coverage'

    def setup_live_jacoco(self) -> None:
        # Must run once in the parent process, before the worker pool (if any)
        # is created: it wipes stale state and does a one-time, somewhat
        # expensive class-file extraction that every worker will then share.
        if not self.options.jacoco_lowerings and not self.options.jacoco_always:
            return
        output_dir = self.jacoco_output_dir()
        prepare_output_dir(output_dir)
        (output_dir / 'execs').mkdir(parents=True, exist_ok=True)
        (output_dir / 'logs').mkdir(parents=True, exist_ok=True)
        classes_dir = output_dir / 'classes'
        classes_dir.mkdir(parents=True, exist_ok=True)
        extract_report_classes(self.options.backend, classes_dir)
        self._jacoco_merge_lock_path(output_dir).touch(exist_ok=True)

    @staticmethod
    def _jacoco_merge_lock_path(output_dir: Path) -> Path:
        return output_dir / '.merge.lock'

    def _refresh_jacoco_report(self, output_dir: Path) -> None:
        # Serialize merge + report generation across worker processes: several
        # pids can finish a live-Jacoco compile at roughly the same time, and
        # jacococli.jar has no cross-process coordination of its own.
        with open(self._jacoco_merge_lock_path(output_dir), 'w') as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                exec_dir = output_dir / 'execs'
                pending = sorted(exec_dir.glob('*.exec'))
                if not pending:
                    return
                trunk = output_dir / 'jacoco.exec'
                inputs = ([trunk] if trunk.exists() else []) + pending
                merged = output_dir / 'jacoco.exec.new'
                merge_exec_files(inputs, merged)
                os.replace(merged, trunk)
                for exec_file in pending:
                    exec_file.unlink()
                write_jacoco_report(output_dir, output_dir / 'classes',
                                   self.options.backend)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def maybe_run_live_jacoco(
            self, pid: int, phase_changes: Mapping[str, int | None] | None
            ) -> None:
        if self.options.jacoco_always:
            pass
        elif self.options.jacoco_lowerings and phase_changes:
            columns = lowering_columns(self.options.jacoco_lowerings)
            if not any(phase_changes.get(column) == 1 for column in columns):
                return
        else:
            return
        program_file = os.path.join(self.options.test_directory, str(pid), 'program.kt')
        if not os.path.isfile(program_file):
            return

        output_dir = self.jacoco_output_dir()
        # A given pid is only ever handled by one worker, so its own exec file
        # has no concurrent writer and needs no locking.
        dest_file = output_dir / 'execs' / "{}.exec".format(pid)
        with tempfile.TemporaryDirectory(prefix="hephaestus-live-jacoco-") as tmp:
            stage_dir = os.path.join(tmp, 'src')
            utils.mkdir(stage_dir)
            shutil.copy2(program_file, os.path.join(stage_dir, 'program.kt'))
            _, out = self.compilation.run_command(
                coverage_compile_cmd(self.options.backend, stage_dir, output_dir,
                                     dest_file=dest_file))
            with open(output_dir / 'logs' / "{}.log".format(pid), 'w') as log:
                log.write(out)

        if dest_file.exists():
            self._refresh_jacoco_report(output_dir)