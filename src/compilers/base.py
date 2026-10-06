from collections import defaultdict
from collections.abc import Collection
import re


class BaseCompiler[S]:
    ERROR_REGEX = None
    CRASH_REGEX = None

    def __init__(self, input_name: str,
                 filter_patterns: Collection[str] | None = None, *,
                 settings: S) -> None:
        self.input_name: str = input_name
        self.filter_patterns: Collection[str] = filter_patterns or []
        self.crash_msg: str | None = None
        self.settings: S = settings

    @classmethod
    def get_compiler_version(cls, settings: S) -> list[str]:
        raise NotImplementedError('get_compiler_version() must be implemented')

    def get_compiler_cmd(self) -> list[str]:
        raise NotImplementedError('get_compiler_cmd() must be implemented')

    def get_filename(self, match: tuple[str, ...]) -> str:
        raise NotImplementedError('get_filename() must be implemented')

    def get_error_msg(self, match: tuple[str, ...]) -> str:
        raise NotImplementedError('get_error_msg() must be implemented')

    def analyze_compiler_output(self, output: str
                                ) -> tuple[dict[str, list[str]] | None,
                                           list[tuple[str, ...]]]:
        crash_match = re.search(self.CRASH_REGEX, output)
        if crash_match:
            self.crash_msg = output
            return None, []
        failed: defaultdict[str, list[str]] = defaultdict(list)
        filtered_output = output
        for p in self.filter_patterns:
            filtered_output = re.sub(p, '', filtered_output)
        matches = re.findall(self.ERROR_REGEX, filtered_output)
        for match in matches:
            filename = self.get_filename(match)
            error_msg = self.get_error_msg(match)
            failed[filename].append(error_msg)
        return failed, matches

    def get_error_enrichment_cmds(self, err_file: str) -> dict[str, list[str]]:
        return {}

    def analyze_error_enrichment_output(
            self, err_file: str, command_outputs: dict[str, str]
    ) -> dict[str, str]:
        return {}
