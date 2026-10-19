from dataclasses import dataclass
import json
import os
import time


def read_report(path, attempts=20, delay=0.05):
    if not os.path.exists(path):
        return {}

    last_exc = None
    for _ in range(attempts):
        try:
            with open(path) as report_file:
                return json.load(report_file)
        except json.JSONDecodeError as exc:
            last_exc = exc
            time.sleep(delay)
    raise last_exc


def _normalize_report(report):
    return {int(pid): record for pid, record in report.items()}


@dataclass(frozen=True)
class RunOutcomes:
    faults: dict
    inconclusive: dict

    @classmethod
    def from_reports(cls, faults, inconclusive):
        return cls(_normalize_report(faults), _normalize_report(inconclusive))

    def fault(self, pid):
        return self.faults.get(int(pid))

    def is_inconclusive(self, pid):
        return self.outcome(pid) == "inconclusive"

    def outcome(self, pid):
        pid = int(pid)
        if pid in self.faults:
            return "fault"
        if pid in self.inconclusive:
            return "inconclusive"
        return "successful"


def read_run_outcomes(run_dir):
    return RunOutcomes.from_reports(
        read_report(os.path.join(run_dir, "faults.json")),
        read_report(os.path.join(run_dir, "inconclusive.json")),
    )


def attempted_iterations(totals):
    return (totals.get("passed", 0) + totals.get("failed", 0) +
            totals.get("inconclusive", 0))