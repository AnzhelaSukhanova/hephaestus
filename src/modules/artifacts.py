import json
import os
import shutil

from src import utils
from src.modules.worker import RunOptions
from src.generators.generator import EscalationLog
from src.ir import ast
from src.tools.changes_from_ir_dumps import write_program_ir_changes


class Artifacts:
    def __init__(self, options: RunOptions) -> None:
        self.options = options

    def get_generator_dir(self, pid: int) -> str:
        return os.path.join(self.options.test_directory, "generator",
                            "iter_" + str(pid))

    def get_transformations_dir(self, pid: int, tid: int) -> str:
        return os.path.join(self.options.test_directory, "transformations",
                            "iter_" + str(pid), str(tid))

    def save_program(self, program: ast.Program, program_str: str,
                     program_file: str) -> None:
        dst_dir = os.path.dirname(program_file)
        utils.mkdir(dst_dir)
        # Save the program
        utils.save_text(program_file, program_str)
        utils.dump_program(program_file + ".bin", program)

    def save_escalations(self, pid: int,
                         escalations: list[EscalationLog]) -> None:
        dst_file = os.path.join(self.options.test_directory, 'tmp', str(pid),
                                'escalations.json')
        utils.mkdir(os.path.dirname(dst_file))
        with open(dst_file, 'w') as out:
            json.dump(escalations, out, indent=2)

    def _cleanup_program_tmp(self, pid: int) -> None:
        shutil.rmtree(os.path.join(self.options.test_directory, 'tmp', str(pid)),
                      ignore_errors=True)

    def preserve_final_program_dir(self, pid: int) -> None:
        src_dir = os.path.join(self.options.test_directory, 'tmp', str(pid))
        dst_dir = os.path.join(self.options.test_directory, str(pid))
        if os.path.exists(src_dir):
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)

    def preserve_ir_changes_to_tmp(self, dirname: str, pid: int
                                   ) -> dict[str, int | None] | None:
        if not self.options.dump_ir:
            return None
        dump_root = os.path.join(dirname, 'src', 'ir')
        dst_dir = os.path.join(self.options.test_directory, 'tmp', str(pid))
        out_file = write_program_ir_changes(dst_dir, dump_root)
        if os.path.isdir(dump_root):
            shutil.rmtree(dump_root)
        with open(out_file) as f:
            return json.load(f)