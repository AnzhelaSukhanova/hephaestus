import argparse
import csv
import os
import sys

from src.tools.outcomes import read_report, read_run_outcomes


BACKEND_PHASES = {"backend", "cb"}
PHASES = [
    ("08", "LocalClassesInInlineLambdasLowering"),
    ("10", "PreSerializationPrivateFunctionInlining"),
    ("12", "OuterThisInInlineFunctionsSpecialAccessorLowering"),
    ("13", "SyntheticAccessorLowering"),
    ("15", "FunctionInlining"),
    ("16", "InlineFunctionSerializationPreProcessing"),
    ("17", "RedundantCastsRemoverLowering"),
]
DEFAULT_OUTPUT = "ir_phase_changes.csv"
IR_CHANGES_FILENAME = "ir_changes.json"


def saved_program_ids(run_dir):
    ids = []
    for name in os.listdir(run_dir):
        path = os.path.join(run_dir, name)
        if name.isdigit() and os.path.isdir(path):
            ids.append(int(name))
    return sorted(ids)


def program_kind(pid, outcomes):
    outcome = outcomes.outcome(pid)
    if outcome == "inconclusive":
        return "inconclusive"
    if outcome != "fault":
        return "successful"
    fault = outcomes.fault(pid)
    if fault.get("error_phase") in BACKEND_PHASES:
        return "backend_fault"
    return None


def phase_column(phase_no, phase_name):
    return f"{phase_no}_{phase_name}"


def read_ir_changes(program_dir):
    changes_file = os.path.join(program_dir, IR_CHANGES_FILENAME)
    if not os.path.exists(changes_file):
        raise SystemExit(
            "Missing IR change summary: {}\n"
            "Run Hephaestus with --dump-ir for new runs or migrate old raw "
            "dumps with: python -m src.tools.changes_from_ir_dumps <run_dir>"
            .format(changes_file))
    changes = read_report(changes_file)
    if not isinstance(changes, dict):
        raise SystemExit(
            "Invalid IR change summary, expected JSON object: {}".format(
                changes_file))
    return changes_file, changes


def csv_phase_value(changes_file, changes, column):
    if column not in changes:
        raise SystemExit(
            "Missing phase column {} in {}".format(column, changes_file))
    value = changes[column]
    if value is None:
        return "None"
    if value in (0, 1):
        return str(value)
    if value in ("0", "1", "None"):
        return value
    raise SystemExit(
        "Invalid value for {} in {}: {!r}".format(
            column, changes_file, value))


def iter_rows(run_dir, outcomes):
    for pid in saved_program_ids(run_dir):
        kind = program_kind(pid, outcomes)
        if kind is None:
            continue

        fault = outcomes.fault(pid) or {}
        program_dir = os.path.join(run_dir, str(pid))
        changes_path = os.path.join(program_dir, IR_CHANGES_FILENAME)
        if kind == "inconclusive" and not os.path.exists(changes_path):
            print(
                "Skipping inconclusive program {}: missing IR change summary {}"
                .format(pid, changes_path),
                file=sys.stderr,
            )
            continue
        changes_file, changes = read_ir_changes(program_dir)
        row = {
            "program_id": pid,
            "program_kind": kind,
            "error_phase": fault.get("error_phase", "") if fault else "",
        }
        for phase_no, phase_name in PHASES:
            column = phase_column(phase_no, phase_name)
            row[column] = csv_phase_value(changes_file, changes, column)
        yield row


def write_phase_changes_csv(run_dir, out_file=None):
    outcomes = read_run_outcomes(run_dir)
    out_file = out_file or os.path.join(run_dir, DEFAULT_OUTPUT)
    fieldnames = [
        "program_id",
        "program_kind",
        "error_phase",
    ] + [
        phase_column(phase_no, phase_name)
        for phase_no, phase_name in PHASES
    ]

    with open(out_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(iter_rows(run_dir, outcomes))

    return out_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", choices=["phase_changes"])
    parser.add_argument("run_dir")
    parser.add_argument("-o", "--output")
    args = parser.parse_args()

    run_dir = os.path.abspath(args.run_dir)

    if args.csv == "phase_changes":
        out_file = write_phase_changes_csv(run_dir, args.output)
        print(out_file)


if __name__ == "__main__":
    main()
