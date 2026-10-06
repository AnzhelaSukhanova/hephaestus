import re
import os
from collections.abc import Collection

from src.compilers.base import BaseCompiler


class JavaCompiler(BaseCompiler[None]):
    # Match (example.groovy):(error message until empty line)
    ERROR_REGEX = re.compile(
        r'([a-zA-Z0-9\/_]+.java):(\d+:[ ]+error:[ ]+.*)(.*?(?=\n{1,}))')

    CRASH_REGEX = re.compile(r'(java\.lang.*)\n(.*)')

    def __init__(self, input_name: str,
                 filter_patterns: Collection[str] | None = None, *,
                 settings: None = None) -> None:
        input_name = os.path.join(input_name, '*', '*.java')
        super().__init__(input_name, filter_patterns, settings=settings)

    @classmethod
    def get_compiler_version(cls, settings: None = None) -> list[str]:
        return ['javac', '-version']

    def get_compiler_cmd(self) -> list[str]:
        return ['javac', '-nowarn', self.input_name]

    def get_filename(self, match: tuple[str, ...]) -> str:
        return match[0]

    def get_error_msg(self, match: tuple[str, ...]) -> str:
        return match[1]
