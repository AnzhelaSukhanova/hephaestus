from __future__ import annotations

import re
import os
import shutil
import zipfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
import textwrap
from types import MappingProxyType
from typing import Literal
from urllib.parse import unquote

from src.compilers.base import BaseCompiler
from src.compilers.results import Diagnostic, ParsedDiagnostics


type KotlinBackend = Literal['js', 'native', 'wasm', 'jvm']


@dataclass(frozen=True)
class KotlinSettings:
    backend: KotlinBackend = 'native'
    dump_ir: bool = False
    executable: str | None = None


BACKENDS: tuple[KotlinBackend, ...] = ('js', 'native', 'wasm', 'jvm')


def _compiler_executable(settings: KotlinSettings) -> str:
    if settings.executable is not None:
        return settings.executable
    distribution = ('kotlin-native/dist' if settings.backend == 'native'
                    else 'dist/kotlinc')
    return f'$HOME/kotlin/{distribution}/bin/kotlinc-{settings.backend}'


KOTLIN_COMPARISON_VARIANTS: Mapping[str, KotlinSettings] = MappingProxyType({
    'js': KotlinSettings('js'),
    'native': KotlinSettings('native'),
    'wasm': KotlinSettings('wasm'),
    'jvm': KotlinSettings('jvm'),
})


