import argparse
import csv
import shutil
import subprocess
import tempfile
from pathlib import Path

from src.tools.ir_csv import DEFAULT_OUTPUT
from src.tools.ir_coverage import (
    BACKENDS,
    default_output_dir,
    ensure_default_phase_csv,
    expand_arg,
    extract_report_classes,
    hephaestus_compile_cmd,
    infer_backend,
    lowering_columns,
    prepare_output_dir,
    read_phase_csv,
    repo_root,
    run_command,
    select_rows,
    selected_program_path,
    stage_program,
    validate_selected_programs,
)


def kover_default_output_dir(run_dir, columns):
    return default_output_dir(run_dir, columns).with_name(
        "coverage_kover_" + "_".join(columns))


def find_single_jar(pattern, description, explicit_path=None):
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.exists():
            raise SystemExit("Missing {}: {}".format(description, path))
        return path

    candidates = sorted((repo_root() / "coverage").glob(pattern))
    if len(candidates) == 1:
        return candidates[0]

    if not candidates:
        raise SystemExit(
            "Missing {}. Put {} under coverage/ or pass the explicit path."
            .format(description, pattern))

    raise SystemExit(
        "Multiple {} jars found: {}. Pass the explicit path.".format(
            description, ", ".join(str(path) for path in candidates)))


def prepare_kover_output_dir(output_dir):
    prepare_output_dir(output_dir)
    for filename in ("report.xml", "kover-report.log"):
        path = output_dir / filename
        if path.exists():
            path.unlink()
    for dirname in ("ic", "agent_args"):
        path = output_dir / dirname
        if path.exists():
            shutil.rmtree(path)
        path.mkdir()


def kover_include_regex(backend):
    packages = [
        r"org\.jetbrains\.kotlin\.backend\.common\.lower(\..*)?",
        r"org\.jetbrains\.kotlin\.backend\.common\.phaser\..*",
        r"org\.jetbrains\.kotlin\.ir\.inline\..*",
    ]
    if backend == "native":
        packages.append(r"org\.jetbrains\.kotlin\.backend\.konan\..*")
    if backend == "wasm":
        packages.append(r"org\.jetbrains\.kotlin\.backend\.wasm\..*")
    return "^(" + "|".join(packages) + ")$"


def write_agent_args(output_dir, row, backend):
    report_file = output_dir / "ic" / (row["program_id"] + ".ic")
    args_file = output_dir / "agent_args" / (row["program_id"] + ".args")
    with open(args_file, "w") as f:
        f.write("report.file={}\n".format(report_file))
        f.write("report.append=false\n")
        f.write("include.regex={}\n".format(kover_include_regex(backend)))
    return args_file, report_file


def kover_agent_arg(agent_jar, args_file):
    return "-J-javaagent:{}=file:{}".format(agent_jar, args_file)


def kover_compile_cmd(backend, input_name, agent_jar, args_file):
    return hephaestus_compile_cmd(backend, input_name) + [
        kover_agent_arg(agent_jar, args_file)
    ]


def kover_source_args(backend):
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
        if Path(expanded).exists():
            args.extend(["--src", expanded])
    return args


def extract_kover_report_classes(backend, classes_dir):
    # Reuse the JaCoCo class extraction today: both reporters consume class files.
    # Keep this wrapper so Kover-specific class filters can diverge later.
    extract_report_classes(backend, classes_dir)


def write_manifest(output_dir, rows, columns):
    manifest = output_dir / "selected_programs.csv"
    fieldnames = [
        "program_id",
        "program_kind",
        "error_phase",
        "program_path",
        "log_path",
        "ic_path",
        "compile_returncode",
    ] + columns
    with open(manifest, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    return manifest


def write_kover_report(output_dir, classes_dir, backend, ic_files, cli_jar):
    cmd = [
        "java",
        "-jar",
        str(cli_jar),
        "report",
    ] + [str(path) for path in ic_files] + [
        "--classfiles",
        str(classes_dir),
    ] + kover_source_args(backend) + [
        "--html",
        str(output_dir / "html"),
        "--xml",
        str(output_dir / "report.xml"),
    ]

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    with open(output_dir / "kover-report.log", "w") as log:
        log.write("$ " + " ".join(cmd) + "\n\n")
        log.write(proc.stdout)
    if proc.returncode != 0:
        raise SystemExit("Kover report generation failed; see {}".format(
            output_dir / "kover-report.log"))


def run_kover_coverage(run_dir, columns, backend, csv_file, output_dir,
                       timeout, agent_jar, cli_jar):
    fieldnames, phase_rows = read_phase_csv(csv_file)
    rows = select_rows(fieldnames, phase_rows, columns)
    validate_selected_programs(run_dir, rows)
    prepare_kover_output_dir(output_dir)

    with tempfile.TemporaryDirectory(prefix="hephaestus-ir-kover-") as tmp:
        tmp_dir = Path(tmp)
        ic_files = []
        for row in rows:
            program_file = selected_program_path(run_dir, row)
            log_file = output_dir / "logs" / (row["program_id"] + ".log")
            stage_dir = stage_program(tmp_dir, row, program_file)
            args_file, ic_file = write_agent_args(output_dir, row, backend)
            returncode = run_command(
                kover_compile_cmd(
                    backend, str(stage_dir), agent_jar, args_file),
                log_file,
                timeout,
            )
            if ic_file.exists() and ic_file.stat().st_size > 0:
                ic_files.append(ic_file)

            row["program_path"] = str(program_file)
            row["log_path"] = str(log_file)
            row["ic_path"] = str(ic_file)
            row["compile_returncode"] = str(returncode)

        manifest = write_manifest(output_dir, rows, columns)
        if not rows:
            return {
                "selected": 0,
                "ic_files": 0,
                "output_dir": str(output_dir),
                "manifest": str(manifest),
                "report": None,
            }
        if not ic_files:
            raise SystemExit("No non-empty Kover .ic files were produced")

        classes_dir = tmp_dir / "classes"
        classes_dir.mkdir()
        extract_kover_report_classes(backend, classes_dir)
        write_kover_report(output_dir, classes_dir, backend, ic_files, cli_jar)

    return {
        "selected": len(rows),
        "ic_files": len(ic_files),
        "output_dir": str(output_dir),
        "manifest": str(manifest),
        "report": str(output_dir / "report.xml"),
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
    parser.add_argument("--kover-agent")
    parser.add_argument("--kover-cli")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    backend = args.backend or infer_backend(run_dir)
    columns = lowering_columns(args.lowerings)
    csv_file = Path(args.csv_file).resolve() if args.csv_file else (
        run_dir / DEFAULT_OUTPUT)
    ensure_default_phase_csv(run_dir, csv_file, bool(args.csv_file))
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (
        kover_default_output_dir(run_dir, columns))
    agent_jar = find_single_jar(
        "kover-jvm-agent-*.jar", "Kover JVM agent", args.kover_agent)
    cli_jar = find_single_jar("kover-cli-*.jar", "Kover CLI", args.kover_cli)

    if args.mode == "changed":
        result = run_kover_coverage(
            run_dir, columns, backend, csv_file, output_dir, args.timeout,
            agent_jar, cli_jar)
        print("selected: {}".format(result["selected"]))
        print("ic_files: {}".format(result["ic_files"]))
        print("output_dir: {}".format(result["output_dir"]))
        print("manifest: {}".format(result["manifest"]))
        if result["report"]:
            print("report: {}".format(result["report"]))


if __name__ == "__main__":
    main()
