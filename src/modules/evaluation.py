from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
import os
import shutil
import sys
import traceback

from src import utils
from src.modules.artifacts import Artifacts
from src.modules.compilation import ORDINARY_VARIANT, Compilation, ProviderCompilationError
from src.modules.coverage import LiveCoverage
from src.modules.models import ProgramRes, OracleResult, mark_inconclusive
from src.modules.worker_reporting import TimingMetrics
from src.modules.worker import RunOptions
from src.generators.config import cfg
from src.modules.crossmodule import CrossModuleManager, PublicationError
from src.oracles.compilation import check_compilation
from src.compilers.results import CompilationRequest, CommandObservation
from src.modules.oracle_modes import OracleMode, create_oracle_mode


class Evaluation[S]:
    def __init__(
            self, options: RunOptions, compilation: Compilation[S],
            artifacts: Artifacts, coverage: LiveCoverage[S], reporting: TimingMetrics,
            manager: CrossModuleManager | None = None, *,
            mode: OracleMode | None = None) -> None:
        self.options = options
        self.compilation = compilation
        self.artifacts = artifacts
        self.coverage = coverage
        self.reporting = reporting
        self.manager = manager
        self.mode = mode or create_oracle_mode(options)

    def check_oracle(self, dirname: str,
                     oracles: Mapping[int, ProgramRes]) -> OracleResult:
        """
        This function is responsible for checking the oracle of the generated
        programs.

        It gets a dict of oracles, and a directory that includes a batch of
        program.

        It first invokes the compiler to compile all the programs included in the
        given directory. Then, based on the given oracles, it decides whether
        the compiler produced the expected output for every program.

        It returns a dictionary containing the programs where the compiler did not
        produce the expected results (and the reason why).
        """
        return self.mode.evaluate(self, dirname, oracles)

    def _error_details(self, error: Exception) -> str:
        if self.options.print_stacktrace:
            return ''.join(traceback.format_exception(error))
        return str(error)

    def _record_secondary_error(self, stats: dict, operation: str,
                                error: Exception) -> None:
        stats.setdefault('secondary_errors', []).append({
            'operation': operation, 'error': self._error_details(error)})

    @contextmanager
    def _secondary_operation(self, pid: int, operation: str,
                             stats: dict | None = None) -> Iterator[None]:
        try:
            yield
        except Exception as exc:
            if stats is not None:
                self._record_secondary_error(stats, operation, exc)
            else:
                self._warn_secondary_errors(pid, [(operation, exc)])

    def _generation_failures(
            self, dirname: str, oracles: Mapping[int, ProgramRes]
    ) -> dict[int, dict]:
        inconclusive = {}
        for pid, proc_res in oracles.items():
            if not proc_res.failed:
                continue
            original_error = proc_res.stats.get('error')
            details = (str(original_error) if original_error is not None
                       else 'Generation did not produce a usable program')
            stats = mark_inconclusive(
                proc_res.stats, self.mode.name, 'generation_failed', details)
            inconclusive[pid] = stats
            with self._secondary_operation(pid, 'preserve_partial_source', stats):
                self.artifacts.preserve_partial_source(dirname, pid)
            with self._secondary_operation(pid, 'preserve_generation_artifacts', stats):
                self.artifacts.preserve_final_program_dir(pid)
            with self._secondary_operation(pid, 'cleanup_generation_artifacts', stats):
                self.artifacts._cleanup_program_tmp(pid)
        return inconclusive

    def _exclude_failed_sources(self, dirname: str,
                                oracles: Mapping[int, ProgramRes]) -> None:
        for pid, proc_res in oracles.items():
            if proc_res.failed:
                shutil.rmtree(os.path.join(dirname, 'src', 'd' + str(pid)),
                              ignore_errors=True)

    def _preserve_unresolved(self, dirname: str,
                             oracles: Mapping[int, ProgramRes],
                             reason: str, details: str,
                             evidence: Mapping[str, object] | None = None
                             ) -> OracleResult:
        inconclusive = self._generation_failures(dirname, oracles)
        for pid, proc_res in oracles.items():
            if proc_res.failed:
                continue
            stats = mark_inconclusive(
                proc_res.stats, self.mode.name, reason, details,
                **(evidence if evidence else {}))
            inconclusive[pid] = stats
            self._preserve_inconclusive_case(dirname, pid, stats)
        shutil.rmtree(dirname, ignore_errors=True)
        return OracleResult(inconclusive=inconclusive)

    def _preserve_inconclusive_case(self, dirname: str, pid: int,
                                    stats: dict) -> None:
        with self._secondary_operation(pid, 'preserve_ir_changes', stats):
            self.artifacts.preserve_ir_changes_to_tmp(dirname, pid)
        with self._secondary_operation(pid, 'preserve_partial_source', stats):
            self.artifacts.preserve_partial_source(dirname, pid)
        with self._secondary_operation(pid, 'preserve_program_artifacts', stats):
            self.artifacts.preserve_final_program_dir(pid)
        with self._secondary_operation(pid, 'cleanup_program_artifacts', stats):
            self.artifacts._cleanup_program_tmp(pid)

    def check_oracle_safely(self, dirname: str,
                            oracles: Mapping[int, ProgramRes]) -> OracleResult:
        try:
            return self.check_oracle(dirname, oracles)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            details = self._error_details(exc)
            print('Internal error while checking the oracle')
            print(details)
            return self._preserve_unresolved(
                dirname, oracles, 'evaluation_error',
                'Evaluation could not be completed: ' + details)

    def skipped_compilation_result(
            self, dirname: str, oracles: Mapping[int, ProgramRes]
    ) -> OracleResult:
        """Dry-run counts healthy generation as passed; compilation is omitted."""
        inconclusive = self._generation_failures(dirname, oracles)
        shutil.rmtree(dirname, ignore_errors=True)
        return OracleResult(inconclusive=inconclusive)

    def _check_compilation(self, dirname: str,
                           oracles: Mapping[int, ProgramRes]) -> OracleResult:
        inconclusive = self._generation_failures(dirname, oracles)
        healthy = {pid: result for pid, result in oracles.items()
                   if not result.failed}
        if not healthy:
            shutil.rmtree(dirname, ignore_errors=True)
            return OracleResult(inconclusive=inconclusive)
        self._exclude_failed_sources(dirname, oracles)
        request, dependency_paths, friend_paths, module_pid = \
            self._prepare_compilation(dirname, healthy)
        variant, = self.compilation.run_variants((request,))
        if variant.command is None:
            details = variant.incomplete_reason or 'No compiler process observation was returned'
            return self._preserve_unresolved(
                dirname, oracles, 'compilation_observation_missing', details,
                {'variant': variant.variant_id,
                 'incomplete_reason': variant.incomplete_reason})
        try:
            return self._finalize_compilation(
                dirname, oracles, request, variant.command,
                dependency_paths, friend_paths, module_pid, inconclusive)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return self._preserve_unresolved(
                dirname, oracles, 'evaluation_error',
                'Compilation ran but its result could not be finalized: ' +
                self._error_details(exc),
                {'compilation': self._process_evidence(variant.command)})

    def _prepare_compilation(self, dirname: str,
                             oracles: Mapping[int, ProgramRes]):
        filename = os.path.join(dirname, 'src')
        filter_patterns = utils.path2set(self.options.error_filter_patterns)
        dependency_paths = []
        friend_paths = []
        for pid, proc_res in oracles.items():
            closure = proc_res.dep_transitive_closure_pids
            if not closure:
                continue
            manager = self.manager
            if manager is None:
                raise RuntimeError('Cross-module manager is unavailable')
            kept, dropped = manager.apply_drop_knob(closure)
            if dropped:
                # Only a fault's stats reach faults.json, so annotating here
                # attributes exactly the faults that were compiled this way.
                proc_res.stats['dropped_indirect_deps'] = dropped
            klibs = manager.dependency_klibs(kept)
            friend_klibs = manager.dependency_klibs(closure[:1])
            for path in klibs:
                if path not in dependency_paths:
                    dependency_paths.append(path)
            # The direct dependency comes first, and it is the only friend.
            for path in friend_klibs:
                if path not in friend_paths:
                    friend_paths.append(path)

        # Cross-module generation uses one program per invocation, so that every
        # program is a module of its own with its own retained KLIB.
        module_pid = None
        if cfg.prob.crossmodule_probability:
            module_pid, = oracles
        module_name = None
        compile_cwd = None
        if module_pid is not None:
            module_name = 'd' + str(module_pid)
            compile_cwd = os.path.join(filename, module_name)
            utils.mkdir(compile_cwd)

        if self.options.language == 'kotlin' and (dependency_paths or module_name):
            compiler = self.compilation.make_compiler(
                filename, filter_patterns, dependency_klibs=dependency_paths,
                friend_klibs=friend_paths, module_name=module_name)
        else:
            compiler = self.compilation.make_compiler(filename, filter_patterns)

        return (CompilationRequest(ORDINARY_VARIANT, compiler, compile_cwd),
                dependency_paths, friend_paths, module_pid)

    def _preserve_case(self, dirname: str, pid: int) -> list[tuple[str, Exception]]:
        errors = []
        phase_changes = None
        try:
            phase_changes = self.artifacts.preserve_ir_changes_to_tmp(dirname, pid)
        except Exception as exc:
            errors.append(('preserve_ir_changes', exc))
        try:
            self.artifacts.preserve_final_program_dir(pid)
        except Exception as exc:
            errors.append(('preserve_program_directory', exc))
        try:
            self.coverage.maybe_run_live_jacoco(pid, phase_changes)
        except Exception as exc:
            errors.append(('live_coverage', exc))
        return errors

    def _add_secondary_errors(self, stats: dict,
                              errors: list[tuple[str, Exception]]) -> None:
        for operation, error in errors:
            self._record_secondary_error(stats, operation, error)

    def _warn_secondary_errors(self, pid: int,
                                errors: list[tuple[str, Exception]]) -> None:
        for operation, error in errors:
            print('Secondary operation {} failed for program {}: {}'.format(
                operation, pid, self._error_details(error)))

    @staticmethod
    def _attribute_diagnostics(
            oracles: Mapping[int, ProgramRes],
            diagnostics: Mapping[str, Sequence[str]]) -> dict[str, Sequence[str]]:
        programs = [program for result in oracles.values()
                    for program in result.stats.get('programs', {})]
        attributed = {}
        for diagnostic_path, messages in diagnostics.items():
            observed = os.path.normcase(os.path.abspath(diagnostic_path))
            candidates = []
            for program in programs:
                normalized = os.path.normcase(os.path.abspath(program))
                relative = normalized.split(os.sep + 'src' + os.sep, 1)[-1]
                if (observed == normalized or observed.endswith(os.sep + normalized)
                        or normalized.endswith(os.sep + observed)
                        or observed.endswith(os.sep + relative) or observed == relative):
                    candidates.append(program)
            if not candidates:
                candidates = [program for program in programs
                              if os.path.basename(program) ==
                              os.path.basename(diagnostic_path)]
            attributed[candidates[0] if len(candidates) == 1 else diagnostic_path] = messages
        return attributed

    def _unavailable_reason(
            self, observation: CommandObservation,
            oracles: Mapping[int, ProgramRes],
            diagnostics: Mapping[str, Sequence[str]]
    ) -> tuple[str, str] | None:
        if observation.launch_failure is not None or observation.returncode is None:
            return ('compiler_launch_failed',
                    observation.launch_failure or 'Compiler process did not start')
        if observation.returncode != 0 and not any(
                program in diagnostics
                for result in oracles.values()
                for program in result.stats.get('programs', {})):
            return ('unattributable_compiler_failure',
                    'Compiler exited with status {} without diagnostics attributable '
                    'to the batch programs'.format(observation.returncode))
        return None

    @staticmethod
    def _process_evidence(observation: CommandObservation) -> dict[str, object]:
        return {
            'command': list(observation.command),
            'cwd': observation.cwd,
            'returncode': observation.returncode,
            'launch_failure': observation.launch_failure,
            'elapsed': observation.elapsed,
            'output': observation.output,
        }

    def _finalize_compilation(
            self, dirname: str, oracles: Mapping[int, ProgramRes],
            request: CompilationRequest[S], observation: CommandObservation,
            dependency_paths: list[str], friend_paths: list[str],
            module_pid: int | None,
            inconclusive: dict[int, dict] | None = None) -> OracleResult:
        inconclusive = inconclusive or {}
        healthy = {pid: result for pid, result in oracles.items()
                   if not result.failed}
        compiler = request.compiler
        compile_cwd = request.working_directory
        module_name = 'd' + str(module_pid) if module_pid is not None else None
        compilation_time = observation.elapsed
        diagnostics = self._attribute_diagnostics(healthy, observation.diagnostics)
        time_metrics_error = None
        try:
            time_metrics = self.reporting.get_time_metrics(oracles, compilation_time)
        except Exception as exc:
            time_metrics = {}
            time_metrics_error = exc
            for stats in inconclusive.values():
                self._record_secondary_error(stats, 'collect_time_metrics', exc)
        crash_findings = check_compilation(
            {}, diagnostics, observation.crash_message)
        if crash_findings:
            # We just found a compiler crash.
            crash_finding, = crash_findings
            output = {}
            if self.options.debug:
                print('We found compiler crash')
            for pid, proc_res in healthy.items():
                proc_res.stats['error'] = crash_finding.diagnostics[0]
                output[pid] = proc_res.stats
                if time_metrics_error is not None:
                    self._record_secondary_error(
                        proc_res.stats, 'collect_time_metrics', time_metrics_error)
                with self._secondary_operation(pid, 'attach_time_metrics', proc_res.stats):
                    self.reporting.attach_time_metrics(pid, proc_res.stats, time_metrics)
                self._add_secondary_errors(
                    proc_res.stats, self._preserve_case(dirname, pid))
                if proc_res.direct_dependency_pid is not None:
                    with self._secondary_operation(
                            pid, 'cleanup_program_artifacts', proc_res.stats):
                        self.artifacts._cleanup_program_tmp(pid)
            shutil.rmtree(dirname, ignore_errors=True)
            return OracleResult(output, compilation_time, time_metrics,
                                inconclusive=inconclusive)

        unavailable = self._unavailable_reason(observation, healthy, diagnostics)
        if unavailable is not None:
            reason, details = unavailable
            unresolved = dict(inconclusive)
            for pid, proc_res in healthy.items():
                stats = mark_inconclusive(
                    proc_res.stats, self.mode.name, reason, details,
                    compilation=self._process_evidence(observation))
                if time_metrics_error is not None:
                    self._record_secondary_error(
                        stats, 'collect_time_metrics', time_metrics_error)
                unresolved[pid] = stats
                self._preserve_inconclusive_case(dirname, pid, stats)
            shutil.rmtree(dirname, ignore_errors=True)
            return OracleResult(compilation_time=compilation_time,
                                time_metrics=time_metrics,
                                inconclusive=unresolved)

        output = {}
        published = {}
        for pid, proc_res in healthy.items():
            dependency = proc_res.direct_dependency_pid
            already_preserved = False
            findings = {
                finding.program: finding
                for finding in check_compilation(
                    proc_res.stats['programs'], diagnostics, None)
            }
            for program, oracle in proc_res.stats['programs'].items():
                finding = findings.get(program)
                if finding is None:
                    continue
                if finding.kind == 'unexpected_rejection':
                    # Here the program should be compiled successfully. However,
                    # it's in the list of the error messages.
                    proc_res.stats['error'] = '\n'.join(finding.diagnostics)
                    output[pid] = proc_res.stats
                    if time_metrics_error is not None:
                        self._record_secondary_error(
                            proc_res.stats, 'collect_time_metrics', time_metrics_error)
                    if not self.options.disable_metrics:
                        with self._secondary_operation(
                                pid, 'diagnostic_enrichment', proc_res.stats):
                            enrichment, phase_profiling = self.compilation.timed(
                                self.compilation.enrich_error, compiler,
                                err_file=program, cwd=compile_cwd)
                            proc_res.stats.update(enrichment)
                            if self.options.time_metrics and pid in time_metrics:
                                time_metrics[pid]["phase_profiling"] = phase_profiling
                    with self._secondary_operation(pid, 'attach_time_metrics', proc_res.stats):
                        self.reporting.attach_time_metrics(pid, proc_res.stats, time_metrics)
                    stop = False
                    if self.options.debug:
                        msg = 'Mismatch found in program {}. Expected to compile'
                        print(msg.format(pid))
                        stop = True
                    if self.options.rerun:
                        with self._secondary_operation(
                                pid, 'report_failed_program', proc_res.stats):
                            self.compilation._report_failed(
                                pid, self.options.transformations, compiler, oracle)
                    already_preserved = True
                    self._add_secondary_errors(
                        proc_res.stats, self._preserve_case(dirname, pid))
                    if stop:
                        print(proc_res.stats['error'])
                        sys.exit(1)
                if finding.kind == 'unexpected_acceptance':
                    # Here, we have a case where we expected that the compiler
                    # would not be able to compile the program. However,
                    # the compiler managed to compile it successfully.
                    proc_res.stats['error'] = 'SHOULD NOT BE COMPILED: ' + str(
                        proc_res.stats.get('error') or '')
                    output[pid] = proc_res.stats
                    if time_metrics_error is not None:
                        self._record_secondary_error(
                            proc_res.stats, 'collect_time_metrics', time_metrics_error)
                    with self._secondary_operation(pid, 'attach_time_metrics', proc_res.stats):
                        self.reporting.attach_time_metrics(pid, proc_res.stats, time_metrics)
                    if self.options.debug:
                        msg = 'Mismatch found in program {}. Expected to fail'
                        print(msg.format(pid))
                    if self.options.rerun:
                        with self._secondary_operation(
                                pid, 'report_failed_program', proc_res.stats):
                            self.compilation._report_failed(
                                pid, self.options.transformations, compiler, oracle)
                    already_preserved = True
                    self._add_secondary_errors(
                        proc_res.stats, self._preserve_case(dirname, pid))
            # For now only --batch 1 supported for crossmodule
            if cfg.prob.crossmodule_probability:
                assert module_pid == pid

            if cfg.prob.crossmodule_probability and pid not in output:
                try:
                    provider, provider_compilation_time = \
                        self.compilation._build_crossmodule_provider(
                            proc_res, utils.path2set(self.options.error_filter_patterns),
                            dependency_paths, friend_paths,
                            module_name, compile_cwd)
                except Exception as exc:
                    process_elapsed = (
                        exc.elapsed if isinstance(exc, ProviderCompilationError) else 0.0)
                    provider_evidence = {}
                    if isinstance(exc, ProviderCompilationError):
                        provider_evidence['provider_process'] = {
                            'returncode': exc.process.returncode,
                            'launch_failure': exc.process.launch_failure,
                            'output': exc.process.output,
                            'elapsed': exc.elapsed,
                        }
                    inconclusive[pid] = mark_inconclusive(
                        proc_res.stats, self.mode.name, 'provider_publication_failed',
                        'Primary compilation was usable, but provider construction '
                        'failed: ' + self._error_details(exc),
                        primary_verdict='pass', **provider_evidence)
                    with self._secondary_operation(
                            pid, 'preserve_partial_source', inconclusive[pid]):
                        self.artifacts.preserve_partial_source(dirname, pid)
                    self._add_secondary_errors(
                        inconclusive[pid], self._preserve_case(dirname, pid))
                    provider = None
                    provider_compilation_time = process_elapsed
                compilation_time += provider_compilation_time
                if self.options.time_metrics and pid in time_metrics:
                    time_metrics[pid]["compilation_with_ir_dumps"] += \
                        provider_compilation_time
                provider_phase_changes = None
                if provider is not None and self.options.keep_everything:
                    with self._secondary_operation(pid, 'preserve_ir_changes'):
                        provider_phase_changes = self.artifacts.preserve_ir_changes_to_tmp(
                            dirname, pid)
                # Artifacts land before the ledger advertises the program: the
                # KLIB and the pickle are on disk by the time the parent merges
                # the record returned below.
                record = None
                if provider is not None:
                    provider_klib, provider_command_args = provider
                    try:
                        record = self.manager.publish_provider(
                            pid,
                            provider_klib,
                            provider_command_args,
                            dependency,
                            proc_res.stats.get('dropped_indirect_deps'))
                    except (OSError, ValueError, PublicationError) as exc:
                        inconclusive[pid] = mark_inconclusive(
                            proc_res.stats, self.mode.name,
                            'provider_publication_failed',
                            'Primary compilation was usable, but provider publication '
                            'failed: ' + self._error_details(exc),
                            primary_verdict='pass', provider_klib=provider_klib,
                            provider_command=provider_command_args)
                        with self._secondary_operation(
                                pid, 'preserve_partial_source', inconclusive[pid]):
                            self.artifacts.preserve_partial_source(dirname, pid)
                        self._add_secondary_errors(
                            inconclusive[pid], self._preserve_case(dirname, pid))
                already_preserved = record is not None
                if record is not None:
                    published[pid] = record
                if already_preserved and self.options.keep_everything:
                    with self._secondary_operation(pid, 'live_coverage'):
                        self.coverage.maybe_run_live_jacoco(pid, provider_phase_changes)
            if (self.options.keep_everything and not already_preserved and
                    pid not in inconclusive):
                self._warn_secondary_errors(
                    pid, self._preserve_case(dirname, pid))
            with self._secondary_operation(
                    pid, 'cleanup_program_artifacts',
                    output.get(pid, inconclusive.get(pid))):
                self.artifacts._cleanup_program_tmp(pid)
        # Clear the directory of programs.
        shutil.rmtree(dirname, ignore_errors=True)
        if time_metrics_error is not None:
            for pid in healthy.keys() - output.keys() - inconclusive.keys():
                self._warn_secondary_errors(
                    pid, [('collect_time_metrics', time_metrics_error)])
        return OracleResult(output, compilation_time, time_metrics, published, inconclusive)