class KotlinCompiler(BaseCompiler[KotlinSettings]):
    ERROR_REGEX = re.compile(
        r'([:\\a-zA-Z0-9\/_]+\.kt):(\d+):(\d+):\s+error:\s+(.*)')
    CRASH_REGEX = re.compile(
        r'(org\.jetbrains\..*)\n(.*)',
        re.MULTILINE
    )
    BACKEND_PHASE_REGEX = re.compile(
        r'^[A-Za-z][A-Za-z0-9_$]*: [0-9]+ msec$',
        re.MULTILINE
    )
    IR_MODIFYING_INLINER_PHASES = 'LocalClassesInInlineLambdasLowering,PreSerializationPrivateFunctionInlining,OuterThisInInlineFunctionsSpecialAccessorLowering,SyntheticAccessorLowering,FunctionInlining,InlineFunctionSerializationPreProcessing,RedundantCastsRemoverLowering'

    def __init__(self, input_name: str,
                 filter_patterns: Collection[str] | None = None,
                 dependency_klibs: list[str] | None = None,
                 friend_klibs: list[str] | None = None,
                  module_name: str | None = None, *,
                  settings: KotlinSettings = KotlinSettings(),
                  output_directory: str | None = None,
                  klib_output: bool = False,
                  source_aliases: dict[str, str] | None = None,
                 dependency_aliases: dict[str, str] | None = None) -> None:
        super().__init__(input_name, filter_patterns, settings=settings)
        # The whole transitive closure becomes the library path, while only
        # the direct dependencies are attached as a friend module.
        self.dependency_klibs: list[str] = dependency_klibs or []
        self.friend_klibs: list[str] = friend_klibs or []
        # Unique name of produced KLIB, used to refer by consumers.
        self.module_name = module_name
        self.output_directory = output_directory
        self.klib_output = klib_output
        self.source_aliases = source_aliases or {}
        self.dependency_aliases = dependency_aliases or {}

    def get_dependency_artifact_filename(self) -> str | None:
        """KLIB (or, on JVM, JAR) file build leaves in its working directory."""
        if not self.module_name:
            return None
        return self.module_name + ('.jar' if self.settings.backend == 'jvm' else '.klib')

    def validate_dependency_artifact(self, path: str) -> bool:
        return self.read_manifest_depends(path) is not None

    @classmethod
    def get_compiler_version(
            cls, settings: KotlinSettings = KotlinSettings(),
            scratch_directory: str | None = None) -> list[str]:
        command = [_compiler_executable(settings), '-version']
        # The JS and Wasm launchers insist on an output even when only asked for their version.
        if scratch_directory is not None and settings.backend in ('js', 'wasm'):
            command += ['-ir-output-name', 'probe', '-ir-output-dir', scratch_directory]
        return command

    def installation_problem(self) -> str | None:
        executable = os.path.expanduser(os.path.expandvars(_compiler_executable(self.settings)))
        if shutil.which(executable) is None:
            return f'missing compiler executable {executable}'
        library = os.path.expanduser(os.path.expandvars(self.stdlib_path()))
        if not os.path.exists(library):
            return f'missing standard library {library}'
        return None

    def get_compiler_cmd(self) -> list[str]:
        # The problem is that for get_phases_compiler_cmd we want to provide concrete filename, but self.input_name usually stores whole folder for compilation (batch)
        # And also we want to use _get_compiler_cmd as a base, so don't attach dump_ir_flags to it directly
        dump_ir_flags = ['-Xphases-to-dump=' + self.IR_MODIFYING_INLINER_PHASES, '-Xdump-directory=' + self.input_name + '/ir'] if self.settings.dump_ir else []
        return self._get_compiler_cmd(self.input_name) + dump_ir_flags

    def _get_compiler_cmd(self, input_name: str) -> list[str]:
        compiler = _compiler_executable(self.settings)
        inputs = ([str(path.resolve()) for path in sorted(Path(input_name).rglob('*.kt'))]
                  if self.klib_output and os.path.isdir(input_name)
                  else [os.path.abspath(input_name)] if self.klib_output else [input_name])
        command = [compiler, *inputs,
                   '-J-Dorg.jetbrains.kotlin.cliMessageRenderer=FullPath']
        if self.settings.backend == 'native':
            output_name = self.module_name or input_name
            if self.output_directory is not None and self.module_name is not None:
                output_name = os.path.join(self.output_directory, self.module_name)
            command.extend(['-produce', 'library',
                            '-o', output_name,
                            '-nowarn', '-Xklib-ir-inliner=full'])
            command.extend(self._native_dependency_flags())
            return command
        elif self.settings.backend == 'jvm':
            # JVM runs codegen too and writes a linked JAR, which consumers
            # put on their classpath.
            output_name = self.module_name or input_name
            if self.output_directory is not None and self.module_name is not None:
                output_name = os.path.join(self.output_directory, self.module_name)
            command.extend(['-d', output_name + '.jar', '-nowarn'])
            if self.module_name:
                command.extend(['-module-name', self.module_name])
            if self.dependency_klibs:
                command.extend(['-classpath', os.pathsep.join(self.dependency_klibs)])
            if self.friend_klibs:
                command.append('-Xfriend-paths=' + ','.join(self.friend_klibs))
            return command
        else:
            if self.module_name:
                # The KLIB is written next to the sources of this node, so
                # its name is the module's own name.
                command.extend(['-Xir-produce-klib-file',
                                '-ir-output-dir', self.output_directory or '.',
                                '-ir-output-name', self.module_name])
            else:
                command.extend(['-ir-output-dir', input_name,
                                '-ir-output-name', 'src'])
            command.extend(['-libraries', self._libraries_value(),
                            '-nowarn', '-Xklib-ir-inliner=full'])
            command.extend(self._js_dependency_flags())
            return command

    @staticmethod
    def _normalized_path(path: str) -> str:
        path = unquote(path.removeprefix('file://')).replace('\\', '/')
        if re.match(r'^/[A-Za-z]:/', path):
            path = path[1:]
        return path

    def _normalize_text(self, text: str) -> str:
        text = self._normalized_path(text)
        aliases = {**self.source_aliases, **self.dependency_aliases}
        expanded = {self._normalized_path(path): logical for path, logical in aliases.items()}
        expanded.update({self._normalized_path(os.path.realpath(path)): logical
                         for path, logical in aliases.items() if os.path.exists(path)})
        for path, logical in sorted(expanded.items(), key=lambda item: -len(item[0])):
            text = text.replace(path, logical)
        return text

    def collect_diagnostics(self, output: str) -> ParsedDiagnostics:
        output = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output).replace('\r\n', '\n').replace('\r', '\n')
        source_error = re.compile(
            r'^(?:e:\s*)?(.+?\.kt):(?:(\d+):(\d+)|\s*\((\d+),\s*(\d+)\)):\s*(?:error:\s*)?(.*)$')
        errors = []
        unparsed = []
        current = None
        continuation = []

        def finish():
            if current is not None:
                source, line, column, message, name = current
                tail = textwrap.dedent('\n'.join(continuation)).rstrip()
                errors.append(Diagnostic(source, line, column,
                                         message + ('\n' + tail if tail else ''), name))

        lines = output.splitlines()
        for index, line in enumerate(lines):
            match = source_error.match(line)
            if match and ('error:' in line or line.startswith('e:')):
                finish()
                continuation = []
                path, row, col, row_alt, col_alt, message = match.groups()
                named = re.match(r'^\[([A-Z_0-9]+)\]\s*(.*)', message)
                current = (self._normalize_text(path), int(row or row_alt),
                           int(col or col_alt), self._normalize_text(named[2] if named else message),
                           named[1] if named else None)
            elif line.startswith(('error:', 'e:')):
                finish()
                continuation = []
                current = (None, None, None, self._normalize_text(line.split(':', 1)[1].strip()), None)
            elif (current is not None and index + 1 < len(lines)
                  and re.fullmatch(r'\s*[\^~]+\s*', lines[index + 1])):
                continue
            elif current is not None and re.fullmatch(r'\s*[\^~]+\s*', line):
                continue
            elif current is not None and (line[:1].isspace() or not line):
                continuation.append(self._normalize_text(line))
            else:
                finish()
                current = None
                continuation = []
                if line.strip() and not line.startswith(('warning:', 'w:', 'info:', 'i:')):
                    unparsed.append(line)
        finish()
        return ParsedDiagnostics(tuple(errors), not unparsed, tuple(unparsed))

    def comparison_errors(self, errors: tuple[Diagnostic, ...]) -> tuple[Diagnostic, ...]:
        return tuple(error for error in errors if not any(
            re.search(pattern, f'{error.source or ""}:{error.line}:{error.column}: error: {error.message}')
            for pattern in self.filter_patterns))

    def stdlib_path(self) -> str:
        if self.settings.backend == 'native':
            return '$HOME/kotlin/kotlin-native/dist/klib/common/stdlib'
        if self.settings.backend == 'jvm':
            return '$HOME/kotlin/dist/kotlinc/lib/kotlin-stdlib.jar'
        is_wasm = self.settings.backend == 'wasm'
        return f'$HOME/kotlin/libraries/stdlib/build/libs/kotlin-stdlib-{"wasm-" if is_wasm else ""}js-2.4.255-SNAPSHOT.klib'

    def _libraries_value(self) -> str:
        libraries = [self.stdlib_path()] + self.dependency_klibs
        return os.pathsep.join(libraries)

    def _native_dependency_flags(self) -> list[str]:
        flags = []
        for klib in self.dependency_klibs:
            flags.extend(['-library', klib])
        if self.friend_klibs:
            flags.extend(['-friend-modules',
                          os.pathsep.join(self.friend_klibs)])
        return flags

    def _js_dependency_flags(self) -> list[str]:
        if not self.friend_klibs:
            return []
        return [f'-Xfriend-modules={os.pathsep.join(self.friend_klibs)}']

    @staticmethod
    def read_manifest_depends(klib_path: str) -> set[str] | None:
        """The user modules a KLIB records as its own dependencies.

        ``stdlib`` is dropped: only user modules say anything about the
        module graph this run built. A JAR is usable once its
        ``META-INF/MANIFEST.MF`` is readable, and yields an empty set: no
        dependency names are extracted from it.
        """
        is_jar = klib_path.endswith('.jar')
        manifest = 'META-INF/MANIFEST.MF' if is_jar else 'default/manifest'
        try:
            if os.path.isdir(klib_path):
                with open(os.path.join(klib_path, manifest)) as source:
                    text = source.read()
            else:
                with zipfile.ZipFile(klib_path) as archive:
                    text = archive.read(manifest).decode()
        except (OSError, KeyError, zipfile.BadZipFile):
            return None
        if is_jar:
            return set()
        for line in text.splitlines():
            if line.startswith('depends='):
                return {module
                        for module in line[len('depends='):].split()
                        if module != 'stdlib'}
        return set()

    def get_filename(self, match: tuple[str, ...]) -> str:
        return match[0]

    def get_error_msg(self, match: tuple[str, ...]) -> str:
        return f"{match[1]}:{match[2]}: {match[3]}"

    def get_error_enrichment_cmds(self, err_file: str) -> dict[str, list[str]]:
        return {
            "xprofile-phases": self._get_compiler_cmd(err_file) + ['-Xprofile-phases']
        }

    def analyze_error_enrichment_output(
            self, err_file: str, command_outputs: dict[str, str]
    ) -> dict[str, str]:
        xprofile_phases_output =  command_outputs.get("xprofile-phases", "")
        if self.BACKEND_PHASE_REGEX.search(xprofile_phases_output):
            return {"error_phase": "backend"}
        return {"error_phase": "frontend"}