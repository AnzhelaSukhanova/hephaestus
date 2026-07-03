import argparse
import csv
import json
import os
import time


BACKEND_PHASES = {"backend", "cb"}
PHASES = [
    ("06", "LocalClassesInInlineLambdasLowering"),
    ("08", "PreSerializationPrivateFunctionInlining"),
    ("10", "OuterThisInInlineFunctionsSpecialAccessorLowering"),
    ("11", "SyntheticAccessorLowering"),
    ("13", "FunctionInlining"),
    ("14", "InlineFunctionSerializationPreProcessing"),
    ("15", "RedundantCastsRemoverLowering"),
]
DEFAULT_OUTPUT = "ir_phase_changes.csv"


def read_json_retry(path, attempts=20, delay=0.05):
    if not os.path.exists(path):
        return {}

    last_exc = None
    for _ in range(attempts):
        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError as exc:
            last_exc = exc
            time.sleep(delay)
    raise last_exc


def saved_program_ids(run_dir):
    ids = []
    for name in os.listdir(run_dir):
        path = os.path.join(run_dir, name)
        if name.isdigit() and os.path.isdir(path):
            ids.append(int(name))
    return sorted(ids)


def program_kind(pid, faults):
    fault = faults.get(str(pid))
    if fault is None:
        return "successful"
    if fault.get("error_phase") in BACKEND_PHASES:
        return "backend_fault"
    return None


def read_without_first_line(path):
    with open(path, "rb") as f:
        f.readline()
        return f.read()


def phase_changed(ir_dir, phase_no, phase_name):
    before = os.path.join(ir_dir, f"{phase_no}_BEFORE.{phase_name}.ir")
    after = os.path.join(ir_dir, f"{phase_no}_AFTER.{phase_name}.ir")
    if not os.path.exists(before) or not os.path.exists(after):
        return "None"
    return "1" if read_without_first_line(before) != read_without_first_line(after) else "0"


def phase_column(phase_no, phase_name):
    return f"{phase_no}_{phase_name}"


def iter_rows(run_dir, faults):
    for pid in saved_program_ids(run_dir):
        kind = program_kind(pid, faults)
        if kind is None:
            continue

        fault = faults.get(str(pid), {})
        ir_dir = os.path.join(run_dir, str(pid), "ir")
        row = {
            "program_id": pid,
            "program_kind": kind,
            "error_phase": fault.get("error_phase", ""),
        }
        for phase_no, phase_name in PHASES:
            row[phase_column(phase_no, phase_name)] = phase_changed(
                ir_dir, phase_no, phase_name)
        yield row


def write_phase_changes_csv(run_dir, out_file=None):
    faults = read_json_retry(os.path.join(run_dir, "faults.json"))
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
        writer.writerows(iter_rows(run_dir, faults))

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
