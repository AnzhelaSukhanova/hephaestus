import re
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

import hephaestus
from src.compilers.base import BaseCompiler
from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.compilers.results import ProcessResult
from src.generators.config import cfg
from src.modules.models import ProgramRes


ROOT = Path(__file__).parents[1]


def _run_python(script, *cli_args):
    return subprocess.run(
        [sys.executable, "-c", script, "-P", *cli_args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _compiler_command(backend):
    compiler = KotlinCompiler(
        "/work/src/d7",
        dependency_klibs=["/published/d3.klib"],
        module_name="d7",
        settings=KotlinSettings(backend),
    )
    return compiler.get_compiler_cmd()


def test_native_per_node_command_uses_pid_module_name():
    command = _compiler_command("native")

    assert command[command.index("-o") + 1] == "d7"
    assert command[command.index("-library") + 1] == "/published/d3.klib"
    assert "-Xpurge-user-libs" not in command


def test_js_per_node_command_produces_named_klib():
    command = _compiler_command("js")

    assert "-Xir-produce-klib-file" in command
    assert command[command.index("-ir-output-dir") + 1] == "."
    assert command[command.index("-ir-output-name") + 1] == "d7"
    assert "-Xpurge-user-libs" not in command
    libraries = command[command.index("-libraries") + 1]
    assert "/published/d3.klib" in libraries.split(":")


def test_run_command_honors_explicit_cwd(tmp_path):
    fuzzer = hephaestus.Fuzzer(hephaestus.parse_args(
        ["-b", str(tmp_path / "bugs"), "-n", "session"]))

    status, output = fuzzer.compilation.run_command(["pwd"], cwd=str(tmp_path))

    assert status
    assert output.strip() == str(tmp_path)


def test_main_resolves_compiler_version_before_manager_and_worker_setup(tmp_path):
    script = """
import sys

import hephaestus
from src.generators.config import cfg
from src.modules import scheduler

calls = []


def version_command(arguments, get_stdout=True, cwd=None):
    calls.append(("version", arguments))
    return True, "test compiler\\n"


class Pool:
    def __init__(self, workers, initializer, initargs):
        calls.append(("pool", initargs))
        initializer(*initargs)

    def close(self):
        pass

    def join(self):
        pass


cfg.prob.crossmodule_probability = 0.0
fuzzer = hephaestus.Fuzzer(hephaestus.parse_args(sys.argv[1:]))
fuzzer.compilation.run_command = version_command
scheduler.mp.Pool = Pool
fuzzer.coverage.setup_live_jacoco = lambda: None
fuzzer.scheduler._run_parallel = lambda pool, version, batches: calls.append(
    ("run", version))

fuzzer.start()

assert [call[0] for call in calls] == ["version", "pool", "run"]
inputs, = calls[1][1]
assert inputs.compiler_version == "test compiler"
assert calls[2][1] == "test compiler"
assert fuzzer.manager.compiler_version == "test compiler"
"""

    _run_python(script, "-b", str(tmp_path / "bugs"), "-n", "session",
                "--workers", "1")


class Manager:
    def __init__(self):
        self.published = []

    @staticmethod
    def enabled():
        return True

    def publish_provider(self, pid, source_klib, command_args, direct, dropped):
        assert Path(source_klib).read_text() == "isolated KLIB"
        self.published.append((pid, source_klib, command_args, direct, dropped))
        return {"direct": direct, "closure": []}


class Compiler(BaseCompiler[None]):
    ERROR_REGEX = re.compile(r"(?m)^(.+\.kt): (.*)$")
    CRASH_REGEX = re.compile(r"CRASH")

    def __init__(self, input_name, filter_patterns=None, dependency_klibs=None,
                 friend_klibs=None, module_name=None, *, settings=None):
        super().__init__(input_name, filter_patterns, settings=settings)
        self.module_name = module_name

    def get_compiler_cmd(self):
        return ["fake-compiler", self.input_name]

    def get_dependency_artifact_filename(self):
        return self.module_name + ".klib"

    def get_filename(self, match):
        return match[0]

    def get_error_msg(self, match):
        return match[1]


def test_provider_publication_reruns_the_correct_source_after_ncp_batch(
        tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.prob, "crossmodule_probability", 0.5)
    fuzzer = hephaestus.Fuzzer(
        hephaestus.parse_args(["-b", str(tmp_path / "bugs"), "-n", "session"]),
        compiler_types={"kotlin": Compiler})
    manager = Manager()
    fuzzer.evaluation.manager = manager
    # Diagnostic paths must not contain hyphens, unlike pytest's temporary paths.
    with tempfile.TemporaryDirectory() as temp_dir:
        batch_dir = Path(temp_dir) / "batch"
        src_dir = batch_dir / "src"
        correct_program = src_dir / "d1" / "program.kt"
        incorrect_program = src_dir / "d1_incorrect" / "program.kt"
        correct_program.parent.mkdir(parents=True)
        incorrect_program.parent.mkdir()
        correct_program.write_text("correct")
        incorrect_program.write_text("incorrect")

        calls = []

        def run_process(command, get_stdout=True, cwd=None):
            calls.append((command, cwd))
            klib = Path(cwd) / "d1.klib"
            if len(calls) == 1:
                klib.write_text("combined KLIB")
                return ProcessResult(1, f"{incorrect_program}: expected NCP error")
            assert not klib.exists()
            klib.write_text("isolated KLIB")
            return ProcessResult(0, "")

        def timed(fn, command, **kwargs):
            duration = 1.0 if command[1] == str(src_dir) else 2.0
            return fn(command, **kwargs), duration

        fuzzer.compilation.run_process = run_process
        fuzzer.compilation.timed = timed
        stats = {
            "error": None,
            "programs": {
                str(correct_program): True,
                str(incorrect_program): False,
            },
            "time": 0.0,
        }
        result = fuzzer.evaluation.check_oracle(
            str(batch_dir), OrderedDict([(1, ProgramRes(False, stats))]))

        assert result.faults == {}
        assert result.inconclusive == {}
        assert result.compilation_time == 3.0
        assert list(result.publications) == [1]
        assert [command[1] for command, _ in calls] == [
            str(src_dir), str(correct_program)]
        assert [cwd for _, cwd in calls] == [str(src_dir / "d1")] * 2
        assert len(manager.published) == 1
