import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LANGUAGE_FLAG = "-XXLanguage:-ForbidOverriddenDefaultParametersInInline"

FIXTURES = {
    "direct_inherited_default": textwrap.dedent(
        """
        interface I {
            abstract fun foo(a: Int = 42): Int
        }

        class A(): I {
            inline override fun foo(a: Int): Int = -42
        }

        fun bar() = A().foo()
        """
    ).strip()
    + "\n",
    "transitive_inherited_default": textwrap.dedent(
        """
        abstract class A {
            abstract fun foo(a: Byte = 42)
        }

        open class B(): A() {
            open override fun foo(a: Byte) {}
        }

        class C: B() {
            inline override fun foo(a: Byte) {}
        }

        fun bar() = C().foo()
        """
    ).strip()
    + "\n",
}

SUBPROCESS_SCRIPT = r"""
import json
import os
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
oracle = sys.argv[2]
backend = sys.argv[3]
fixture_file = Path(sys.argv[4])
repo_root = Path(sys.argv[5])
language_flag = sys.argv[6]

sys.path.insert(0, str(repo_root))
import hephaestus
from src.compilers.kotlin import KotlinCompiler
from src.modules.models import ProgramRes

# One standalone program per run: the crash must not depend on cross-module
# generation or provider publication.
generator_config = run_dir.parent / (run_dir.name + "-generator.json")
generator_config.write_text(json.dumps({"prob": {"crossmodule_probability": 0}}))
arguments = [
    "--language", "kotlin",
    "-P",
    "--oracle", oracle,
    "--iterations", "1",
    "--transformations", "0",
    "--bugs", str(run_dir.parent),
    "--name", run_dir.name,
    "--generator-config", str(generator_config),
    "--time-metrics",
]
if oracle == "compilation":
    # Backend comparison selects its own targets and rejects --dump-ir.
    arguments += ["--backend", backend, "--dump-ir"]
options = hephaestus.parse_args(arguments)
hephaestus.configure_generator(options)
fuzzer = hephaestus.Fuzzer(options)
program_text = fixture_file.read_text()


def reference_commands(src_dir):
    if not fuzzer.mode.compares_variants():
        return {"compilation": fuzzer.compilation.make_compiler(src_dir).get_compiler_cmd()}
    return {
        variant_id: fuzzer.compilation.make_compiler(
            src_dir, module_name="d1",
            output_directory=os.path.join(os.path.dirname(src_dir), "variants", variant_id),
            klib_output=True,
            settings=settings,
        ).get_compiler_cmd()
        for variant_id, settings in fuzzer.compilation.settings_by_variant.items()
    }


original_get_compiler_cmd = KotlinCompiler._get_compiler_cmd


def patched_get_compiler_cmd(self, input_name):
    return original_get_compiler_cmd(self, input_name) + [language_flag]


executed = []
run_process = fuzzer.compilation.run_process


def recording_run_process(arguments, *args, **kwargs):
    executed.append(list(arguments))
    return run_process(arguments, *args, **kwargs)


fuzzer.compilation.run_process = recording_run_process
expected = {}


def generate_fixture(pid, dirname):
    assert pid == 1, pid
    manager = fuzzer.manager
    assert manager is not None
    assert manager is fuzzer.setup_crossmodule_manager("other compiler")
    assert manager.compiler_version == fuzzer.reporting.stats["Info"]["compiler"]
    program_file = Path(dirname) / "d1" / "program.kt"
    program_file.parent.mkdir(parents=True)
    program_file.write_text(program_text)
    tmp_program_dir = Path(fuzzer.options.test_directory) / "tmp" / "1"
    tmp_program_dir.mkdir(parents=True)
    (tmp_program_dir / "program.kt").write_text(program_text)
    expected.update(reference_commands(dirname))
    # Disabling the frontend restriction lets the fixture reach the backend.
    # Startup probes ran without it, so only program compilation is affected.
    KotlinCompiler._get_compiler_cmd = patched_get_compiler_cmd
    return ProgramRes(False, {
        "transformations": [],
        "error": None,
        "programs": {str(program_file): True},
        "time": 0.0,
    })


fuzzer.generation.gen_program = generate_fixture
fuzzer.start()

print(json.dumps({"executed": executed, "expected": expected}))
"""


def requirements(oracle, backend):
    from src.compilers.kotlin import (
        KotlinCompiler, KotlinSettings, _compiler_executable,
    )
    from src.modules.oracle_modes import BACKEND_COMPARISON_VARIANTS

    if oracle == "compilation":
        settings = [KotlinSettings(backend=backend)]
    else:
        settings = list(BACKEND_COMPARISON_VARIANTS.values())
    paths = []
    for item in settings:
        paths.append(_compiler_executable(item))
        # Native commands do not name the standard library explicitly.
        if oracle != "compilation" or item.backend != "native":
            paths.append(KotlinCompiler("", settings=item).stdlib_path())
    return [Path(os.path.expandvars(path)) for path in paths]


def skip_if_backend_unavailable(oracle, backend):
    missing = [path for path in requirements(oracle, backend) if not path.exists()]
    if missing:
        pytest.skip(
            "{} {} test needs missing file(s): {}".format(
                oracle, backend,
                ", ".join(str(path) for path in missing),
            )
        )


