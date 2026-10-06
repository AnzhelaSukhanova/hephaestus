from collections.abc import Mapping
import os
import shutil
import sys

from src import utils
from src.modules.artifacts import Artifacts
from src.modules.compilation import Compilation
from src.modules.coverage import LiveCoverage
from src.modules.models import ProgramRes, OracleResult
from src.modules.worker_reporting import TimingMetrics
from src.modules.worker import RunOptions
from src.generators.config import cfg
from src.modules.crossmodule import CrossModuleManager
from src.oracles.compilation import check_compilation


class Evaluation[S]:
    def __init__(
            self, options: RunOptions, compilation: Compilation[S],
            artifacts: Artifacts, coverage: LiveCoverage[S], reporting: TimingMetrics,
            manager: CrossModuleManager | None = None) -> None:
        self.options = options
        self.compilation = compilation
        self.artifacts = artifacts
        self.coverage = coverage
        self.reporting = reporting
        self.manager = manager

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
        filename = os.path.join(dirname, 'src')
        filter_patterns = utils.path2set(self.options.error_filter_patterns)
        dependency_paths = []
        friend_paths = []
        for pid, proc_res in oracles.items():
            # A per-node build runs from its own directory, so every library it
            # is handed has to be addressed absolutely.
            klibs = [os.path.abspath(klib)
                     for klib in self.manager.dependency_klibs(
                         proc_res.dep_transitive_closure_klibs)]
            if not klibs:
                continue
            kept, dropped = self.manager.apply_drop_knob(klibs)
            if dropped:
                # Only a fault's stats reach faults.json, so annotating here
                # attributes exactly the faults that were compiled this way.
                proc_res.stats['dropped_indirect_deps'] = dropped
            for path in kept:
                if path not in dependency_paths:
                    dependency_paths.append(path)
            # The direct dependency comes first, and it is the only friend.
            if klibs[0] not in friend_paths:
                friend_paths.append(klibs[0])

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

        observation = self.compilation.observe(compiler, cwd=compile_cwd)
        compilation_time = observation.elapsed
        time_metrics = self.reporting.get_time_metrics(oracles, compilation_time)
        # TODO In case there is an error in the compiler output and none of the
        # programs match with regex to that error, it means that something bad
        # happened. For example, heap space error. In that case, we should log a
        # message in stdout and in Reporting.stats.

        crash_findings = check_compilation(
            {}, observation.diagnostics, observation.crash_message)
        if crash_findings:
            # We just found a compiler crash.
            crash_finding, = crash_findings
            output = {}
            if self.options.debug:
                print('We found compiler crash')
            for pid, proc_res in oracles.items():
                if not proc_res.failed:
                    dependency = proc_res.direct_dependency_pid
                    phase_changes = self.artifacts.preserve_ir_changes_to_tmp(
                        dirname, pid)
                    self.artifacts.preserve_final_program_dir(pid)
                    self.coverage.maybe_run_live_jacoco(pid, phase_changes)
                    proc_res.stats['error'] = crash_finding.diagnostics[0]
                    self.reporting.attach_time_metrics(pid, proc_res.stats, time_metrics)
                    output[pid] = proc_res.stats
                    if dependency is not None:
                        self.artifacts._cleanup_program_tmp(pid)
            shutil.rmtree(dirname)
            return output, compilation_time, time_metrics, {}

        output = {}
        published = {}
        for pid, proc_res in oracles.items():
            if proc_res.failed:
                continue
            dependency = proc_res.direct_dependency_pid
            already_preserved = False
            findings = {
                finding.program: finding
                for finding in check_compilation(
                    proc_res.stats['programs'], observation.diagnostics, None)
            }
            for program, oracle in proc_res.stats['programs'].items():
                finding = findings.get(program)
                if finding is None:
                    continue
                if finding.kind == 'unexpected_rejection':
                    # Here the program should be compiled successfully. However,
                    # it's in the list of the error messages.
                    proc_res.stats['error'] = '\n'.join(finding.diagnostics)
                    if not self.options.disable_metrics:
                        enrichment, phase_profiling = self.compilation.timed(
                            self.compilation.enrich_error, compiler,
                            err_file=program, cwd=compile_cwd)
                        proc_res.stats.update(enrichment)
                        if self.options.time_metrics:
                            time_metrics[pid]["phase_profiling"] = phase_profiling
                    self.reporting.attach_time_metrics(pid, proc_res.stats, time_metrics)
                    output[pid] = proc_res.stats
                    stop = False
                    if self.options.debug:
                        msg = 'Mismatch found in program {}. Expected to compile'
                        print(msg.format(pid))
                        stop = True
                    if self.options.rerun:
                        self.compilation._report_failed(
                            pid, self.options.transformations, compiler, oracle)
                    phase_changes = self.artifacts.preserve_ir_changes_to_tmp(
                        dirname, pid)
                    already_preserved = True
                    self.artifacts.preserve_final_program_dir(pid)
                    self.coverage.maybe_run_live_jacoco(pid, phase_changes)
                    if stop:
                        print(proc_res.stats['error'])
                        sys.exit(1)
                if finding.kind == 'unexpected_acceptance':
                    # Here, we have a case where we expected that the compiler
                    # would not be able to compile the program. However,
                    # the compiler managed to compile it successfully.
                    proc_res.stats['error'] = 'SHOULD NOT BE COMPILED: ' + \
                        proc_res.stats['error']
                    self.reporting.attach_time_metrics(pid, proc_res.stats, time_metrics)
                    output[pid] = proc_res.stats
                    if self.options.debug:
                        msg = 'Mismatch found in program {}. Expected to fail'
                        print(msg.format(pid))
                    if self.options.rerun:
                        self.compilation._report_failed(
                            pid, self.options.transformations, compiler, oracle)
                    phase_changes = self.artifacts.preserve_ir_changes_to_tmp(
                        dirname, pid)
                    already_preserved = True
                    self.artifacts.preserve_final_program_dir(pid)
                    self.coverage.maybe_run_live_jacoco(pid, phase_changes)
            # For now only --batch 1 supported for crossmodule
            if cfg.prob.crossmodule_probability:
                assert module_pid == pid

            if cfg.prob.crossmodule_probability and pid not in output:
                provider, provider_compilation_time = \
                    self.compilation._build_crossmodule_provider(
                        proc_res, filter_patterns, dependency_paths, friend_paths,
                        module_name, compile_cwd)
                compilation_time += provider_compilation_time
                if self.options.time_metrics:
                    time_metrics[pid]["compilation_with_ir_dumps"] += \
                        provider_compilation_time
                provider_phase_changes = None
                if provider is not None and self.options.keep_everything:
                    provider_phase_changes = self.artifacts.preserve_ir_changes_to_tmp(
                        dirname, pid)
                # Artifacts land before the ledger advertises the program: the
                # KLIB and the pickle are on disk by the time the parent merges
                # the record returned below.
                record = None
                if provider is not None:
                    provider_klib, provider_command_args = provider
                    record = self.manager.publish_provider(
                        pid,
                        provider_klib,
                        provider_command_args,
                        dependency,
                        proc_res.stats.get('dropped_indirect_deps'))
                already_preserved = record is not None
                if record is not None:
                    published[pid] = record
                if already_preserved and self.options.keep_everything:
                    self.coverage.maybe_run_live_jacoco(pid, provider_phase_changes)
            if self.options.keep_everything and not already_preserved:
                phase_changes = self.artifacts.preserve_ir_changes_to_tmp(dirname, pid)
                self.artifacts.preserve_final_program_dir(pid)
                self.coverage.maybe_run_live_jacoco(pid, phase_changes)
            self.artifacts._cleanup_program_tmp(pid)
        # Clear the directory of programs.
        shutil.rmtree(dirname)
        return output, compilation_time, time_metrics, published