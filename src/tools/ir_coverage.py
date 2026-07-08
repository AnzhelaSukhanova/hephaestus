import argparse
import csv
import json
import os
import posixpath
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from src.tools.ir_csv import DEFAULT_OUTPUT, PHASES, phase_column


BACKENDS = {"native", "js", "wasm"}
REPORT_CLASSES = [
    "InlineCallCycleCheckerLowering",
    "LocalClassesInInlineLambdasLowering",
    "PreSerializationPrivateFunctionInlining",
    "InlineDeclarationCheckerLowering",
    "OuterThisInInlineFunctionsSpecialAccessorLowering",
    "CommonLoweringPhasesKt",
    "SyntheticAccessorLowering",
    "IrValidationAfterInliningOnlyPrivateFunctionsPhase",
    "FunctionInlining",
    "InlineFunctionSerializationPreProcessing",
    "IrValidationAfterInliningAllFunctionsOnTheFirstStagePhase",
    "RedundantCastsRemoverLowering",
]
COMMON_CLASS_PREFIXES = [
    "org/jetbrains/kotlin/backend/common/lower/",
    "org/jetbrains/kotlin/backend/common/lower/inline/",
    "org/jetbrains/kotlin/ir/inline/",
    "org/jetbrains/kotlin/backend/common/phaser/",
]
NATIVE_CLASS_PREFIXES = [
    "org/jetbrains/kotlin/backend/konan/lower/",
    "org/jetbrains/kotlin/backend/konan/driver/phases/",
]
WASM_CLASS_PREFIXES = [
    "org/jetbrains/kotlin/backend/wasm/",
]
COMMON_JACOCO_INCLUDES = (
    "org/jetbrains/kotlin/backend/common/lower/*:"
    "org/jetbrains/kotlin/backend/common/lower/inline/*:"
    "org/jetbrains/kotlin/ir/inline/*:"
    "org/jetbrains/kotlin/backend/common/phaser/*"
)


def repo_root():
    return Path(__file__).resolve().parents[2]


def expand_arg(arg):
    return os.path.expanduser(os.path.expandvars(str(arg)))


def expand_cmd(cmd):
    return [expand_arg(arg) for arg in cmd]


def read_json(path):
    with open(path) as f:
        return json.load(f)


def infer_backend(run_dir):
    stats_file = run_dir / "stats.json"
    if not stats_file.exists():
        raise SystemExit("Cannot infer backend: stats.json is missing")

    compiler_info = read_json(stats_file).get("Info", {}).get("compiler", "")
    if "kotlinc-native" in compiler_info:
        return "native"
    if "kotlinc-js" in compiler_info:
        return "js"
    if "kotlinc-wasm" in compiler_info:
        return "wasm"
    raise SystemExit(
        "Cannot infer backend from stats.json compiler field; pass --backend")


def lowering_columns(lowerings):
    valid = {
        phase_name: phase_column(phase_no, phase_name)
        for phase_no, phase_name in PHASES
    }
    valid.update({
        phase_column(phase_no, phase_name): phase_column(phase_no, phase_name)
        for phase_no, phase_name in PHASES
    })

    unknown = [lowering for lowering in lowerings if lowering not in valid]
    if unknown:
        valid_names = ", ".join(sorted(valid))
        raise SystemExit(
            "Unknown lowering(s): {}. Valid lowerings: {}".format(
                ", ".join(unknown), valid_names))

    columns = []
    for lowering in lowerings:
        column = valid[lowering]
        if column not in columns:
            columns.append(column)
    return columns


def default_output_dir(run_dir, columns):
    suffix = "_".join(columns)
    return run_dir / ("coverage_" + suffix)


