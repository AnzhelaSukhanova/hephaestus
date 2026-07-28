import json
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

BACKEND_REQUIREMENTS = {
    "native": [
        Path.home() / "kotlin/kotlin-native/dist/bin/kotlinc-native",
    ],
    "wasm": [
        Path.home() / "kotlin/dist/kotlinc/bin/kotlinc-wasm",
        Path.home()
        / "kotlin/libraries/stdlib/build/libs/"
        / "kotlin-stdlib-wasm-js-2.4.255-SNAPSHOT.klib",
    ],
}

SUBPROCESS_SCRIPT = r"""
import json
import sys
from collections import OrderedDict
from pathlib import Path

run_dir = Path(sys.argv[1])
backend = sys.argv[2]
fixture_file = Path(sys.argv[3])
repo_root = Path(sys.argv[4])
language_flag = sys.argv[5]

sys.path.insert(0, str(repo_root))
sys.argv = [
    "hephaestus.py",
    "--language", "kotlin",
    "--backend", backend,
    "--iterations", "1",
    "--transformations", "0",
    "--bugs", str(run_dir.parent),
    "--name", run_dir.name,
    "--dump-ir",
    "--time-metrics",
]

import hephaestus

batch_dir = run_dir / "batch"
src_dir = batch_dir / "src"
src_dir.mkdir(parents=True)
program_file = src_dir / "program.kt"
program_text = fixture_file.read_text()
program_file.write_text(program_text)

tmp_program_dir = run_dir / "tmp" / "1"
tmp_program_dir.mkdir(parents=True)
(tmp_program_dir / "program.kt").write_text(program_text)

reference_compiler_cmd = hephaestus.KotlinCompiler(str(src_dir)).get_compiler_cmd()
original_get_compiler_cmd = hephaestus.KotlinCompiler._get_compiler_cmd


def patched_get_compiler_cmd(self, input_name):
    return original_get_compiler_cmd(self, input_name) + [language_flag]


hephaestus.KotlinCompiler._get_compiler_cmd = patched_get_compiler_cmd

compiler_cmd = hephaestus.KotlinCompiler(str(src_dir)).get_compiler_cmd()
assert compiler_cmd.count(language_flag) == 1, compiler_cmd
compiler_cmd_without_language_flag = list(compiler_cmd)
compiler_cmd_without_language_flag.remove(language_flag)
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

stats = {
    "transformations": [],
    "error": None,
    "programs": {
        str(program_file): True,
    },
    "time": 0.0,
}
oracles = OrderedDict([(1, hephaestus.ProgramRes(False, stats))])
result = hephaestus.check_oracle(str(batch_dir), oracles)
hephaestus.update_stats(result, 1, 0, {})

print(json.dumps({"compiler_cmd": compiler_cmd, "run_dir": str(run_dir)}))
"""


def skip_if_backend_unavailable(backend):
    missing = [path for path in BACKEND_REQUIREMENTS[backend] if not path.exists()]
    if missing:
        pytest.skip(
            "{} backend test needs missing file(s): {}".format(
                backend,
                ", ".join(str(path) for path in missing),
            )
        )


def run_hephaestus_fault_capture(tmp_path, backend, fixture_name, source):
    skip_if_backend_unavailable(backend)

    run_dir = tmp_path / "{}-{}".format(backend, fixture_name)
    fixture_file = tmp_path / "{}.kt".format(fixture_name)
    fixture_file.write_text(source)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            SUBPROCESS_SCRIPT,
            str(run_dir),
            backend,
            str(fixture_file),
            str(REPO_ROOT),
            LANGUAGE_FLAG,
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    return run_dir, completed.stdout


@pytest.mark.parametrize("backend", ["native", "wasm"])
@pytest.mark.parametrize(
    "fixture_name,source",
    FIXTURES.items(),
    ids=list(FIXTURES.keys()),
)
def test_kotlin_backend_exception_is_recorded_in_faults_json(
        tmp_path, backend, fixture_name, source):
    run_dir, output = run_hephaestus_fault_capture(
        tmp_path, backend, fixture_name, source)

    faults = json.loads((run_dir / "faults.json").read_text())
    stats = json.loads((run_dir / "stats.json").read_text())
    time_metrics = json.loads((run_dir / "time_metrics.json").read_text())

    assert stats["totals"]["failed"] == 1
    assert stats["totals"]["passed"] == 0
    assert "1" in faults
    assert "1" in time_metrics

    fault = faults["1"]
    assert "time_metrics" in fault
    assert fault["time_metrics"] == time_metrics["1"]

    error = fault["error"]
    assert "org.jetbrains.kotlin.backend.common.CompilationException" in error
    assert "Incomplete expression" in error
    assert "has no argument at index 1" in error

    preserved_program_dir = run_dir / "1"
    assert preserved_program_dir.is_dir()
    assert (preserved_program_dir / "program.kt").read_text() == source
    assert (preserved_program_dir / "ir_changes.json").is_file()
    assert LANGUAGE_FLAG in output
