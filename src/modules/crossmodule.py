import json
import os
import shutil
import hashlib
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from typing import NotRequired, TypedDict

from src.generators.config import GenConfig
from src.ir import ast
from src.ir.context import Context
from src.utils import RandomUtils
from src.compilers.results import VariantObservation
from src.compilers.kotlin import KotlinSettings


class ManifestDiff(TypedDict):
    manifest_depends: list[str]
    closure: list[str]
    unexpected: list[str]


class PublicationRecord(TypedDict):
    direct: int | None
    closure: list[int]
    variants: NotRequired[dict[str, 'VariantPublication']]
    # Diagnostics travel with the record to the parent, but never enter the ledger.
    manifest_diff: NotRequired[ManifestDiff]
    variant_manifest_diff: NotRequired[dict[str, ManifestDiff]]


DIAGNOSTIC_RECORD_KEYS = ('manifest_diff', 'variant_manifest_diff')


class VariantPublication(TypedDict):
    backend: str
    module_name: str
    klib: str
    metadata: str


class PublicationError(RuntimeError):
    pass


class KlibMetadata(TypedDict):
    pid: int
    command: list[str]
    compiler_version: str
    flags: list[str]
    backend: str
    manifest_diff: NotRequired[ManifestDiff]


class PreparedDependency(TypedDict):
    pid: int
    context: Context
    closure: list[int]


