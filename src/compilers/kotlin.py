import re
import shutil
import os
import subprocess

from src.compilers.base import BaseCompiler


# Native compilers
#linux:
# compiler = '$HOME/kotlin/kotlin-native/dist/bin/konanc'

#windows hardcode
compiler = "D:/fuzzer/kotlin/kotlin-native/dist/bin/konanc.bat"

# compiler = '$HOME/.konan/kotlin-native-prebuilt-macos-aarch64-/bin/konanc'

# JS compiler
# compiler = '$HOME/kotlin/dist/kotlinc/bin/kotlinc-js'

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

    @classmethod
    def is_installed(cls) -> bool:
        if shutil.which(compiler, mode = os.X_OK) is None:
            return False
        cmd = cls.get_compiler_version()
        try:
            subprocess.run(cmd, capture_output=True, check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            return False



    def get_compiler_cmd(self):
        # Native compilation
        return [compiler, self.input_name, '-produce', 'library', '-o', self.input_name, '-nowarn']
        # JS compilation
        # return [compiler, self.input_name,
        #         '-Xir-produce-klib-file',
        #         '-ir-output-name', 'library_js',
        #         '-ir-output-dir', 'library_js',
        #         '-libraries', '$HOME/kotlin/libraries/stdlib/build/libs/kotlin-stdlib-js-2.4.255-SNAPSHOT.klib',
        #         '-nowarn']

    def get_filename(self, match):
        return match[0]

    def get_error_msg(self, match):
        return match[1]
