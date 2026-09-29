import os
import re
from collections.abc import Collection

from src.compilers.base import BaseCompiler


class ScalaCompiler(BaseCompiler):
    ERROR_REGEX = re.compile(
        r"-- .*Error: (.*\.scala):\d+:\d+ -+\n((?:[^-]+))", re.MULTILINE)
    CRASH_REGEX = re.compile(r".*at dotty(.*)")

    def __init__(self, input_name: str,
                 filter_patterns: Collection[str] | None = None) -> None:
        input_name = os.path.join(input_name, '*', '*.scala')
        super().__init__(input_name, filter_patterns)

    @classmethod
    def get_compiler_version(cls) -> list[str]:
        return ['scalac', '-version']

    def get_compiler_cmd(self) -> list[str]:
        return ['scalac', '-color', 'never', '-nowarn', self.input_name]

    def get_filename(self, match: tuple[str, ...]) -> str:
        return match[0]

    def get_error_msg(self, match: tuple[str, ...]) -> str:
        return match[1]