class CrossModuleManager:

    def __init__(self, test_directory: str, config: GenConfig,
                 backend: str, compiler_version: str,
                 read_manifest_depends: Callable[[str], set[str] | None],
                 random_source: RandomUtils,
                 load_program: Callable[[str], ast.Program | None], *,
                 settings_by_variant: Mapping[str, KotlinSettings] | None = None,
                 versions_by_variant: Mapping[str, str] | None = None) -> None:
        self.test_directory: str = test_directory
        self.config: GenConfig = config
        self.backend: str = backend
        self.compiler_version: str = compiler_version
        self.read_manifest_depends: Callable[[str], set[str] | None] = read_manifest_depends
        self.random: RandomUtils = random_source
        self.load_program: Callable[[str], ast.Program | None] = load_program
        # Backend comparison publishes one KLIB per configured variant_id, built
        # with these settings by the compiler versions recorded at startup.
        self.settings_by_variant: Mapping[str, KotlinSettings] | None = (
            dict(settings_by_variant) if settings_by_variant is not None else None)
        self.versions_by_variant: Mapping[str, str] = dict(versions_by_variant or {})
        self._source_identity_cache: dict[int, str] = {}

    def enabled(self) -> bool:
        return bool(self.config.prob.crossmodule_probability)

    def ledger_path(self) -> str:
        return os.path.join(self.test_directory, 'crossmodule.json')

    def load_ledger(self) -> dict[str, object]:
        """Load the pid-to-dependency ledger, tolerating its first absence."""
        self._source_identity_cache = {}
        try:
            with open(self.ledger_path(), 'r') as ledger_file:
                ledger: object = json.load(ledger_file)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(ledger, dict):
            return {}
        return ledger

    def save_ledger(self, ledger: Mapping[str, object]) -> None:
        ledger_path = self.ledger_path()
        with open(ledger_path + '.tmp', 'w') as out:
            json.dump(ledger, out, indent=2)
        os.replace(ledger_path + '.tmp', ledger_path)

    def commit_publications(self, publications: Mapping[int, PublicationRecord]) -> None:
        """Advertise published providers by merging them into the ledger.

        Only the parent commits, and only records whose artifacts were already
        published. Existing entries are kept, and the ledger is replaced
        atomically. Manifest disagreements are reported here, but the ledger
        holds only the graph.
        """
        if not publications:
            return
        ledger = self.load_ledger()
        for pid, record in publications.items():
            ledger[str(pid)] = {key: value for key, value in record.items()
                                if key not in DIAGNOSTIC_RECORD_KEYS}
            diff = record.get('manifest_diff')
            if diff is not None:
                print('\nProgram {} records dependencies outside its '
                      'closure: {}'.format(pid, diff['unexpected']))
            for variant_id, variant_diff in record.get('variant_manifest_diff', {}).items():
                print('\nProgram {} ({}) records dependencies outside its '
                      'closure: {}'.format(pid, variant_id, variant_diff['unexpected']))
        self.save_ledger(ledger)

    def published_klibs_dir(self) -> str:
        return os.path.join(self.test_directory, 'klibs')

    def published_klib_path(self, pid: int, variant_id: str | None = None) -> str:
        """Where a published KLIB (a JAR on JVM) lives; consumers reference it in place."""
        if variant_id is not None:
            return os.path.join(self.test_directory,
                                self.variant_publication(pid, variant_id)['klib'])
        return os.path.join(self.published_klibs_dir(),
                            f'd{pid}.jar' if self.backend == 'jvm' else f'd{pid}.klib')

    @staticmethod
    def _variant_file(pid: int, variant_id: str, suffix: str) -> str:
        """Variant artifacts are named by their variant_id, not by their backend."""
        return os.path.join('klibs', f'd{pid}-{variant_id}{suffix}')

    def variant_publication(self, pid: int, variant_id: str) -> VariantPublication:
        backend = self.settings_by_variant[variant_id].backend
        return {'backend': backend,
                'module_name': f'd{pid}',
                'klib': self._variant_file(pid, variant_id, '.jar' if backend == 'jvm' else '.klib'),
                'metadata': self._variant_file(pid, variant_id, '.meta.json')}

    def _source_identity(self, pid: int, source: str) -> str:
        identity = self._source_identity_cache.get(pid)
        if identity is None:
            with open(source, 'rb') as source_file:
                identity = hashlib.sha256(source_file.read()).hexdigest()
            self._source_identity_cache[pid] = identity
        return identity

    def _validate_variant(self, ledger: Mapping[str, object], pid: int,
                          variant_id: str) -> str | None:
        record = self._ledger_record(ledger, pid)
        if self.settings_by_variant is None:
            return 'No backend comparison variants are configured'
        variants = record.get('variants')
        if (not isinstance(variants, dict)
                or set(variants) != set(self.settings_by_variant)):
            return f'd{pid}: no committed complete comparison bundle'
        expected = self.variant_publication(pid, variant_id)
        if variants.get(variant_id) != expected:
            return f'd{pid}/{variant_id}: incompatible artifact identity'
        source = os.path.join(self.test_directory, str(pid), 'program.kt')
        try:
            with open(os.path.join(self.test_directory, expected['metadata'])) as metadata:
                meta = json.load(metadata)
            identity = self._source_identity(pid, source)
            if (not isinstance(meta, dict) or meta.get('pid') != pid
                    or meta.get('backend') != expected['backend']
                    or meta.get('module_name') != f'd{pid}'
                    or meta.get('settings') != asdict(self.settings_by_variant[variant_id])
                    or meta.get('compiler_version') != self.versions_by_variant.get(variant_id)
                    or meta.get('source_identity') != identity):
                return f'd{pid}/{variant_id}: incompatible or stale metadata'
            if not os.path.isfile(source + '.bin'):
                return f'd{pid}: missing provider pickle'
            klib = os.path.join(self.test_directory, expected['klib'])
            if self.read_manifest_depends(klib) is None:
                return f'd{pid}/{variant_id}: missing or unusable dependency KLIB'
        except (OSError, json.JSONDecodeError):
            return f'd{pid}/{variant_id}: unavailable provider artifacts'
        return None

    def resolve_variant_dependencies(self, closure: Sequence[int], variant_id: str
                                     ) -> tuple[list[str], str | None]:
        ledger = self.load_ledger()
        paths = []
        for pid in closure:
            reason = self._validate_variant(ledger, pid, variant_id)
            if reason:
                return paths, reason
            paths.append(os.path.abspath(self.published_klib_path(pid, variant_id)))
        return paths, None

    def publish_klib(self, pid: int, source_klib: str,
                     command_args: Sequence[str],
                     manifest_diff: ManifestDiff | None = None) -> str | None:
        """Retain a per-node KLIB: it is published once and never rebuilt.

        Its metadata records how it was built and any manifest disagreement.
        """
        if not os.path.exists(source_klib):
            return None
        klibs_dir = self.published_klibs_dir()
        os.makedirs(klibs_dir, exist_ok=True)
        dst = self.published_klib_path(pid)
        if os.path.isdir(source_klib):
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(source_klib, dst)
        else:
            shutil.copyfile(source_klib, dst + '.tmp')
            os.replace(dst + '.tmp', dst)
        meta: KlibMetadata = {
            'pid': pid,
            'command': list(command_args),
            'compiler_version': self.compiler_version,
            'flags': [arg for arg in command_args if arg.startswith('-X')],
            'backend': self.backend,
        }
        if manifest_diff is not None:
            meta['manifest_diff'] = manifest_diff
        meta_path = os.path.join(klibs_dir, 'd' + str(pid) + '.meta.json')
        with open(meta_path + '.tmp', 'w') as out:
            json.dump(meta, out, indent=2)
        os.replace(meta_path + '.tmp', meta_path)
        return dst

    def manifest_closure_diff(self, klib_path: str, closure: Sequence[int],
                              dropped: Sequence[int] | None = None
                              ) -> ManifestDiff | None:
        """Compare a KLIB's manifest dependencies against the ledger closure.

        Explicitly passed libraries may be listed even when unused. Anything the
        manifest names beyond the closure the driver handed it is a disagreement
        worth surfacing: either the ledger or the compiler is wrong about the
        module graph. Modules dropped from the command line are excluded, since
        a dropped module cannot be recorded in the first place.
        """
        depends = self.read_manifest_depends(klib_path)
        if depends is None:
            return None
        expected = {'d' + str(pid) for pid in closure}
        expected.difference_update('d' + str(pid) for pid in dropped or [])
        unexpected = sorted(depends - expected)
        if not unexpected:
            return None
        return {
            'manifest_depends': sorted(depends),
            'closure': sorted(expected),
            'unexpected': unexpected,
        }

    @staticmethod
    def _ledger_record(ledger: Mapping[str, object], pid: int
                       ) -> dict[str, object]:
        record = ledger.get(str(pid))
        return record if isinstance(record, dict) else {}

    def ledger_closure(self, ledger: Mapping[str, object], pid: int) -> list[int]:
        """The pids whose KLIBs a consumer of ``pid`` also has to be handed."""
        closure = self._ledger_record(ledger, pid).get('closure') or []
        if not isinstance(closure, (list, tuple)):
            return []
        return [int(item) for item in closure]

    def publication_record(self, direct: int | None,
                           ledger: Mapping[str, object] | None = None
                           ) -> PublicationRecord:
        """The ledger record of a published program: edge and closure.

        The closure is computed once, from the direct dependency's own published
        closure, and is never recomputed by traversal.
        """
        if direct is None:
            closure = []
        else:
            direct = int(direct)
            ledger = self.load_ledger() if ledger is None else ledger
            closure = [direct] + self.ledger_closure(ledger, direct)
        return {
            'direct': direct,
            'closure': closure,
        }

    def provider_candidates(self, pid: int) -> list[int]:
        """Published programs that may serve as a direct dependency.

        Every published program is a candidate, crossmodule ones included: a
        chain grows by a consumer becoming a provider itself. A candidate needs
        complete artifacts, and the chain it would head has to fit within
        ``max_module_chain_depth`` (0 is unbounded).
        """
        ledger = self.load_ledger()
        max_depth = self.config.prob.max_module_chain_depth
        candidates = []
        for provider_pid, record in ledger.items():
            if not str(provider_pid).isdigit() or int(provider_pid) == pid:
                continue
            if not isinstance(record, dict):
                continue
            provider_pid = int(provider_pid)
            closure = record.get('closure') or []
            if self.settings_by_variant is not None:
                if (not isinstance(closure, list)
                        or any(type(item) is not int for item in closure)
                        or len(set([provider_pid, *closure])) != len(closure) + 1):
                    continue
                if any(self._validate_variant(ledger, item, variant_id)
                       for item in [provider_pid, *closure]
                       for variant_id in self.settings_by_variant):
                    continue
            if max_depth and len(closure) + 2 > max_depth:
                continue
            binary = os.path.join(self.test_directory, str(provider_pid),
                                  'program.kt.bin')
            if not os.path.isfile(binary):
                continue
            if (self.settings_by_variant is None
                    and not os.path.exists(self.published_klib_path(provider_pid))):
                continue
            candidates.append(provider_pid)
        return candidates

    def prepare_dependency(self, pid: int) -> PreparedDependency | None:
        """Seed a new program from a published dependency.

        The dependency is reused exactly as published: its pickle seeds the
        context, and its KLIB closure is checked before the logical IDs are
        handed to generation. Nothing is copied or rebuilt.
        """
        ledger = self.load_ledger()
        candidates = self.provider_candidates(pid)
        while candidates:
            provider_pid = self.random.choice(candidates)
            candidates.remove(provider_pid)
            closure = [provider_pid] + self.ledger_closure(ledger, provider_pid)
            if self.settings_by_variant is not None:
                if any(self._validate_variant(ledger, item, variant_id)
                       for item in closure for variant_id in self.settings_by_variant):
                    continue
            else:
                klibs = self.dependency_klibs(closure)
                if not all(os.path.exists(klib) for klib in klibs):
                    continue
            binary = os.path.join(self.test_directory, str(provider_pid),
                                  'program.kt.bin')
            try:
                program = self.load_program(binary)
                if program is None:
                    continue
                context = program.context
                context.prepare_this_context_for_import()
            except Exception:
                # A published program should be complete, but a stale or corrupt
                # directory must stay harmless to a later worker.
                continue

            return {
                'pid': provider_pid,
                'context': context,
                'closure': closure,
            }
        return None

    def apply_drop_knob(self, closure: Sequence[int]
                        ) -> tuple[list[int], list[int]]:
        """Adversarially withhold indirect dependencies from the compiler.

        Each indirect KLIB is dropped independently, and only from the command
        line: the ledger keeps the full closure, so the fault stays attributable
        to the complete graph. The direct dependency is never dropped.
        """
        prob = self.config.prob.drop_indirect_deps_prob
        if not prob or len(closure) < 2:
            return list(closure), []
        kept = [closure[0]]
        dropped: list[int] = []
        for pid in closure[1:]:
            if self.random.bool(prob):
                dropped.append(pid)
            else:
                kept.append(pid)
        return kept, dropped

    def dependency_klibs(self, closure: Sequence[int],
                         variant_id: str | None = None) -> list[str]:
        """Resolve a logical dependency closure, preserving its order."""
        return [os.path.abspath(self.published_klib_path(pid, variant_id))
                for pid in closure]

    def publish_program(self, pid: int) -> ast.Program | None:
        """Publish a program's source and pickle, and return the program.

        A consumer is seeded from exactly these artifacts, so publication
        completes before the ledger advertises the program at all.
        """
        if not self.enabled():
            return None
        src_dir = os.path.join(self.test_directory, 'tmp', str(pid))
        source = os.path.join(src_dir, 'program.kt')
        if not (os.path.isfile(source) and os.path.isfile(source + '.bin')):
            return None

        dst_dir = os.path.join(self.test_directory, str(pid))
        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
        dst_source = os.path.join(dst_dir, 'program.kt')
        dst_binary = dst_source + '.bin'
        if not (os.path.isfile(dst_source) and os.path.isfile(dst_binary)):
            return None
        try:
            return self.load_program(dst_binary)
        except Exception:
            return None

    def publish_provider(self, pid: int, source_klib: str,
                         command_args: Sequence[str], direct: int | None,
                         dropped: Sequence[int] | None = None
                         ) -> PublicationRecord | None:
        """Publish a complete provider and return its ledger record.

        A provider can enter the ledger only after both its KLIB and its
        source/pickle artifacts have been published successfully.
        """
        record = self.publication_record(direct)
        diff = self.manifest_closure_diff(source_klib, record['closure'], dropped)
        published_klib = self.publish_klib(pid, source_klib, command_args, diff)
        if published_klib is None or not os.path.exists(published_klib):
            return None
        if self.publish_program(pid) is None:
            return None
        if diff is not None:
            record['manifest_diff'] = diff
        return record

    def publish_comparison_provider(
            self, pid: int, observations: Mapping[str, VariantObservation[KotlinSettings]],
            direct: int | None, closure: Sequence[int],
            dropped: Sequence[int] = ()) -> PublicationRecord:
        variant_ids = set(self.settings_by_variant or ())
        if self.settings_by_variant is None or set(observations) != variant_ids:
            raise PublicationError('Cannot publish an incomplete backend bundle')
        if str(pid) in self.load_ledger():
            raise PublicationError(f'd{pid}: committed provider artifacts are immutable')
        source_dir = os.path.join(self.test_directory, 'tmp', str(pid))
        source = os.path.join(source_dir, 'program.kt')
        if not os.path.isfile(source + '.bin') or self.load_program(source + '.bin') is None:
            raise PublicationError(f'd{pid}: no usable common provider source/pickle')
        with open(source, 'rb') as source_file:
            identity = hashlib.sha256(source_file.read()).hexdigest()
        record: PublicationRecord = {'direct': direct, 'closure': list(closure), 'variants': {}}
        diffs = {}
        os.makedirs(self.published_klibs_dir(), exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f'.bundle-d{pid}-',
                                         dir=self.published_klibs_dir()) as staging:
            staged_program = os.path.join(staging, 'program')
            shutil.copytree(source_dir, staged_program)
            for variant_id in sorted(variant_ids):
                variant = observations[variant_id]
                command = variant.command
                klib = variant.produced_dependency_artifact_path
                if (variant.variant_id != variant_id or command is None
                        or not command.succeeded()
                        or not variant.dependency_artifact_valid or klib is None
                        or self.read_manifest_depends(klib) is None):
                    raise PublicationError(
                        f'd{pid}/{variant_id}: no usable successful provider output')
                entry = self.variant_publication(pid, variant_id)
                staged_klib = os.path.join(staging, os.path.basename(entry['klib']))
                if os.path.isdir(klib):
                    shutil.copytree(klib, staged_klib)
                else:
                    shutil.copyfile(klib, staged_klib)
                diff = self.manifest_closure_diff(klib, closure, dropped)
                # The metadata describes the configuration this KLIB was built with.
                meta = {'pid': pid, 'backend': variant.settings.backend,
                        'module_name': f'd{pid}', 'settings': asdict(variant.settings),
                        'compiler_version': self.versions_by_variant[variant_id],
                        'command': list(command.command), 'cwd': command.cwd,
                        'source_identity': identity}
                if diff:
                    meta['manifest_diff'] = diff
                    diffs[variant_id] = diff
                with open(os.path.join(staging, os.path.basename(entry['metadata'])), 'w') as out:
                    json.dump(meta, out, indent=2)
                record['variants'][variant_id] = entry
            # Only the parent ledger commit makes these staged artifacts discoverable.
            for entry in record['variants'].values():
                for key in ('klib', 'metadata'):
                    os.replace(os.path.join(staging, os.path.basename(entry[key])),
                               os.path.join(self.test_directory, entry[key]))
            shutil.copytree(staged_program, os.path.join(self.test_directory, str(pid)),
                            dirs_exist_ok=True)
        if diffs:
            record['variant_manifest_diff'] = diffs
        return record