from collections.abc import Callable, Mapping, Sequence
import os
import shutil
import subprocess as sp
import tempfile
import time

from src import utils
from src.compilers.base import BaseCompiler
from src.compilers.groovy import GroovyCompiler
from src.compilers.java import JavaCompiler
from src.compilers.kotlin import KotlinCompiler
from src.compilers.scala import ScalaCompiler
from src.compilers.results import (
    CommandObservation, CompilationRequest, ProcessResult, VariantObservation,
)
from src.modules.artifacts import Artifacts
from src.modules.generation import TRANSLATORS
from src.modules.models import ProgramRes
from src.modules.worker import RunOptions


class ProviderCompilationError(RuntimeError):
    def __init__(self, process: ProcessResult, elapsed: float):
        self.process = process
        self.elapsed = elapsed
        super().__init__(process.launch_failure or process.output)


COMPILERS = {
    'kotlin': KotlinCompiler,
    'groovy': GroovyCompiler,
    'java': JavaCompiler,
    'scala': ScalaCompiler
}

# The variant id of ordinary single-configuration compilation.
ORDINARY_VARIANT = 'compilation'


class Compilation[S]:
    def __init__(self, options: RunOptions, artifacts: Artifacts,
                 compiler_types: Mapping[str, type[BaseCompiler[S]]], *,
                 settings: S,
                 settings_by_variant: Mapping[str, S] | None = None,
                 versions_by_variant: Mapping[str, str] | None = None) -> None:
        self.options = options
        self.artifacts = artifacts
        self.compiler_types = compiler_types
        self.settings = settings
        # The selected configurations, keyed by variant_id, and the versions
        # discovered for them once at startup.
        self.settings_by_variant: Mapping[str, S] = dict(
            settings_by_variant if settings_by_variant is not None
            else {ORDINARY_VARIANT: settings})
        self.versions_by_variant: dict[str, str] = dict(versions_by_variant or {})

    def make_compiler(self, *args, **kwargs) -> BaseCompiler[S]:
        compiler = self.compiler_types[self.options.language]
        kwargs.setdefault('settings', self.settings)
        return compiler(*args, **kwargs)

    def discover_versions(self) -> None:
        """Query and record the version of every selected configuration."""
        compiler = self.compiler_types[self.options.language]
        with tempfile.TemporaryDirectory() as directory:
            for variant_id, settings in self.settings_by_variant.items():
                status, output = self.run_command(
                    compiler.get_compiler_version(settings, directory))
                if not status:
                    raise ValueError(f'{variant_id}: version probe failed: {output}')
                self.versions_by_variant[variant_id] = output.strip()

    def run_command(self, arguments: list[str], get_stdout: bool = True,
                    cwd: str | None = None) -> tuple[bool, str]:
        result = self.run_process(arguments, get_stdout=get_stdout, cwd=cwd)
        return result.status, result.output

    def run_process(self, arguments: list[str], get_stdout: bool = True,
                    cwd: str | None = None) -> ProcessResult:
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
                           stderr=sp.STDOUT, shell=True, env=sys_env, cwd=cwd)
            stdout, stderr = cmd.communicate()
        except (OSError, sp.CalledProcessError) as err:
            return ProcessResult(None, str(err), str(err))
        finally:
            if is_groovy:
                os.chdir(old_cwd)
        stderr = stderr.decode("utf-8", errors='replace') if stderr else ""
        stdout = stdout.decode("utf-8", errors='replace') if stdout else ""
        err = stdout if get_stdout else stderr
        return ProcessResult(cmd.returncode, err)

    @staticmethod
    def timed[**P, R](fn: Callable[P, R], *args: P.args,
                      **kwargs: P.kwargs) -> tuple[R, float]:
        start = time.perf_counter()
        result = fn(*args, **kwargs)
        return result, time.perf_counter() - start

    def observe(self, compiler: BaseCompiler[S],
                cwd: str | None = None) -> CommandObservation:
        command_args = compiler.get_compiler_cmd()
        process, elapsed = self.timed(
            self.run_process, command_args, cwd=cwd)
        diagnostics, _ = compiler.analyze_compiler_output(process.output)
        parsed = compiler.collect_diagnostics(process.output)
        return CommandObservation(
            process, diagnostics or {}, parsed,
            compiler.comparison_errors(parsed.errors), compiler.crash_msg,
            tuple(command_args), cwd, elapsed)

    def run_variants(self, requests: Sequence[CompilationRequest[S]]
                     ) -> tuple[VariantObservation[S], ...]:
        observations = []
        for request in requests:
            # A blocked attempt still records the configuration it would have run.
            settings = request.compiler.settings
            if request.blocked_reason:
                observations.append(VariantObservation(
                    request.variant_id, None, request.expected_dependency_artifact_path,
                    dependency_artifact_valid=False, incomplete_reason=request.blocked_reason,
                    settings=settings))
                continue
            if request.expected_dependency_artifact_path:
                path = request.expected_dependency_artifact_path
                if os.path.isdir(path):
                    shutil.rmtree(path)
                elif os.path.exists(path):
                    os.remove(path)
            observation = self.observe(request.compiler, cwd=request.working_directory)
            path = request.expected_dependency_artifact_path
            valid = path is None or request.compiler.validate_dependency_artifact(path)
            reason = None
            if observation.status and path is not None and not valid:
                reason = 'Successful compilation did not produce a usable KLIB: ' + path
            observations.append(VariantObservation(
                request.variant_id, observation, path, path if valid else None,
                valid, reason, settings=settings))
        return tuple(observations)

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
        dependency_artifact_filename = provider_compiler.get_dependency_artifact_filename()
        if dependency_artifact_filename is None:
            raise ValueError('Provider compiler has no dependency artifact filename')
        provider_klib = os.path.join(compile_cwd, dependency_artifact_filename)
        if os.path.isdir(provider_klib):
            shutil.rmtree(provider_klib)
        elif os.path.exists(provider_klib):
            os.remove(provider_klib)
        process, compilation_time = self.timed(
            self.run_process, provider_command_args, cwd=compile_cwd)
        if process.launch_failure or process.returncode is None:
            raise ProviderCompilationError(process, compilation_time)
        if process.status:
            return (provider_klib, provider_command_args), compilation_time
        return None, compilation_time