def prepare_output_dir(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("jacoco.exec", "report.xml", "report.csv",
                     "selected_programs.csv"):
        path = output_dir / filename
        if path.exists():
            path.unlink()
    for dirname in ("html", "logs"):
        path = output_dir / dirname
        if path.exists():
            shutil.rmtree(path)
    (output_dir / "logs").mkdir()


def read_phase_csv(csv_file):
    if not csv_file.exists():
        raise SystemExit("Missing IR phase CSV: {}".format(csv_file))
    with open(csv_file, newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames or [], list(reader)


def ensure_default_phase_csv(run_dir, csv_file, explicit_csv):
    if csv_file.exists() or explicit_csv:
        return
    raise SystemExit(
        "Missing IR phase CSV: {}\n"
        "Create it explicitly with: python -m src.tools.ir_csv "
        "phase_changes {}".format(csv_file, run_dir))


def select_rows(fieldnames, rows, columns):
    missing_columns = [column for column in columns
                       if column not in fieldnames]
    if missing_columns:
        raise SystemExit(
            "Missing column(s) in IR phase CSV: {}".format(
                ", ".join(missing_columns)))
    return [
        row for row in rows
        if any(row.get(column) == "1" for column in columns)
    ]


def selected_program_path(run_dir, row):
    return run_dir / row["program_id"] / "program.kt"


def validate_selected_programs(run_dir, rows):
    missing = [
        str(selected_program_path(run_dir, row))
        for row in rows
        if not selected_program_path(run_dir, row).exists()
    ]
    if missing:
        preview = "\n".join(missing[:20])
        suffix = "" if len(missing) <= 20 else "\n..."
        raise SystemExit("Missing selected program.kt file(s):\n" +
                         preview + suffix)


def hephaestus_compiler(backend):
    is_native = backend == "native"
    base = "kotlin-native/dist" if is_native else "dist/kotlinc"
    return f"$HOME/kotlin/{base}/bin/kotlinc-{backend}"


def hephaestus_compile_cmd(backend, input_name):
    compiler = hephaestus_compiler(backend)
    if backend == "native":
        return [
            compiler,
            input_name,
            "-produce",
            "library",
            "-o",
            input_name,
            "-nowarn",
        ]

    is_wasm = backend == "wasm"
    stdlib = (
        "$HOME/kotlin/libraries/stdlib/build/libs/"
        f"kotlin-stdlib-{'wasm-' if is_wasm else ''}js-"
        "2.4.255-SNAPSHOT.klib"
    )
    return [
        compiler,
        input_name,
        "-ir-output-dir",
        input_name,
        "-ir-output-name",
        "src",
        "-libraries",
        stdlib,
        "-nowarn",
    ]


def jacoco_includes(backend):
    if backend == "native":
        return COMMON_JACOCO_INCLUDES + ":org/jetbrains/kotlin/backend/konan/*"
    if backend == "wasm":
        return COMMON_JACOCO_INCLUDES + ":org/jetbrains/kotlin/backend/wasm/*"
    return COMMON_JACOCO_INCLUDES


def jacoco_agent_arg(output_dir, backend):
    agent = repo_root() / "coverage" / "jacocoagent.jar"
    if not agent.exists():
        raise SystemExit("Missing JaCoCo agent jar: {}".format(agent))
    return (
        "-J-javaagent:{}=destfile={},append=true,"
        "inclnolocationclasses=true,includes={}"
    ).format(agent, output_dir / "jacoco.exec", jacoco_includes(backend))


def coverage_compile_cmd(backend, input_name, output_dir):
    return hephaestus_compile_cmd(backend, input_name) + [
        jacoco_agent_arg(output_dir, backend)
    ]


def run_command(cmd, log_file, timeout):
    expanded_cmd = expand_cmd(cmd)
    with open(log_file, "w") as log:
        log.write("$ " + " ".join(expanded_cmd) + "\n\n")
        try:
            proc = subprocess.run(
                expanded_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            log.write(proc.stdout)
            return proc.returncode
        except subprocess.TimeoutExpired as exc:
            if exc.stdout:
                log.write(exc.stdout)
            log.write("\nTimed out after {} seconds\n".format(timeout))
            return 124


def stage_program(tmp_dir, row, program_file):
    stage_dir = tmp_dir / "programs" / row["program_id"] / "src"
    stage_dir.mkdir(parents=True)
    shutil.copy2(program_file, stage_dir / "program.kt")
    return stage_dir


def compiler_jar(backend):
    if backend == "native":
        return Path(expand_arg(
            "$HOME/kotlin/kotlin-native/dist/konan/lib/"
            "kotlin-native-compiler-embeddable.jar"))
    return Path(expand_arg("$HOME/kotlin/dist/kotlinc/lib/kotlin-compiler.jar"))


def class_prefixes(backend):
    prefixes = list(COMMON_CLASS_PREFIXES)
    if backend == "native":
        prefixes.extend(NATIVE_CLASS_PREFIXES)
    if backend == "wasm":
        prefixes.extend(WASM_CLASS_PREFIXES)
    return prefixes


def should_extract_class(path, backend):
    if not path.endswith(".class"):
        return False
    if not any(path.startswith(prefix) for prefix in class_prefixes(backend)):
        return False

    stem = posixpath.basename(path)[:-len(".class")]
    return any(stem == cls or stem.startswith(cls + "$")
               for cls in REPORT_CLASSES)


def extract_report_classes(backend, classes_dir):
    jar_file = compiler_jar(backend)
    if not jar_file.exists():
        raise SystemExit("Missing Kotlin compiler jar: {}".format(jar_file))

    extracted = 0
    with zipfile.ZipFile(jar_file) as jar:
        for path in jar.namelist():
            if should_extract_class(path, backend):
                jar.extract(path, classes_dir)
                extracted += 1

    if extracted == 0:
        raise SystemExit("No report class files extracted from {}".format(
            jar_file))


def sourcefile_args(backend):
    paths = [
        "$HOME/kotlin/compiler/ir/backend.common/src",
        "$HOME/kotlin/compiler/ir/ir.inline/src",
    ]
    if backend == "native":
        paths.append(
            "$HOME/kotlin/kotlin-native/backend.native/compiler/ir/"
            "backend.native/src")
    if backend == "wasm":
        paths.append("$HOME/kotlin/compiler/ir/backend.wasm/src")

    args = []
    for path in paths:
        expanded = expand_arg(path)
        if os.path.exists(expanded):
            args.extend(["--sourcefiles", expanded])
    return args


def write_manifest(output_dir, rows, columns):
    manifest = output_dir / "selected_programs.csv"
    fieldnames = [
        "program_id",
        "program_kind",
        "error_phase",
        "program_path",
        "log_path",
        "compile_returncode",
    ] + columns
    with open(manifest, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    return manifest


def write_jacoco_report(output_dir, classes_dir, backend):
    cli = repo_root() / "coverage" / "jacococli.jar"
    exec_file = output_dir / "jacoco.exec"
    if not cli.exists():
        raise SystemExit("Missing JaCoCo CLI jar: {}".format(cli))
    if not exec_file.exists():
        raise SystemExit("No JaCoCo exec file was produced: {}".format(
            exec_file))

    cmd = [
        "java",
        "-jar",
        str(cli),
        "report",
        str(exec_file),
        "--classfiles",
        str(classes_dir),
    ] + sourcefile_args(backend) + [
        "--html",
        str(output_dir / "html"),
        "--xml",
        str(output_dir / "report.xml"),
        "--csv",
        str(output_dir / "report.csv"),
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    with open(output_dir / "jacoco-report.log", "w") as log:
        log.write("$ " + " ".join(cmd) + "\n\n")
        log.write(proc.stdout)
    if proc.returncode != 0:
        raise SystemExit("JaCoCo report generation failed; see {}".format(
            output_dir / "jacoco-report.log"))


def run_coverage(run_dir, columns, backend, csv_file, output_dir, timeout):
    fieldnames, phase_rows = read_phase_csv(csv_file)
    rows = select_rows(fieldnames, phase_rows, columns)
    validate_selected_programs(run_dir, rows)
    prepare_output_dir(output_dir)

    with tempfile.TemporaryDirectory(prefix="hephaestus-ir-coverage-") as tmp:
        tmp_dir = Path(tmp)
        for row in rows:
            program_file = selected_program_path(run_dir, row)
            log_file = output_dir / "logs" / (row["program_id"] + ".log")
            stage_dir = stage_program(tmp_dir, row, program_file)
            returncode = run_command(
                coverage_compile_cmd(backend, str(stage_dir), output_dir),
                log_file,
                timeout,
            )
            row["program_path"] = str(program_file)
            row["log_path"] = str(log_file)
            row["compile_returncode"] = str(returncode)

        manifest = write_manifest(output_dir, rows, columns)
        if not rows:
            return {
                "selected": 0,
                "output_dir": str(output_dir),
                "manifest": str(manifest),
                "report": None,
            }

        classes_dir = tmp_dir / "classes"
        classes_dir.mkdir()
        extract_report_classes(backend, classes_dir)
        write_jacoco_report(output_dir, classes_dir, backend)

    return {
        "selected": len(rows),
        "output_dir": str(output_dir),
        "manifest": str(manifest),
        "report": str(output_dir / "report.csv"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["changed"])
    parser.add_argument("run_dir")
    parser.add_argument("lowerings", nargs="+")
    parser.add_argument("--backend", choices=sorted(BACKENDS))
    parser.add_argument("--csv", dest="csv_file")
    parser.add_argument("--output-dir")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    backend = args.backend or infer_backend(run_dir)
    columns = lowering_columns(args.lowerings)
    csv_file = Path(args.csv_file).resolve() if args.csv_file else (
        run_dir / DEFAULT_OUTPUT)
    ensure_default_phase_csv(run_dir, csv_file, bool(args.csv_file))
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (
        default_output_dir(run_dir, columns))

    if args.mode == "changed":
        result = run_coverage(
            run_dir, columns, backend, csv_file, output_dir, args.timeout)
        print("selected: {}".format(result["selected"]))
        print("output_dir: {}".format(result["output_dir"]))
        print("manifest: {}".format(result["manifest"]))
        if result["report"]:
            print("report: {}".format(result["report"]))


if __name__ == "__main__":
    main()
