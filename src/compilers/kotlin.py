import re

from src.compilers.base import BaseCompiler
from src.args import args as cli_args

backend = cli_args.backend
is_native = backend == 'native'
compiler = f'$HOME/kotlin/{"kotlin-native/dist" if is_native else "dist/kotlinc"}/bin/kotlinc-{backend}'

class KotlinCompiler(BaseCompiler):
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

    def __init__(self, input_name, filter_patterns=None):
        super().__init__(input_name, filter_patterns)

    @classmethod
    def get_compiler_version(cls):
        return [compiler, '-version']

    def get_compiler_cmd(self):
        # The problem is that for get_phases_compiler_cmd we want to provide concrete filename, but self.input_name usually stores whole folder for compilation (batch)
        # And also we want to use _get_compiler_cmd as a base, so don't attach dump_ir_flags to it directly
        dump_ir_flags = ['-Xphases-to-dump=' + self.IR_MODIFYING_INLINER_PHASES, '-Xdump-directory=' + self.input_name + '/ir'] if cli_args.dump_ir  else []
        return self._get_compiler_cmd(self.input_name) + dump_ir_flags

    def _get_compiler_cmd(self, input_name):
        if is_native:
            return [compiler, input_name, '-produce', 'library', '-o', input_name,
                        '-nowarn']
        else:
            is_wasm = backend == 'wasm'
            stdlib = f'$HOME/kotlin/libraries/stdlib/build/libs/kotlin-stdlib-{"wasm-" if is_wasm else ""}js-2.4.255-SNAPSHOT.klib'
            return [compiler, input_name,
                    '-ir-output-dir', input_name,
                    '-ir-output-name', 'src',
                    '-libraries', stdlib,
                    '-nowarn']

    def get_filename(self, match):
        return match[0]

    def get_error_msg(self, match):
        return f"{match[1]}:{match[2]}: {match[3]}"

    def get_error_enrichment_cmds(self, err_file):
        return {
            "xprofile-phases": self._get_compiler_cmd(err_file) + ['-Xprofile-phases']
        }

    def analyze_error_enrichment_output(self, err_file, command_outputs):
        xprofile_phases_output =  command_outputs.get("xprofile-phases", "")
        if self.BACKEND_PHASE_REGEX.search(xprofile_phases_output):
            return {"error_phase": "backend"}
        return {"error_phase": "frontend"}