def run_hephaestus_fault_capture(tmp_path, oracle, backend, fixture_name, source):
    skip_if_backend_unavailable(oracle, backend)

    run_dir = tmp_path / "{}-{}-{}".format(oracle, backend, fixture_name)
    fixture_file = tmp_path / "{}.kt".format(fixture_name)
    fixture_file.write_text(source)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            SUBPROCESS_SCRIPT,
            str(run_dir),
            oracle,
            backend,
            str(fixture_file),
            str(REPO_ROOT),
            LANGUAGE_FLAG,
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=300,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    commands = json.loads(completed.stdout.splitlines()[-1])
    return run_dir, completed.stdout, commands


def assert_backend_exception(error):
    assert "org.jetbrains.kotlin.backend.common.CompilationException" in error
    assert "Incomplete expression" in error
    assert "has no argument at index 1" in error


def assert_crash_accounting(run_dir):
    faults = json.loads((run_dir / "faults.json").read_text())
    stats = json.loads((run_dir / "stats.json").read_text())
    time_metrics = json.loads((run_dir / "time_metrics.json").read_text())

    assert stats["totals"] == {"passed": 0, "failed": 1, "inconclusive": 0}
    assert json.loads((run_dir / "inconclusive.json").read_text()) == {}
    assert set(faults) == {"1"}
    assert "1" in time_metrics

    fault = faults["1"]
    assert "time_metrics" in fault
    assert fault["time_metrics"] == time_metrics["1"]
    assert_backend_exception(fault["error"])
    assert not (run_dir / "tmp").exists()
    return fault


@pytest.mark.parametrize("backend", ["native", "wasm"])
@pytest.mark.parametrize(
    "fixture_name,source",
    FIXTURES.items(),
    ids=list(FIXTURES.keys()),
)
def test_kotlin_backend_exception_is_recorded_in_faults_json(
        tmp_path, backend, fixture_name, source):
    run_dir, output, commands = run_hephaestus_fault_capture(
        tmp_path, "compilation", backend, fixture_name, source)

    reference_compiler_cmd = commands["expected"]["compilation"]
    # The startup version probe is unchanged; the program compiles with the flag.
    version_cmd, compiler_cmd = commands["executed"]
    assert LANGUAGE_FLAG not in version_cmd
    assert compiler_cmd.count(LANGUAGE_FLAG) == 1, compiler_cmd
    compiler_cmd_without_language_flag = list(compiler_cmd)
    compiler_cmd_without_language_flag.remove(LANGUAGE_FLAG)
    assert compiler_cmd_without_language_flag == reference_compiler_cmd, compiler_cmd
    assert any(arg.startswith("-Xphases-to-dump=") for arg in compiler_cmd), compiler_cmd
    assert any(arg.startswith("-Xdump-directory=") for arg in compiler_cmd), compiler_cmd
    assert "-nowarn" in compiler_cmd, compiler_cmd

    if backend == "native":
        assert "-produce" in compiler_cmd, compiler_cmd
        assert "library" in compiler_cmd, compiler_cmd
        assert "-o" in compiler_cmd, compiler_cmd
    else:
        assert "-ir-output-dir" in compiler_cmd, compiler_cmd
        assert "-ir-output-name" in compiler_cmd, compiler_cmd
        assert "-libraries" in compiler_cmd, compiler_cmd

    assert_crash_accounting(run_dir)

    preserved_program_dir = run_dir / "1"
    assert preserved_program_dir.is_dir()
    assert (preserved_program_dir / "program.kt").read_text() == source
    assert (preserved_program_dir / "ir_changes.json").is_file()
    assert LANGUAGE_FLAG in output


@pytest.mark.parametrize(
    "fixture_name,source",
    FIXTURES.items(),
    ids=list(FIXTURES.keys()),
)
def test_kotlin_backend_comparison_records_backend_exceptions(
        tmp_path, fixture_name, source):
    run_dir, output, commands = run_hephaestus_fault_capture(
        tmp_path, "backend-comparison", "native", fixture_name, source)

    expected = commands["expected"]
    assert set(expected) == {"js", "native", "wasm", "jvm"}
    executed = commands["executed"]
    # Version probes and the fixture preflight run without the language flag.
    assert not any(LANGUAGE_FLAG in command for command in executed[:-4])
    assert executed[-4:] == [expected[variant_id] + [LANGUAGE_FLAG]
                             for variant_id in ("js", "native", "wasm", "jvm")]

    fault = assert_crash_accounting(run_dir)
    assert fault["oracle"] == "backend-comparison"
    comparison = fault["comparison"]
    crashes = {finding["variants"][0]: finding for finding in comparison["findings"]
               if finding["kind"] == "crash"}
    # Native and Wasm crash in the backend; their crash findings are recorded
    # even though crash output leaves the diagnostics incompletely parsed.
    assert {"native", "wasm"} <= set(crashes)
    assert set(comparison["groups"]["crashed"]) == set(crashes)
    for variant_id in ("native", "wasm"):
        assert_backend_exception(crashes[variant_id]["diagnostics"][0])

    preserved_program_dir = run_dir / "1"
    assert (preserved_program_dir / "program.kt").read_text() == source
    retained = json.loads((preserved_program_dir / "comparison.json").read_text())
    assert retained["comparison"] == comparison
    for variant_id in ("native", "wasm"):
        result = json.loads(
            (preserved_program_dir / "variants" / variant_id / "result.json").read_text())
        assert result["backend"] == variant_id
        assert_backend_exception(result["crash"])
        assert result["returncode"] != 0
        # The crash output is not fully parsed; the crash still decides the verdict.
        assert result["parsing_complete"] is False
        assert LANGUAGE_FLAG in result["original_command"]
    assert LANGUAGE_FLAG in output
