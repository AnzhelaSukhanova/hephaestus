import json
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import asdict

from src import utils
from src.compilers.kotlin import KotlinSettings
from src.compilers.results import CompilationRequest, VariantObservation
from src.modules.crossmodule import CrossModuleManager
from src.modules.worker import RunOptions
from src.modules.models import ComparisonDependencies
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

    def preserve_partial_source(self, dirname: str, pid: int) -> None:
        src_dir = os.path.join(dirname, 'src', 'd' + str(pid))
        if os.path.isdir(src_dir):
            dst_dir = os.path.join(self.options.test_directory, str(pid),
                                   'partial_source')
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)

    def preserve_comparison(
            self, pid: int, requests: Sequence[CompilationRequest[KotlinSettings]],
            observations: Sequence[VariantObservation[KotlinSettings]], comparison: dict,
            dependencies: ComparisonDependencies,
            manager: CrossModuleManager, versions_by_variant: Mapping[str, str]) -> None:
        target = os.path.join(self.options.test_directory, 'tmp', str(pid))
        utils.mkdir(target)
        source = os.path.join(target, 'program.kt')
        case_directory = os.path.abspath(
            os.path.join(self.options.test_directory, str(pid)))
        available, published_paths = self._published_dependencies(
            case_directory, dependencies, requests, manager)
        for request, variant in zip(requests, observations):
            self._variant_record(pid, target, request, variant,
                                 published_paths, manager, versions_by_variant)
        with open(os.path.join(target, 'comparison.json'), 'w') as out:
            json.dump({'source': os.path.basename(source), 'comparison': comparison,
                       'dependencies': asdict(dependencies),
                       'available_dependencies': available,
                       'variant_elapsed': {variant.variant_id: (
                           variant.command.elapsed if variant.command else None)
                           for variant in observations},
                       'compiler_versions': versions_by_variant}, out, indent=2)

    @staticmethod
    def _published_dependencies(
            case_directory: str, dependencies: ComparisonDependencies,
            requests: Sequence[CompilationRequest], manager: CrossModuleManager
    ) -> tuple[dict[str, dict[str, bool]], dict[str, str]]:
        """Retained cases reference the run's published KLIBs in place.

        Each published path maps to its location relative to the retained case
        directory, so reproductions work while the case stays in its run.
        """
        available: dict[str, dict[str, bool]] = {}
        published_paths: dict[str, str] = {}
        for request in requests:
            klibs = manager.dependency_klibs(dependencies.closure, request.variant_id)
            for ancestor, klib in zip(dependencies.closure, klibs):
                available.setdefault(str(ancestor), {})[request.variant_id] = (
                    os.path.exists(klib))
                published_paths[klib] = os.path.relpath(klib, case_directory)
        return available, published_paths

    def _reproduction_argv(
            self, arguments: Sequence[str], target: str, variant_id: str,
            published_paths: Mapping[str, str], path_aliases: Mapping[str, str],
            output_directory: str | None, module_name: str) -> list[str]:
        relative_output = os.path.join('reproduce', variant_id)
        utils.mkdir(os.path.join(target, relative_output))
        replacements = dict(published_paths)
        replacements.update({path: 'program.kt' for path in path_aliases})
        if output_directory is not None:
            replacements[os.path.join(output_directory, module_name)] = os.path.join(
                relative_output, module_name)
            replacements[output_directory] = relative_output
        reproduce = []
        for argument in arguments:
            argument = os.path.expanduser(os.path.expandvars(argument))
            for original, retained in sorted(replacements.items(),
                                             key=lambda item: -len(item[0])):
                argument = argument.replace(original, retained)
            reproduce.append(argument)
        return reproduce

    def _variant_record(
            self, pid: int, target: str, request: CompilationRequest[KotlinSettings],
            variant: VariantObservation[KotlinSettings], published_paths: Mapping[str, str],
            manager: CrossModuleManager, versions_by_variant: Mapping[str, str]) -> None:
        variant_id = request.variant_id
        settings = variant.settings
        artifact = manager.variant_publication(pid, variant_id)
        directory = os.path.join(target, 'variants', variant_id)
        utils.mkdir(directory)
        command = variant.command
        arguments = command.command if command else request.compiler.get_compiler_cmd()
        reproduce = self._reproduction_argv(
            arguments, target, variant_id, published_paths, request.path_aliases,
            request.working_directory, artifact['module_name'])
        result = {
            'variant_id': variant_id, 'backend': settings.backend,
            'settings': asdict(settings),
            'compiler_version': versions_by_variant.get(variant_id),
            'source_identity': 'program.kt',
            'original_command': list(arguments),
            'original_cwd': command.cwd if command else request.working_directory,
            'reproduce_argv': reproduce,
            'reproduce_cwd': '.',
            'status': command.status if command else None,
            'returncode': command.returncode if command else None,
            'launch_failure': command.launch_failure if command else None,
            'crash': command.crash_message if command else None,
            'elapsed': command.elapsed if command else None,
            'diagnostics': [asdict(error) for error in command.errors] if command else [],
            'filtered_diagnostics': [asdict(error) for error in
                                     (command.filtered_errors or ())] if command else [],
            'parsing_complete': command.parsing_complete if command else None,
            'unparsed': command.unparsed if command else (),
            'artifact_valid': variant.dependency_artifact_valid,
            'incomplete_reason': variant.incomplete_reason,
            'expected_klib': request.expected_dependency_artifact_path,
        }
        with open(os.path.join(directory, 'result.json'), 'w') as out:
            json.dump(result, out, indent=2)
        with open(os.path.join(directory, 'output.txt'), 'w') as out:
            out.write(str(command.output) if command else variant.incomplete_reason or '')
        dependency_artifact = variant.produced_dependency_artifact_path
        if dependency_artifact and os.path.exists(dependency_artifact):
            destination = os.path.join(directory, os.path.basename(dependency_artifact))
            if os.path.isdir(dependency_artifact):
                shutil.copytree(dependency_artifact, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(dependency_artifact, destination)

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