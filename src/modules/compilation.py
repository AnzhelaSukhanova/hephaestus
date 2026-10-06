from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
import shutil
import subprocess as sp
import time
from types import MappingProxyType

from src import utils
from src.compilers.base import BaseCompiler
from src.compilers.groovy import GroovyCompiler
from src.compilers.java import JavaCompiler
from src.compilers.kotlin import KotlinCompiler
from src.compilers.scala import ScalaCompiler
from src.modules.artifacts import Artifacts
from src.modules.generation import TRANSLATORS
from src.modules.models import ProgramRes
from src.modules.worker import RunOptions


COMPILERS = {
    'kotlin': KotlinCompiler,
    'groovy': GroovyCompiler,
    'java': JavaCompiler,
    'scala': ScalaCompiler
}


@dataclass(frozen=True)
class CommandObservation:
    status: bool
    output: str | sp.CalledProcessError
    diagnostics: Mapping[str, Sequence[str]]
    crash_message: str | None
    command: tuple[str, ...]
    cwd: str | None
    elapsed: float

    def __post_init__(self) -> None:
        object.__setattr__(self, 'diagnostics', MappingProxyType({
            program: tuple(messages)
            for program, messages in self.diagnostics.items()
        }))
        object.__setattr__(self, 'command', tuple(self.command))


class Compilation[S]:
    def __init__(self, options: RunOptions, artifacts: Artifacts,
                 compiler_types: Mapping[str, type[BaseCompiler[S]]], *,
                 settings: S) -> None:
        self.options = options
        self.artifacts = artifacts
        self.compiler_types = compiler_types
        self.settings = settings

    def make_compiler(self, *args, **kwargs) -> BaseCompiler[S]:
        compiler = self.compiler_types[self.options.language]
        kwargs['settings'] = self.settings
        return compiler(*args, **kwargs)

    def version_command(self) -> list[str]:
        compiler = self.compiler_types[self.options.language]
        return compiler.get_compiler_version(self.settings)

    def run_command(self, arguments: list[str], get_stdout: bool = True,
                    cwd: str | None = None
                    ) -> tuple[bool, str | sp.CalledProcessError]:
        """Run a command
        Args:
            A list with the arguments to execute. For example ['ls', 'foo']
            cwd: the directory to run the command from.  The Kotlin compiler
                resolves modules by unique name against its working directory,
        Returns:
            return status, stderr.
        """
        is_groovy = arguments[0] == "groovyc"
        if is_groovy:
            tmp_src_dir = os.path.join(self.options.test_directory, 'tmp')
            utils.mkdir(tmp_src_dir)
            old_cwd = os.getcwd()
            os.chdir(tmp_src_dir)
        try:
            is_windows = os.name == 'nt'
            sys_env = os.environ.copy()
            sys_env['JAVA_OPTS'] = "-Xmx8g"
            if not is_windows:
                # FIXME the wildcard * maybe won't work in Windows
                command: str | list[str] = ' '.join(arguments)
            else:
                command = arguments
            cmd = sp.Popen(command, stdout=sp.PIPE,
                           stderr=sp.STDOUT, shell=True, env=sys_env,
                           cwd=cwd)
            stdout, stderr = cmd.communicate()
        except sp.CalledProcessError as err:
            return False, err
        if is_groovy:
            os.chdir(old_cwd)
        stderr = stderr.decode("utf-8") if stderr else ""
        stdout = stdout.decode("utf-8") if stdout else ""
        err = stdout if get_stdout else stderr
        status = cmd.returncode == 0
        return status, err

    @staticmethod
    def timed[**P, R](fn: Callable[P, R], *args: P.args,
                      **kwargs: P.kwargs) -> tuple[R, float]:
        start = time.perf_counter()
        result = fn(*args, **kwargs)
        return result, time.perf_counter() - start

    def observe(self, compiler: BaseCompiler[S],
                cwd: str | None = None) -> CommandObservation:
        command_args = compiler.get_compiler_cmd()
        (status, output), elapsed = self.timed(
            self.run_command, command_args, cwd=cwd)
        diagnostics, _ = compiler.analyze_compiler_output(output)
        return CommandObservation(
            status, output, diagnostics or {}, compiler.crash_msg,
            tuple(command_args), cwd, elapsed)

    def enrich_error(self, compiler: BaseCompiler[S], err_file: str,
                     cwd: str | None = None) -> dict[str, str]:
        command_outputs: dict[str, str] = {}

        for name, command in compiler.get_error_enrichment_cmds(err_file).items():
            _, output = self.run_command(command, cwd=cwd)
            command_outputs[name] = output

        return compiler.analyze_error_enrichment_output(err_file, command_outputs)

    def _report_failed(self, pid: int, tid: int | None, compiler: BaseCompiler[S],
                       oracle: bool) -> None:
        """Find which program introduce the error and then report it.
        """
        translator = TRANSLATORS[self.options.language]()
        prev_file = None
        while tid:
            program_file = os.path.join(
                self.artifacts.get_transformations_dir(pid, tid),
                translator.get_filename())
            compiler = self.make_compiler(program_file)
            status, _ = self.run_command(compiler.get_compiler_cmd())
            if status == oracle:
                dst_file = os.path.join(self.options.test_directory, 'tmp', str(pid),
                                        "initial_program.kt")
                dst_file2 = os.path.join(self.options.test_directory, 'tmp', str(pid),
                                         "program.kt")
                shutil.copyfile(program_file, dst_file)
                shutil.copyfile(program_file + ".bin", dst_file + ".bin")
                if prev_file:
                    shutil.copyfile(prev_file, dst_file2)
                    shutil.copyfile(prev_file + ".bin", dst_file2 + ".bin")
                break

            prev_file = program_file
            tid -= 1

    def _build_crossmodule_provider(
            self, proc_res: ProgramRes, filter_patterns: set[str],
            dependency_paths: list[str], friend_paths: list[str],
            module_name: str, compile_cwd: str
            ) -> tuple[tuple[str, list[str]] | None, float]:
        """Build the correct source as an isolated provider KLIB."""
        correct_program, = (
            program
            for program, oracle in proc_res.stats['programs'].items()
            if oracle)
        provider_compiler = self.make_compiler(
            correct_program, filter_patterns,
            dependency_klibs=dependency_paths,
            friend_klibs=friend_paths,
            module_name=module_name)
        provider_command_args = provider_compiler.get_compiler_cmd()
        provider_klib = os.path.join(
            compile_cwd, provider_compiler.get_klib_filename())
        if os.path.isdir(provider_klib):
            shutil.rmtree(provider_klib)
        elif os.path.exists(provider_klib):
            os.remove(provider_klib)
        (provider_status, _), compilation_time = self.timed(
            self.run_command, provider_command_args, cwd=compile_cwd)
        if provider_status:
            return (provider_klib, provider_command_args), compilation_time
        return None, compilation_time