import re
import os
import zipfile
from collections.abc import Collection
from dataclasses import dataclass

from src.compilers.base import BaseCompiler


@dataclass(frozen=True)
class KotlinSettings:
    backend: str = 'native'
    dump_ir: bool = False
    executable: str | None = None


def _compiler_executable(settings: KotlinSettings) -> str:
    if settings.executable is not None:
        return settings.executable
    distribution = ('kotlin-native/dist' if settings.backend == 'native'
                    else 'dist/kotlinc')
    return f'$HOME/kotlin/{distribution}/bin/kotlinc-{settings.backend}'


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
                 settings: KotlinSettings = KotlinSettings()) -> None:
        super().__init__(input_name, filter_patterns, settings=settings)
        # The whole transitive closure becomes the library path, while only
        # the direct dependencies are attached as a friend module.
        self.dependency_klibs: list[str] = dependency_klibs or []
        self.friend_klibs: list[str] = friend_klibs or []
        # Unique name of produced KLIB, used to refer by consumers.
        self.module_name = module_name

    def get_klib_filename(self) -> str | None:
        """KLIB file build leaves in its working directory."""
        if not self.module_name:
            return None
        return self.module_name + '.klib'

    @classmethod
    def get_compiler_version(
            cls, settings: KotlinSettings = KotlinSettings()) -> list[str]:
        return [_compiler_executable(settings), '-version']

    def get_compiler_cmd(self) -> list[str]:
        # The problem is that for get_phases_compiler_cmd we want to provide concrete filename, but self.input_name usually stores whole folder for compilation (batch)
        # And also we want to use _get_compiler_cmd as a base, so don't attach dump_ir_flags to it directly
        dump_ir_flags = ['-Xphases-to-dump=' + self.IR_MODIFYING_INLINER_PHASES, '-Xdump-directory=' + self.input_name + '/ir'] if self.settings.dump_ir else []
        return self._get_compiler_cmd(self.input_name) + dump_ir_flags

    def _get_compiler_cmd(self, input_name: str) -> list[str]:
        compiler = _compiler_executable(self.settings)
        command = [compiler, input_name,
                   '-J-Dorg.jetbrains.kotlin.cliMessageRenderer=FullPath']
        if self.settings.backend == 'native':
            command.extend(['-produce', 'library',
                            '-o', self.module_name or input_name,
                            '-nowarn', '-Xklib-ir-inliner=full'])
            command.extend(self._native_dependency_flags())
            return command
        else:
            if self.module_name:
                # The KLIB is written next to the sources of this node, so
                # its name is the module's own name.
                command.extend(['-Xir-produce-klib-file',
                                '-ir-output-dir', '.',
                                '-ir-output-name', self.module_name])
            else:
                command.extend(['-ir-output-dir', input_name,
                                '-ir-output-name', 'src'])
            command.extend(['-libraries', self._libraries_value(),
                            '-nowarn', '-Xklib-ir-inliner=full'])
            command.extend(self._js_dependency_flags())
            return command

    def _stdlib(self) -> str:
        is_wasm = self.settings.backend == 'wasm'
        return f'$HOME/kotlin/libraries/stdlib/build/libs/kotlin-stdlib-{"wasm-" if is_wasm else ""}js-2.4.255-SNAPSHOT.klib'

    def _libraries_value(self) -> str:
        libraries = [self._stdlib()] + self.dependency_klibs
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
        return ['-Xfriend-modules', os.pathsep.join(self.friend_klibs)]

    @staticmethod
    def read_manifest_depends(klib_path: str) -> set[str] | None:
        """The user modules a KLIB records as its own dependencies.

        ``stdlib`` is dropped: only user modules say anything about the
        module graph this run built.
        """
        manifest = 'default/manifest'
        try:
            if os.path.isdir(klib_path):
                with open(os.path.join(klib_path, manifest)) as source:
                    text = source.read()
            else:
                with zipfile.ZipFile(klib_path) as archive:
                    text = archive.read(manifest).decode()
        except (OSError, KeyError, zipfile.BadZipFile):
            return None
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