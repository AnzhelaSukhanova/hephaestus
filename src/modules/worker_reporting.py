from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
import json
import os
import sys
from typing import Any

from src import utils
from src.modules.models import (
    ComparisonDependencies, OracleResult, ProgramRes, ProgramStats, TimeMetrics,
)
from src.modules.worker import RunOptions
from src.generators.generator import EscalationLog
from src.modules.crossmodule import CrossModuleManager
from src.modules.oracle_modes import OracleMode
from src.tools.divergences import write_divergences


def initial_stats(options: RunOptions) -> dict[str, Any]:
    return {
    "Info": {
        "stop_cond": options.stop_cond,
        "stop_cond_value": (
            options.seconds
            if options.stop_cond == "timeout"
            else options.iterations
        ),
        "transformations": options.transformations,
        "transformation_types": ",".join(options.transformation_types),
        "bugs": options.bugs,
        "name": options.name,
        "language": options.language
    },
    "totals": {
        "passed": 0,
        "failed": 0,
        "inconclusive": 0,
    },
    "time": 0,
    "compilation_time": 0,
    "faults": {},
    "inconclusive": {},
    "escalations": {},
    "time_metrics": {},
    }


TEMPLATE_MSG: str = (u"Test Programs Passed {} / {} \u2714\t\t"
                "Test Programs Failed {} / {} \u2718\r")


def _json_default(value: object) -> object:
    if isinstance(value, ComparisonDependencies):
        return asdict(value)
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')


class TimingMetrics:
    def __init__(self, options: RunOptions) -> None:
        self.options = options

    def get_time_metrics(self, oracles: Mapping[int, ProgramRes],
                         compilation_time: float) -> TimeMetrics:
        if not self.options.time_metrics:
            return {}
        return {
            pid: {
                "generation_cpu": proc_res.stats.get("time", 0),
                "compilation_with_ir_dumps": compilation_time,
                "phase_profiling": 0.0
            }
            for pid, proc_res in oracles.items()
        }

    def attach_time_metrics(self, pid: int, stats: ProgramStats,
                            time_metrics: TimeMetrics) -> None:
        if self.options.time_metrics:
            stats["time_metrics"] = time_metrics[pid]


class Reporting(TimingMetrics):
    def __init__(self, options: RunOptions,
                 manager: CrossModuleManager | None = None, *,
                 mode: OracleMode) -> None:
        super().__init__(options)
        self.manager = manager
        self.mode = mode
        self.stats = initial_stats(options)

    def print_msg(self) -> None:
        sys.stdout.write('\033[2K\033[1G')
        failed = self.stats['totals']['failed']
        passed = self.stats['totals']['passed']
        inconclusive = self.stats['totals'].get('inconclusive', 0)
        iterations = (
            self.options.iterations
            if self.options.iterations else passed + failed + inconclusive)
        msg = TEMPLATE_MSG.format(passed, iterations, failed, iterations)
        msg = msg.rstrip('\r') + f'  Inconclusive {inconclusive} / {iterations}\r'
        sys.stdout.write(msg)

    def logging(self, compiler_version: str) -> None:
        print("{} {} ({})".format("stop_cond".ljust(21), self.options.stop_cond,
                                  (self.options.seconds
                                   if self.options.stop_cond == "timeout"
                                   else self.options.iterations)))
        print("{} {}".format("transformations".ljust(21),
                             self.options.transformations))
        print("{} {}".format("transformation_types".ljust(21), ",".join(
              self.options.transformation_types)))
        print("{} {}".format("bugs".ljust(21), self.options.bugs))
        print("{} {}".format("name".ljust(21), self.options.name))
        print("{} {}".format("language".ljust(21), self.options.language))
        print("{} {}".format("compiler".ljust(21), compiler_version))
        utils.fprint("")

        if not self.options.seconds and not self.options.iterations:
            print()
            print(("Warning: To stop the tool press Ctr + c (Linux) or Ctrl + "
                   "Break (Windows)"))
            print()

        if not self.options.debug:
            self.print_msg()
        with open(self.options.log_file, 'a') as out:
            now = datetime.now()
            dt_string = now.strftime("%d/%m/%Y %H:%M:%S")
            out.write("{}; {}; {}; {}; {}\n".format(
                dt_string, self.options.name, self.options.bugs, self.options.language,
                compiler_version))

        self.stats['Info']['compiler'] = compiler_version

    def save_stats(self) -> None:
        dst_dir = os.path.join(self.options.test_directory)
        faults_file = os.path.join(dst_dir, 'faults.json')
        inconclusive_file = os.path.join(dst_dir, 'inconclusive.json')
        stats_file = os.path.join(dst_dir, "stats.json")
        escalations_file = os.path.join(dst_dir, "escalations.json")
        time_metrics_file = os.path.join(dst_dir, "time_metrics.json")
        utils.mkdir(dst_dir)
        faults = self.stats.pop('faults')
        inconclusive = self.stats.pop('inconclusive')
        escalations = self.stats.pop('escalations')
        time_metrics = self.stats.pop('time_metrics')
        with open(faults_file, 'w') as out:
            json.dump(faults, out, indent=2, default=_json_default)
        with open(inconclusive_file, 'w') as out:
            json.dump(inconclusive, out, indent=2, default=_json_default)
        if not self.options.disable_metrics:
            with open(escalations_file, 'w') as out:
                json.dump(escalations, out, indent=2)
        if self.options.time_metrics:
            with open(time_metrics_file, 'w') as out:
                json.dump(time_metrics, out, indent=2)
        with open(stats_file, 'w') as out:
            json.dump(self.stats, out, indent=2)
        self.stats['faults'] = faults
        self.stats['inconclusive'] = inconclusive
        self.stats['escalations'] = escalations
        self.stats['time_metrics'] = time_metrics
        if self.mode.compares_variants():
            write_divergences(dst_dir, faults)

    def update_stats(self, res: OracleResult, batch: int, batch_time: float,
                     escalations: Mapping[str, list[EscalationLog]] | None = None
                     ) -> None:
        failed = len(res.faults)
        inconclusive = len(res.inconclusive.keys() - res.faults.keys())
        passed = batch - failed - inconclusive
        self.stats['totals']['failed'] += failed
        self.stats['totals']['passed'] += passed
        self.stats['totals']['inconclusive'] += inconclusive
        self.stats["time"] += batch_time
        self.stats["compilation_time"] += res.compilation_time
        self.stats['faults'].update(res.faults)
        self.stats['inconclusive'].update({pid: stats for pid, stats in res.inconclusive.items()
                                           if pid not in res.faults})
        if res.publications:
            # Publications exist only when the manager published their
            # artifacts; the parent commits them to the manager's ledger.
            self.manager.commit_publications(res.publications)
        if not self.options.disable_metrics:
            self.stats['escalations'].update(escalations or {})
        self.stats['time_metrics'].update(res.time_metrics)
        if not self.options.debug:
            self.print_msg()
        self.save_stats()