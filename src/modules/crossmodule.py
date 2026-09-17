import json
import os
import shutil


class CrossModuleManager:

    def __init__(self, test_directory, config, backend, compiler_version,
                 read_manifest_depends, random_source, load_program):
        self.test_directory = test_directory
        self.config = config
        self.backend = backend
        self.compiler_version = compiler_version
        self.read_manifest_depends = read_manifest_depends
        self.random = random_source
        self.load_program = load_program

    def enabled(self):
        return bool(self.config.prob.crossmodule_probability)

    def ledger_path(self):
        return os.path.join(self.test_directory, 'crossmodule.json')

    def load_ledger(self):
        """Load the pid-to-dependency ledger, tolerating its first absence."""
        try:
            with open(self.ledger_path(), 'r') as ledger_file:
                ledger = json.load(ledger_file)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(ledger, dict):
            return {}
        return ledger

    def save_ledger(self, ledger):
        ledger_path = self.ledger_path()
        with open(ledger_path + '.tmp', 'w') as out:
            json.dump(ledger, out, indent=2)
        os.replace(ledger_path + '.tmp', ledger_path)

    def published_klibs_dir(self):
        return os.path.join(self.test_directory, 'klibs')

    def published_klib_path(self, pid):
        """Where a published KLIB lives; consumers reference it in place."""
        return os.path.join(self.published_klibs_dir(),
                            'd' + str(pid) + '.klib')

    def publish_klib(self, pid, source_klib, command_args):
        """Retain a per-node KLIB: it is published once and never rebuilt."""
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
        meta = {
            'pid': pid,
            'command': list(command_args),
            'compiler_version': self.compiler_version,
            'flags': [arg for arg in command_args if arg.startswith('-X')],
            'backend': self.backend,
        }
        meta_path = os.path.join(klibs_dir, 'd' + str(pid) + '.meta.json')
        with open(meta_path + '.tmp', 'w') as out:
            json.dump(meta, out, indent=2)
        os.replace(meta_path + '.tmp', meta_path)
        return dst

    def manifest_closure_diff(self, klib_path, closure, dropped=None):
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
    def _ledger_record(ledger, pid):
        record = ledger.get(str(pid))
        return record if isinstance(record, dict) else {}

    def ledger_closure(self, ledger, pid):
        """The pids whose KLIBs a consumer of ``pid`` also has to be handed."""
        closure = self._ledger_record(ledger, pid).get('closure') or []
        return [int(item) for item in closure]

    def publication_record(self, direct, ledger=None):
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

    def provider_candidates(self, pid):
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
            if max_depth and len(closure) + 2 > max_depth:
                continue
            binary = os.path.join(self.test_directory, str(provider_pid),
                                  'program.kt.bin')
            if not os.path.isfile(binary):
                continue
            if not os.path.exists(self.published_klib_path(provider_pid)):
                continue
            candidates.append(provider_pid)
        return candidates

    def prepare_dependency(self, pid):
        """Seed a new program from a published dependency.

        The dependency is reused exactly as published: its pickle seeds the
        context, and its KLIB, together with the KLIBs of its own closure, is
        handed to the compiler in place. Nothing is copied or rebuilt.
        """
        ledger = self.load_ledger()
        candidates = self.provider_candidates(pid)
        while candidates:
            provider_pid = self.random.choice(candidates)
            candidates.remove(provider_pid)
            closure = [provider_pid] + self.ledger_closure(ledger, provider_pid)
            klibs = [os.path.abspath(self.published_klib_path(item))
                     for item in closure]
            if not all(os.path.exists(klib) for klib in klibs):
                continue
            binary = os.path.join(self.test_directory, str(provider_pid),
                                  'program.kt.bin')
            try:
                context = self.load_program(binary).context
                context.prepare_this_context_for_import()
            except Exception:
                # A published program should be complete, but a stale or corrupt
                # directory must stay harmless to a later worker.
                continue

            return {
                'pid': provider_pid,
                'context': context,
                'closure': closure,
                'klibs': klibs,
            }
        return None

    @staticmethod
    def _klib_pid(klib_path):
        """The pid a published KLIB belongs to."""
        stem = os.path.splitext(os.path.basename(str(klib_path)))[0]
        if stem.startswith('d') and stem[1:].isdigit():
            return int(stem[1:])
        return stem

    def apply_drop_knob(self, klibs):
        """Adversarially withhold indirect dependencies from the compiler.

        Each indirect KLIB is dropped independently, and only from the command
        line: the ledger keeps the full closure, so the fault stays attributable
        to the complete graph. The direct dependency is never dropped.
        """
        prob = self.config.prob.drop_indirect_deps_prob
        if not prob or len(klibs) < 2:
            return list(klibs), []
        kept = [klibs[0]]
        dropped = []
        for klib in klibs[1:]:
            if self.random.bool(prob):
                dropped.append(self._klib_pid(klib))
            else:
                kept.append(klib)
        return kept, dropped

    @staticmethod
    def dependency_klibs(proc_res):
        """A program's dependency closure KLIBs, the direct one first."""
        klibs = proc_res.dep_transitive_closure_klibs
        if klibs is None:
            return []
        if isinstance(klibs, (str, os.PathLike)):
            return [str(klibs)]
        return [str(path) for path in klibs]

    def publish_program(self, pid):
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

    def publish_provider(self, pid, source_klib, command_args, direct,
                         dropped=None):
        """Publish a complete provider and return its ledger record.

        A provider can enter the ledger only after both its KLIB and its
        source/pickle artifacts have been published successfully.
        """
        published_klib = self.publish_klib(pid, source_klib, command_args)
        if published_klib is None or not os.path.exists(published_klib):
            return None
        if self.publish_program(pid) is None:
            return None
        record = self.publication_record(direct)
        diff = self.manifest_closure_diff(
            self.published_klib_path(pid), record['closure'], dropped)
        if diff is not None:
            record['manifest_diff'] = diff
        return record