#! /usr/bin/env python3
import argparse
from collections.abc import Mapping, Sequence
import os

from src.args import parse_args, configure_generator, validate_args, pre_process_args
from src.compilers.base import BaseCompiler
from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.modules.artifacts import Artifacts
from src.modules.compilation import Compilation, COMPILERS
from src.modules.coverage import LiveCoverage
from src.modules.evaluation import Evaluation
from src.modules.generation import Generation
from src.modules.worker_reporting import Reporting
from src.modules.scheduler import Scheduler
from src.modules.worker import RunOptions, create_manager
from src.modules.crossmodule import CrossModuleManager


class Fuzzer:
    def __init__(
            self, options: argparse.Namespace,
            compiler_types: Mapping[str, type[BaseCompiler[KotlinSettings]] |
                                    type[BaseCompiler[None]]] = COMPILERS) -> None:
        self.options = RunOptions.from_namespace(options)
        self.manager: CrossModuleManager | None = None
        self.manager_pid: int | None = None
        self.artifacts = Artifacts(self.options)
        self.generation = Generation(self.options, self.artifacts)
        compiler_cls = compiler_types[self.options.language]
        settings = (KotlinSettings(
            backend=self.options.backend, dump_ir=self.options.dump_ir)
            if issubclass(compiler_cls, KotlinCompiler) else None)
        self.compilation = Compilation(
            self.options, self.artifacts, compiler_types, settings=settings)
        self.coverage = LiveCoverage(self.options, self.compilation)
        self.reporting = Reporting(self.options)
        self.evaluation = Evaluation(
            self.options, self.compilation, self.artifacts,
            self.coverage, self.reporting)
        self.scheduler = Scheduler(
            self.options, self.generation, self.evaluation, self.reporting,
            compiler_cls, compiler_settings=settings)

    def setup_crossmodule_manager(self, compiler_version: str) -> CrossModuleManager:
        process_id = os.getpid()
        if self.manager_pid == process_id:
            return self.manager
        self.manager = create_manager(
            self.options, compiler_version, self.scheduler.compiler_cls)
        self.manager_pid = process_id
        self.generation.manager = self.manager
        self.evaluation.manager = self.manager
        self.reporting.manager = self.manager
        return self.manager

    def start(self) -> None:
        validate_args(self.options)
        pre_process_args(self.options)
        _, compiler_version = self.compilation.run_command(
            self.compilation.version_command())
        compiler_version = compiler_version.strip()
        self.setup_crossmodule_manager(compiler_version)
        self.coverage.setup_live_jacoco()

        if self.options.debug or self.options.workers is None:
            self.scheduler.run(compiler_version)
        else:
            self.scheduler.run_parallel(compiler_version)


def main(argv: Sequence[str] | None = None) -> None:
    options = parse_args(argv)
    configure_generator(options)
    Fuzzer(options).start()


if __name__ == "__main__":
    main()
