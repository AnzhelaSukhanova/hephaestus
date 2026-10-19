from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import TYPE_CHECKING, Protocol

from src import utils
from src.compilers.results import CompilationRequest
from src.modules.crossmodule import PublicationError
from src.modules.models import (
    ComparisonDependencies, OracleResult, ProgramRes, VariantDependencies,
    mark_inconclusive,
)
from src.oracles.backend_comparison import check_backend_compilation


if TYPE_CHECKING:
    from src.compilers.kotlin import KotlinSettings
    from src.modules.compilation import Compilation
    from src.modules.evaluation import Evaluation
    from src.modules.worker import RunOptions


class OracleMode(Protocol):
    @property
    def name(self) -> str: ...

    def evaluate(self, evaluation: Evaluation, dirname: str,
                 oracles: Mapping[int, ProgramRes]) -> OracleResult: ...

    def counts_inconclusive(self) -> bool: ...

    def compares_variants(self) -> bool: ...


@dataclass(frozen=True)
class CompilationMode:
    name: str = 'compilation'

    def evaluate(self, evaluation: Evaluation, dirname: str,
                 oracles: Mapping[int, ProgramRes]) -> OracleResult:
        return evaluation._check_compilation(dirname, oracles)

    def counts_inconclusive(self) -> bool:
        return True

    def compares_variants(self) -> bool:
        return False

