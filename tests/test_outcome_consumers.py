import csv
import json
from types import SimpleNamespace

from src.modules.models import OracleResult
from src.modules.oracle_modes import CompilationMode
from src.modules.worker_reporting import Reporting
from src.tools.graph import graph_n_common, n_common_points
from src.tools.ir_coverage import coverage_rows
from src.tools.ir_csv import PHASES, phase_column, write_phase_changes_csv
from src.tools.outcomes import read_run_outcomes


def write_json(path, value):
    path.write_text(json.dumps(value))


def ir_changes(changed_column=None):
    return {
        phase_column(*phase): (
            "1" if phase_column(*phase) == changed_column else "0")
        for phase in PHASES
    }


def test_inconclusive_without_ir_summary_is_skipped_with_explanation(
        tmp_path, capsys):
    run_dir = tmp_path / "run"
    (run_dir / "1").mkdir(parents=True)
    write_json(run_dir / "inconclusive.json", {"1": {"oracle": "compilation"}})

    output = write_phase_changes_csv(str(run_dir))

    with open(output, newline="") as phase_csv:
        rows = list(csv.DictReader(phase_csv))
    assert rows == []
    error = capsys.readouterr().err
    assert "Skipping inconclusive program 1" in error
    assert "missing IR change summary" in error


def test_inconclusive_with_phase_evidence_keeps_its_csv_classification(tmp_path):
    run_dir = tmp_path / "run"
    program_dir = run_dir / "1"
    program_dir.mkdir(parents=True)
    write_json(run_dir / "inconclusive.json", {"1": {"oracle": "compilation"}})
    write_json(program_dir / "ir_changes.json", ir_changes(phase_column(*PHASES[0])))

    output = write_phase_changes_csv(str(run_dir))

    with open(output, newline="") as phase_csv:
        rows = list(csv.DictReader(phase_csv))
    assert rows[0]["program_kind"] == "inconclusive"
    assert rows[0]["error_phase"] == ""
    assert rows[0][phase_column(*PHASES[0])] == "1"


def test_graph_keeps_mixed_outcomes_at_original_program_ids(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stats = {"totals": {"passed": 1, "failed": 1, "inconclusive": 1}}
    faults = {"3": {"error_phase": "backend"}}
    inconclusive = {"2": {"oracle": "compilation"}}
    write_json(run_dir / "stats.json", stats)
    write_json(run_dir / "faults.json", faults)
    write_json(run_dir / "inconclusive.json", inconclusive)

    outcomes = read_run_outcomes(run_dir)
    points = n_common_points(stats, outcomes)
    graph_n_common(str(run_dir))

    assert points[-1] == (3, 2)
    assert points[2] == (2, 1)
    svg = (run_dir / "graph_n_common.svg").read_text()
    assert 'cx="776.67" cy="350.00" r="3" fill="#7f7f7f"' in svg
    assert 'cx="1130.00" cy="70.00" r="3" fill="#2ca02c"' in svg


def test_legacy_run_without_inconclusive_file_or_counter_is_unchanged(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stats = {"totals": {"passed": 1, "failed": 1}}
    write_json(run_dir / "stats.json", stats)
    write_json(run_dir / "faults.json", {})

    outcomes = read_run_outcomes(run_dir)
    assert n_common_points(stats, outcomes) == [(0, 0), (1, 1), (2, 2)]

    successful_dir = run_dir / "1"
    successful_dir.mkdir()
    try:
        write_phase_changes_csv(str(run_dir))
    except SystemExit as exc:
        assert "Missing IR change summary" in str(exc)
    else:
        raise AssertionError("legacy successful cases still require IR summaries")


def test_supplied_csv_is_reclassified_and_phase_evidence_controls_coverage(
        tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_json(run_dir / "inconclusive.json", {
        "1": {"oracle": "compilation"},
        "2": {"oracle": "compilation"},
        "3": {"oracle": "compilation"},
    })
    write_json(run_dir / "faults.json", {"2": {"error_phase": "backend"}})
    column = phase_column(*PHASES[0])
    csv_file = tmp_path / "supplied.csv"
    with open(csv_file, "w", newline="") as supplied:
        writer = csv.DictWriter(
            supplied,
            fieldnames=["program_id", "program_kind", "error_phase", column],
        )
        writer.writeheader()
        writer.writerows([
            {"program_id": "1", "program_kind": "successful", column: "1"},
            {"program_id": "2", "program_kind": "inconclusive", column: "1"},
            {"program_id": "3", "program_kind": "successful", column: "0"},
        ])

    rows = coverage_rows(run_dir, csv_file, [column])

    assert [(row["program_id"], row["program_kind"]) for row in rows] == [
        ("1", "inconclusive"), ("2", "backend_fault")]
    assert rows[1]["error_phase"] == "backend"


def reporting_options(test_directory):
    return SimpleNamespace(
        stop_cond="iterations", seconds=0, iterations=2, transformations=0,
        transformation_types=[], bugs="", name="test", language="kotlin",
        oracle="compilation", time_metrics=False, disable_metrics=True,
        debug=True, test_directory=str(test_directory),
    )


def test_reporting_tracks_unexpected_inconclusive_in_compilation_mode(
        tmp_path, monkeypatch):
    from src.generators.config import cfg

    monkeypatch.setattr(cfg.prob, "crossmodule_probability", 0)
    reporting = Reporting(reporting_options(tmp_path), mode=CompilationMode())
    reporting.update_stats(
        OracleResult(
            faults={2: {"oracle": "compilation"}},
            inconclusive={1: {"oracle": "compilation"},
                          2: {"oracle": "compilation"}},
        ),
        batch=2,
        batch_time=0,
    )

    assert reporting.stats["totals"] == {
        "passed": 0, "failed": 1, "inconclusive": 1}
    assert (tmp_path / "inconclusive.json").exists()
    assert json.loads((tmp_path / "inconclusive.json").read_text()) == {
        "1": {"oracle": "compilation"}}


def test_compilation_mode_healthy_accounting_writes_empty_inconclusive_report(
        tmp_path, monkeypatch):
    from src.generators.config import cfg

    monkeypatch.setattr(cfg.prob, "crossmodule_probability", 0)
    reporting = Reporting(reporting_options(tmp_path), mode=CompilationMode())
    reporting.update_stats(OracleResult(), batch=1, batch_time=0)

    assert reporting.stats["totals"] == {
        "passed": 1, "failed": 0, "inconclusive": 0}
    assert json.loads((tmp_path / "inconclusive.json").read_text()) == {}
    assert not (tmp_path / "divergences.md").exists()
