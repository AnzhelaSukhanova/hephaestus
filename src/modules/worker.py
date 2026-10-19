from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from src import utils
from src.compilers.base import BaseCompiler
from src.generators.config import ConfigOverrideValue, cfg
from src.modules.oracle_modes import OracleMode
from src.modules.crossmodule import CrossModuleManager


if TYPE_CHECKING:
    from src.compilers.kotlin import KotlinBackend, KotlinSettings
    from src.modules.scheduler import Scheduler


class RunOptions(argparse.Namespace):
    transformation_types: list[str]
    transformations: int | None
    transformation_schedule: str
    replay: str | None
    log: bool
    debug: bool
    name: str
    test_directory: str
    language: str
    backend: KotlinBackend
    oracle: str
    options: Mapping[str, Mapping[str, int]]

    @classmethod
    def from_namespace(cls, options: argparse.Namespace) -> RunOptions:
        values = vars(options).copy()
        values['transformation_types'] = list(options.transformation_types)
        return cls(**deepcopy(values))


@dataclass(frozen=True)
class WorkerInputs[S]:
    options: RunOptions
    generator_config: dict[str, ConfigOverrideValue]
    compiler_version: str
    compiler_cls: type[BaseCompiler[S]]
    compiler_settings: S
    mode: OracleMode
    settings_by_variant: Mapping[str, S] | None = None
    versions_by_variant: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerResources[S]:
    options: RunOptions
    manager: CrossModuleManager
    compiler_cls: type[BaseCompiler[S]]
    compiler_settings: S
    mode: OracleMode
    scheduler: Scheduler[S] | None = None
    settings_by_variant: Mapping[str, S] | None = None
    versions_by_variant: Mapping[str, str] = field(default_factory=dict)


_worker_resources: WorkerResources | None = None
_worker_pid: int | None = None


def _no_manifest_depends(klib_path: str) -> set[str] | None:
    return None


def create_manager[S](options: RunOptions, compiler_version: str,
                      compiler_cls: type[BaseCompiler[S]], *,
                      settings_by_variant: Mapping[str, KotlinSettings] | None = None,
                      versions_by_variant: Mapping[str, str] | None = None
                      ) -> CrossModuleManager:
    return CrossModuleManager(
        test_directory=options.test_directory,
        config=cfg,
        backend=options.backend,
        compiler_version=compiler_version,
        read_manifest_depends=getattr(
            compiler_cls, 'read_manifest_depends', _no_manifest_depends),
        random_source=utils.random,
        load_program=utils.load_program,
        settings_by_variant=settings_by_variant,
        versions_by_variant=versions_by_variant,
    )


def snapshot_generator_config() -> dict[str, ConfigOverrideValue]:
    return json.loads(cfg.to_json())


def initialize_worker[S](
        inputs: WorkerInputs[S], *,
        scheduler_factory: Callable[[WorkerResources[S]], Scheduler[S]] | None = None
        ) -> WorkerResources[S]:
    global _worker_resources, _worker_pid
    cfg.__init__()
    cfg.json_config(inputs.generator_config)
    options = RunOptions.from_namespace(inputs.options)
    utils.random.remove_reserved_words(options.language)
    # Only backend comparison publishes per-variant bundles.
    manager = create_manager(
        options, inputs.compiler_version, inputs.compiler_cls,
        settings_by_variant=(inputs.settings_by_variant
                             if inputs.mode.compares_variants() else None),
        versions_by_variant=inputs.versions_by_variant)
    resources = WorkerResources(
        options, manager, inputs.compiler_cls, inputs.compiler_settings, inputs.mode,
        settings_by_variant=inputs.settings_by_variant,
        versions_by_variant=inputs.versions_by_variant)
    if scheduler_factory is not None:
        resources = replace(resources, scheduler=scheduler_factory(resources))
    _worker_resources = resources
    _worker_pid = os.getpid()
    return resources


def get_worker_resources() -> WorkerResources:
    if _worker_resources is None or _worker_pid != os.getpid():
        raise RuntimeError('Worker resources misuse')
    return _worker_resources