import argparse
import json
import os
import shutil

from src.tools.ir_csv import (
    IR_CHANGES_FILENAME,
    PHASES,
    phase_column,
    saved_program_ids,
)


def read_without_first_line(path):
    with open(path, "rb") as f:
        f.readline()
        return f.read()


def collect_ir_dump_files(ir_dir):
    files = {}
    if not os.path.isdir(ir_dir):
        return files
    for root, _, filenames in os.walk(ir_dir):
        for filename in filenames:
            if filename.endswith(".ir"):
                files.setdefault(filename, os.path.join(root, filename))
    return files


def phase_changed(ir_files, phase_no, phase_name):
    before = ir_files.get(f"{phase_no}_BEFORE.{phase_name}.ir")
    after = ir_files.get(f"{phase_no}_AFTER.{phase_name}.ir")
    if before is None or after is None:
        return None
    return (
        1
        if read_without_first_line(before) != read_without_first_line(after)
        else 0
    )


def summarize_ir_dump_dir(ir_dir):
    ir_files = collect_ir_dump_files(ir_dir)
    return {
        phase_column(phase_no, phase_name): phase_changed(
            ir_files, phase_no, phase_name)
        for phase_no, phase_name in PHASES
    }


def write_program_ir_changes(program_dir, ir_dir=None):
    ir_dir = ir_dir or os.path.join(program_dir, "ir")
    os.makedirs(program_dir, exist_ok=True)
    out_file = os.path.join(program_dir, IR_CHANGES_FILENAME)
    with open(out_file, "w") as f:
        json.dump(summarize_ir_dump_dir(ir_dir), f, indent=2)
    return out_file


def write_run_ir_changes(run_dir, delete_ir=False):
    out_files = []
    for pid in saved_program_ids(run_dir):
        program_dir = os.path.join(run_dir, str(pid))
        out_files.append(write_program_ir_changes(program_dir))
        ir_dir = os.path.join(program_dir, "ir")
        if delete_ir and os.path.isdir(ir_dir):
            shutil.rmtree(ir_dir)
    return out_files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir")
    parser.add_argument(
        "--delete-ir",
        action="store_true",
        help=("Delete each program's raw ir/ directory after writing "
              "ir_changes.json"))
    args = parser.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    out_files = write_run_ir_changes(run_dir, delete_ir=args.delete_ir)
    print("wrote {} {}".format(
        len(out_files),
        IR_CHANGES_FILENAME
        if len(out_files) == 1
        else IR_CHANGES_FILENAME + " files"))


if __name__ == "__main__":
    main()
