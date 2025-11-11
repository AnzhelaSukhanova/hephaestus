import re

from src.compilers.base import BaseCompiler

compiler = '$HOME/kotlin/kotlin-native/dist/bin/konanc'
# compiler = '$HOME/kotlin/dist/kotlinc/bin/kotlinc-js'
# compiler = '$HOME/.konan/kotlin-native-prebuilt-macos-aarch64-2.3.0-dev-10303/bin/konanc'

class KotlinCompiler(BaseCompiler):
    ERROR_REGEX = re.compile(
        r'([a-zA-Z0-9\/_]+.kt):\d+:\d+:[ ]+error:[ ]+(.*)')
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
        return [compiler, self.input_name, '-Xklib-ir-inliner=full',
                '-produce', 'library', '-o', self.input_name, '-nowarn']
        # return [compiler, self.input_name,
        #         '-Xklib-ir-inliner=full',
        #         '-Xir-produce-klib-dir',
        #         '-ir-output-name', 'library_js',
        #         '-ir-output-dir', 'library_js',
        #         '-libraries', '$HOME/kotlin/libraries/stdlib/build/libs/kotlin-stdlib-js-2.3.255-SNAPSHOT.klib',
        #         '-nowarn']

    def get_filename(self, match):
        return match[0]

    def get_error_msg(self, match):
        return match[1]