@dataclass(frozen=True)
class BackendComparisonMode:
    name: str = 'backend-comparison'

    def evaluate(self, evaluation: Evaluation, dirname: str,
                 oracles: Mapping[int, ProgramRes]) -> OracleResult:
        return self._evaluate(evaluation, dirname, oracles)

    def counts_inconclusive(self) -> bool:
        return True

    def compares_variants(self) -> bool:
        return True

    def preflight(self, compilation: Compilation[KotlinSettings]) -> None:
        """Check every selected configuration before fuzzing starts.

        Records the configurations' versions, which must agree, and requires
        each configuration to build a usable fixture KLIB.
        """
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'program.kt'
            source.write_text('internal fun oracleFixture(): Int = 1\n')
            requests = []
            for variant_id, settings in compilation.settings_by_variant.items():
                destination = os.path.join(directory, variant_id)
                os.mkdir(destination)
                compiler = compilation.make_compiler(
                    str(source), module_name='fixture',
                    output_directory=destination, klib_output=True,
                    settings=settings)
                problem = compiler.installation_problem()
                if problem is not None:
                    raise ValueError(f'{variant_id}: {problem}')
                requests.append(CompilationRequest(
                    variant_id, compiler, destination,
                    os.path.join(destination, compiler.get_dependency_artifact_filename())))
            compilation.discover_versions()
            version_ids = set()
            for variant_id, version in compilation.versions_by_variant.items():
                match = re.search(r'\b\d+\.\d+(?:\.\d+)?(?:[-+][\w.-]+)?', version)
                if match is None:
                    raise ValueError(f'{variant_id}: cannot identify compiler version: {version}')
                version_ids.add(match[0])
            if len(version_ids) != 1:
                raise ValueError('Kotlin backend compiler versions differ: '
                                 + repr(compilation.versions_by_variant))
            for observation in compilation.run_variants(requests):
                command = observation.command
                if (command is None or not command.succeeded()
                        or not observation.dependency_artifact_valid):
                    raise ValueError(f'{observation.variant_id}: KLIB preflight failed: '
                                     f'{command.output if command else observation.incomplete_reason}')

    def _prepare_requests(
            self, evaluation: Evaluation, dirname: str, pid: int,
            proc_res: ProgramRes
    ) -> tuple[list[CompilationRequest], ComparisonDependencies]:
        manager = evaluation.manager
        if manager is None:
            raise RuntimeError('Cross-module manager is unavailable')
        filename = os.path.abspath(os.path.join(dirname, 'src'))
        aliases = {os.path.abspath(path): os.path.basename(path)
                   for path in proc_res.stats['programs']}
        filters = utils.path2set(evaluation.options.error_filter_patterns)
        closure = proc_res.dep_transitive_closure_pids
        kept, dropped = manager.apply_drop_knob(closure)
        direct = proc_res.direct_dependency_pid
        graph_reason = None
        if (direct is not None and (not closure or closure[0] != direct)
                or direct is None and closure):
            graph_reason = 'Missing or inconsistent logical dependency closure'
        requests = []
        variants: dict[str, VariantDependencies] = {}
        for variant_id, settings in evaluation.compilation.settings_by_variant.items():
            destination = os.path.abspath(os.path.join(dirname, 'variants', variant_id))
            utils.mkdir(destination)
            _, reason = manager.resolve_variant_dependencies(kept, variant_id)
            # The kept closure supplies the libraries; only the direct provider is a friend.
            dependency_paths = manager.dependency_klibs(kept, variant_id)
            friend_paths = manager.dependency_klibs(closure[:1], variant_id)
            variants[variant_id] = VariantDependencies(
                tuple(dependency_paths), tuple(friend_paths))
            dependency_aliases = {
                path: f'module:d{item}'
                for item, path in zip(
                    closure, manager.dependency_klibs(closure, variant_id))}
            compiler = evaluation.compilation.make_compiler(
                filename, filters, module_name='d' + str(pid),
                output_directory=destination, klib_output=True, source_aliases=aliases,
                dependency_klibs=dependency_paths, friend_klibs=friend_paths,
                dependency_aliases=dependency_aliases, settings=settings)
            requests.append(CompilationRequest(
                variant_id, compiler, destination,
                os.path.join(destination, compiler.get_dependency_artifact_filename()),
                graph_reason or reason, path_aliases=aliases))
        dependencies = ComparisonDependencies(
            direct, closure, tuple(kept), tuple(dropped), variants)
        proc_res.stats['comparison_dependencies'] = dependencies
        return requests, dependencies

    def _evaluate(self, evaluation: Evaluation, dirname: str,
                  oracles: Mapping[int, ProgramRes]) -> OracleResult:
        pid, = oracles
        proc_res = oracles[pid]
        if proc_res.failed:
            proc_res.stats['comparison'] = {
                'findings': [], 'incomplete_reasons': ['Generation failed']}
            stats = mark_inconclusive(
                proc_res.stats, self.name, 'generation_failed',
                str(proc_res.stats.get('error') or
                    'Generation did not produce a usable program'))
            with evaluation._secondary_operation(pid, 'preserve_partial_source', stats):
                evaluation.artifacts.preserve_partial_source(dirname, pid)
            with evaluation._secondary_operation(pid, 'preserve_program_artifacts', stats):
                evaluation.artifacts.preserve_final_program_dir(pid)
            with evaluation._secondary_operation(pid, 'cleanup_program_artifacts', stats):
                evaluation.artifacts._cleanup_program_tmp(pid)
            shutil.rmtree(dirname, ignore_errors=True)
            return OracleResult(inconclusive={pid: stats})

        manager = evaluation.manager
        if manager is None:
            raise RuntimeError('Cross-module manager is unavailable')
        requests, dependencies = self._prepare_requests(evaluation, dirname, pid, proc_res)
        variants = evaluation.compilation.run_variants(requests)
        comparison = check_backend_compilation(
            {variant.variant_id: variant for variant in variants},
            tuple(evaluation.compilation.settings_by_variant))
        publications = {}
        if comparison.all_success and manager.enabled():
            try:
                publications[pid] = manager.publish_comparison_provider(
                    pid, {variant.variant_id: variant for variant in variants},
                    dependencies.direct, dependencies.closure, dependencies.dropped)
            except (OSError, ValueError, PublicationError) as exc:
                comparison = replace(comparison, incomplete_reasons=(
                    'Provider publication failed: ' + str(exc),))
        elapsed = sum(variant.command.elapsed for variant in variants if variant.command)
        timings = evaluation.reporting.get_time_metrics(oracles, elapsed)
        if evaluation.options.time_metrics:
            timings[pid]['variants'] = {
                variant.variant_id: variant.command.elapsed if variant.command else None
                for variant in variants}
        proc_res.stats['oracle'] = self.name
        proc_res.stats['comparison'] = asdict(comparison)
        evaluation.reporting.attach_time_metrics(pid, proc_res.stats, timings)
        faults = {}
        inconclusive = {}
        if comparison.findings:
            proc_res.stats['error'] = '\n'.join(
                message for finding in comparison.findings for message in finding.diagnostics)
            faults[pid] = proc_res.stats
        elif not comparison.complete:
            reason = ('backend_comparison_incomplete'
                      if comparison.incomplete_reasons else 'backend_comparison_unresolved')
            inconclusive[pid] = mark_inconclusive(
                proc_res.stats, self.name, reason,
                '; '.join(comparison.incomplete_reasons) or
                'Backend comparison could not establish a verdict')
        record = faults.get(pid) or inconclusive.get(pid)
        if faults or inconclusive or evaluation.options.keep_everything:
            with evaluation._secondary_operation(pid, 'preserve_comparison', record):
                evaluation.artifacts.preserve_comparison(
                    pid, requests, variants, proc_res.stats['comparison'],
                    dependencies, manager, evaluation.compilation.versions_by_variant)
            with evaluation._secondary_operation(pid, 'preserve_program_directory', record):
                evaluation.artifacts.preserve_final_program_dir(pid)
        with evaluation._secondary_operation(pid, 'cleanup_program_artifacts', record):
            evaluation.artifacts._cleanup_program_tmp(pid)
        shutil.rmtree(dirname, ignore_errors=True)
        return OracleResult(faults, elapsed, timings, publications, inconclusive)


def create_oracle_mode(options: RunOptions) -> OracleMode:
    if options.oracle == 'compilation':
        return CompilationMode()
    if options.oracle == 'backend-comparison':
        return BackendComparisonMode()
    raise ValueError(f'Unsupported oracle mode: {options.oracle}')
