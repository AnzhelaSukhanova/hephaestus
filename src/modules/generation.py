import os
import time
import traceback

from src import utils
from src.modules.artifacts import Artifacts
from src.modules.models import ProgramRes
from src.modules.worker import RunOptions
from src.generators.config import cfg
from src.ir import ast
from src.modules.crossmodule import CrossModuleManager
from src.modules.processor import ProgramProcessor
from src.translators.kotlin import KotlinTranslator
from src.translators.groovy import GroovyTranslator
from src.translators.scala import ScalaTranslator
from src.translators.java import JavaTranslator


type Translator = KotlinTranslator | GroovyTranslator | JavaTranslator | ScalaTranslator
TRANSLATORS: dict[str, type[Translator]] = {
    'kotlin': KotlinTranslator,
    'groovy': GroovyTranslator,
    'java': JavaTranslator,
    'scala': ScalaTranslator
}


class Generation:
    def __init__(self, options: RunOptions, artifacts: Artifacts,
                 manager: CrossModuleManager | None = None) -> None:
        self.options = options
        self.artifacts = artifacts
        self.manager = manager

    def process_cp_transformations(self, pid: int, dirname: str,
                                   translator: Translator,
                                   proc: ProgramProcessor, program: ast.Program,
                                   package_name: str) -> str:
        program_str = None
        while proc.can_transform():
            res = proc.transform_program(program)
            if res is None:
                continue
            program, oracle = res
            if self.options.keep_all:
                # Save every program resulted by the current transformation.
                program_str = utils.translate_program(translator, program)
                self.artifacts.save_program(
                    program,
                    program_str,
                    os.path.join(
                        self.artifacts.get_transformations_dir(
                            pid, proc.current_transformation - 1),
                        translator.get_filename())
                )
        if program_str is None:
            program_str = utils.translate_program(translator, program)
        dst_file = os.path.join(dirname, package_name,
                                translator.get_filename())
        dst_file2 = os.path.join(self.options.test_directory, 'tmp', str(pid),
                                 translator.get_filename())
        self.artifacts.save_program(program, program_str, dst_file)
        self.artifacts.save_program(program, program_str, dst_file2)
        return dst_file

    def process_ncp_transformations(self, pid: int, dirname: str,
                                    translator: Translator,
                                    proc: ProgramProcessor,
                                    program: ast.Program, package_name: str
                                    ) -> tuple[str, str | None] | None:
        translator.package = 'src.' + package_name
        res = proc.inject_fault(program)
        if res is None:
            return None
        program, injected_err = res
        if self.options.keep_all:
            # Save every program resulted by the current transformation.
            program_str = utils.translate_program(translator, program)
            self.artifacts.save_program(
                program,
                program_str,
                os.path.join(self.artifacts.get_generator_dir(pid),
                             translator.get_incorrect_filename())
            )
        dst_file = os.path.join(dirname, package_name,
                                translator.get_filename())
        dst_file2 = os.path.join(self.options.test_directory, 'tmp', str(pid),
                                 translator.get_incorrect_filename())
        program_str = utils.translate_program(translator, program)
        self.artifacts.save_program(program, program_str, dst_file)
        self.artifacts.save_program(program, program_str, dst_file2)
        return dst_file, injected_err

    def gen_program(self, pid: int, dirname: str) -> ProgramRes:
        """
        This function is responsible processing an iteration.

        It generates a program with a given id, it then applies a number of
        transformations, and finally it saves the resulting program into the
        given directory.

        The program belongs to a package derived from its pid.
        """
        utils.random.reset_word_pool()
        package_name = 'd' + str(pid)
        generation_package = 'src.' + package_name
        dependency = None
        if (cfg.prob.crossmodule_probability and
                utils.random.bool(cfg.prob.crossmodule_probability)):
            dependency = self.manager.prepare_dependency(pid)

        direct_dependency_pid = dependency['pid'] if dependency else None
        dep_transitive_closure_klibs = dependency['klibs'] if dependency else None
        translator = TRANSLATORS[self.options.language](
            generation_package,
            self.options.options['Translator'])
        # For Kotlin we reuse generation_package as module name we're making
        # There's 1:1 correspondence between packages (exist for all programs) and modules (only in Kotlin)
        proc = ProgramProcessor(
            pid,
            self.options,
            generation_package if self.options.language == 'kotlin' else None,
        )
        try:
            start_time_gen = time.process_time()
            if dependency:
                program, oracle = proc.get_program(pre_existing_context=dependency['context'])
            else:
                program, oracle = proc.get_program()
            if self.options.examine:
                print("pp program.context._context (to print the context)")
                __import__('ipdb').set_trace()
            if self.options.keep_all:
                # Save the initial program.
                self.artifacts.save_program(
                    program,
                    utils.translate_program(translator, program),
                    os.path.join(self.artifacts.get_generator_dir(pid),
                                 translator.get_filename())
                )
            correct_program = self.process_cp_transformations(
                pid, dirname, translator, proc, program, package_name)
            if not self.options.disable_metrics:
                self.artifacts.save_escalations(pid, proc.escalations)
            stats = {
                'transformations': [t.get_name()
                                    for t in proc.get_transformations()],
                'error': None,
                'programs': {
                    correct_program: True
                },
                "time": time.process_time() - start_time_gen,
            }
            if not self.options.disable_metrics:
                stats["escalations"] = proc.escalations
            if not self.options.only_correctness_preserving_transformations:
                incorrect_program = self.process_ncp_transformations(
                    pid, dirname, translator, proc, program,
                    package_name + '_incorrect')
                if incorrect_program:
                    stats['error'] = incorrect_program[1]
                    stats['programs'][incorrect_program[0]] = False
            return ProgramRes(
                False, stats, direct_dependency_pid,
                dep_transitive_closure_klibs)
        except Exception as exc:
            # This means that we have programming error in transformations
            err = ''
            if self.options.print_stacktrace:
                err = str(traceback.format_exc())
            else:
                err = str(exc)
            if self.options.debug:
                print(err)
            stats = {
                'transformations': [t.get_name()
                                    for t in proc.get_transformations()],
                'error': err,
                'program': None,
                'time': 0
            }
            return ProgramRes(
                True, stats, direct_dependency_pid,
                dep_transitive_closure_klibs)