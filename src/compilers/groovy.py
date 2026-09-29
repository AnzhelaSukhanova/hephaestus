import re
import os
from collections.abc import Collection

from src.compilers.base import BaseCompiler


class GroovyCompiler(BaseCompiler):
    # Match (example.groovy):(error message until empty line)
    ERROR_REGEX = re.compile(r'([a-zA-Z0-9\\/_]+.groovy):([\s\S]*?(?=\n{2,}))')

    CRASH_REGEX = re.compile(r'(at org.codehaus.groovy)(.*)')

    STACKOVERFLOW_REGEX = re.compile(r'(.*java.lang.StackOverflowError)(.*)')

    def __init__(self, input_name: str,
                 filter_patterns: Collection[str] | None = None) -> None:
        input_name = os.path.join(input_name, '*', '*.groovy')
        super().__init__(input_name, filter_patterns)

    @classmethod
    def get_compiler_version(cls) -> list[str]:
        return ['groovyc-l', '-version']

    def get_compiler_cmd(self) -> list[str]:
        return ['groovyc-l', '--compile-static', self.input_name]

    def get_filename(self, match: tuple[str, ...]) -> str:
        return match[0]

    def get_error_msg(self, match: tuple[str, ...]) -> str:
        return match[1]

    def analyze_compiler_output(self, output: str
                                ) -> tuple[dict[str, list[str]] | None,
                                           list[tuple[str, ...]]]:
        failed, matches = super().analyze_compiler_output(output)
        stack_overflow = re.search(self.STACKOVERFLOW_REGEX, output)
        if stack_overflow and not matches:
            self.crash_msg = output
            return None, matches

        return failed, matches
