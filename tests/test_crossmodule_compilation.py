import json
import subprocess
import sys
from pathlib import Path


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
    script = """
import json
from src.compilers.kotlin import KotlinCompiler

compiler = KotlinCompiler(
    "/work/src/d7",
    dependency_klibs=["/published/d3.klib"],
    module_name="d7",
)
print(json.dumps(compiler.get_compiler_cmd()))
"""
    return json.loads(_run_python(script, "--backend", backend))


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
    script = """
import json
import hephaestus

status, output = hephaestus.run_command(["pwd"], cwd={cwd!r})
print(json.dumps([status, output.strip()]))
""".format(cwd=str(tmp_path))

    status, output = json.loads(_run_python(script))

    assert status
    assert output == str(tmp_path)


def test_main_resolves_compiler_version_before_manager_and_worker_setup():
    script = """
import hephaestus

calls = []


def version_command(arguments):
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


hephaestus.cfg.prob.crossmodule_probability = 0.0
hephaestus.run_command = version_command
hephaestus.mp.Pool = Pool
hephaestus.setup_live_jacoco = lambda: None
hephaestus._run = lambda program, results, version: calls.append(
    ("run", version))
hephaestus.cli_args.debug = False
hephaestus.cli_args.workers = 1

hephaestus.main()

assert [call[0] for call in calls] == ["version", "pool", "run"]
assert calls[1][1] == ("test compiler",)
assert calls[2][1] == "test compiler"
assert hephaestus.CROSSMODULE_MANAGER.compiler_version == "test compiler"
"""

    _run_python(script)


def test_provider_publication_reruns_the_correct_source_after_ncp_batch():
    script = """
import os
import tempfile
from collections import OrderedDict
from pathlib import Path

import hephaestus


class Manager:
    def __init__(self):
        self.published = []

    @staticmethod
    def dependency_klibs(proc_res):
        return []

    @staticmethod
    def apply_drop_knob(klibs):
        return klibs, []

    def publish_provider(self, pid, source_klib, command_args, direct, dropped):
        assert Path(source_klib).read_text() == "isolated KLIB"
        self.published.append((pid, source_klib, command_args, direct, dropped))
        return {"direct": direct, "closure": []}


class Compiler:
    def __init__(self, input_name, filter_patterns=None, dependency_klibs=None,
                 friend_klibs=None, module_name=None):
        self.input_name = input_name
        self.module_name = module_name
        self.crash_msg = None

    def get_compiler_cmd(self):
        return ["fake-compiler", self.input_name]

    def get_klib_filename(self):
        return self.module_name + ".klib"

    @staticmethod
    def analyze_compiler_output(output):
        return ({str(incorrect_program): ["expected NCP error"]}
                if output else {}), []


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

    def run_command(command, get_stdout=True, cwd=None):
        calls.append((command, cwd))
        klib = Path(cwd) / "d1.klib"
        if len(calls) == 1:
            klib.write_text("combined KLIB")
            return False, "expected NCP error"
        assert not klib.exists()
        klib.write_text("isolated KLIB")
        return True, ""

    def timed(fn, command, **kwargs):
        duration = 1.0 if command[1] == str(src_dir) else 2.0
        return fn(command, **kwargs), duration

    manager = Manager()
    hephaestus.CROSSMODULE_MANAGER = manager
    hephaestus.COMPILERS["kotlin"] = Compiler
    hephaestus.run_command = run_command
    hephaestus.timed = timed
    hephaestus.cfg.prob.crossmodule_probability = 0.5
    hephaestus.cli_args.language = "kotlin"
    hephaestus.cli_args.keep_everything = False
    hephaestus.cli_args.time_metrics = False
    hephaestus.cli_args.debug = False
    hephaestus.cli_args.rerun = False

    stats = {
        "error": None,
        "programs": {
            str(correct_program): True,
            str(incorrect_program): False,
        },
        "time": 0.0,
    }
    result = hephaestus.check_oracle(
        str(batch_dir), OrderedDict([(1, hephaestus.ProgramRes(False, stats))]))

    assert result[0] == {}
    assert result[1] == 3.0
    assert list(result[3]) == [1]
    assert [command[1] for command, _ in calls] == [
        str(src_dir), str(correct_program)]
    assert len(manager.published) == 1
"""

    _run_python(script)