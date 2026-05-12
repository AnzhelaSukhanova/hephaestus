import re

from src.compilers.base import BaseCompiler
from src.args import args as cli_args

backend = cli_args.backend
is_native = backend == 'native'
compiler = f'$HOME/kotlin/{"kotlin-native/dist" if is_native else "dist/kotlinc"}/bin/kotlinc-{backend}'

class KotlinCompiler(BaseCompiler):
    ERROR_REGEX = re.compile(
        r'([:\\a-zA-Z0-9\/_]+.kt):\d+:\d+:[ ]+error:[ ]+(.*)')
    CRASH_REGEX = re.compile(
        r'(org\.jetbrains\..*)\n(.*)',
        re.MULTILINE
    )

    def __init__(self, input_name, filter_patterns=None):
        super().__init__(input_name, filter_patterns)

    @classmethod
    def get_compiler_version(cls):
        return [compiler, '-version']

    def get_compiler_cmd(self):
        if is_native:
            return [compiler, self.input_name, '-produce', 'library', '-o', self.input_name,
                    '-nowarn']
        else:
            is_wasm = backend == 'wasm'
            stdlib = f'$HOME/kotlin/libraries/stdlib/build/libs/kotlin-stdlib-{"wasm-" if is_wasm else ""}js-2.4.255-SNAPSHOT.klib'
            return [compiler, self.input_name,
                    '-ir-output-dir', 'dir',
                    '-ir-output-name', 'library',
                    '-libraries', stdlib,
                    '-nowarn']

    def get_filename(self, match):
        return match[0]

    def get_error_msg(self, match):
        return match[1]
