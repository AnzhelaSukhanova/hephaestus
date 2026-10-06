from collections.abc import Mapping
from datetime import datetime
import json
import os
import sys
from typing import Any

from src import utils
from src.modules.models import ProgramRes, ProgramStats, TimeMetrics, OracleResult
from src.modules.worker import RunOptions
from src.generators.config import cfg
from src.generators.generator import EscalationLog
from src.modules.crossmodule import CrossModuleManager


def initial_stats(options: RunOptions) -> dict[str, Any]:
    stats = {
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
        "failed": 0
    },
    "time": 0,
    "compilation_time": 0,
    "faults": {},
    "escalations": {},
    "time_metrics": {},
    }
    if cfg.prob.crossmodule_probability:
        stats["crossmodule"] = {}
    return stats


TEMPLATE_MSG: str = (u"Test Programs Passed {} / {} \u2714\t\t"
                "Test Programs Failed {} / {} \u2718\r")


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
                 manager: CrossModuleManager | None = None) -> None:
        super().__init__(options)
        self.manager = manager
        self.stats = initial_stats(options)

    def print_msg(self) -> None:
        sys.stdout.write('\033[2K\033[1G')
        failed = self.stats['totals']['failed']
        passed = self.stats['totals']['passed']
        iterations = (
            self.options.iterations
            if self.options.iterations else passed + failed)
        msg = TEMPLATE_MSG.format(passed, iterations, failed, iterations)
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
        stats_file = os.path.join(dst_dir, "stats.json")
        escalations_file = os.path.join(dst_dir, "escalations.json")
        time_metrics_file = os.path.join(dst_dir, "time_metrics.json")
        utils.mkdir(dst_dir)
        faults = self.stats.pop('faults')
        escalations = self.stats.pop('escalations')
        time_metrics = self.stats.pop('time_metrics')
        with open(faults_file, 'w') as out:
            json.dump(faults, out, indent=2)
        if not self.options.disable_metrics:
            with open(escalations_file, 'w') as out:
                json.dump(escalations, out, indent=2)
        if self.options.time_metrics:
            with open(time_metrics_file, 'w') as out:
                json.dump(time_metrics, out, indent=2)
        if cfg.prob.crossmodule_probability:
            self.stats.setdefault('crossmodule', {})
        with open(stats_file, 'w') as out:
            json.dump(self.stats, out, indent=2)
        if cfg.prob.crossmodule_probability:
            self.manager.save_ledger(self.stats['crossmodule'])
        self.stats['faults'] = faults
        self.stats['escalations'] = escalations
        self.stats['time_metrics'] = time_metrics

    def update_stats(self, res: OracleResult, batch: int, batch_time: float,
                     escalations: Mapping[str, list[EscalationLog]] | None = None
                     ) -> None:
        res, compilation_time, time_metrics, published = res
        failed = len(res)
        passed = batch - failed
        self.stats['totals']['failed'] += failed
        self.stats['totals']['passed'] += passed
        self.stats["time"] += batch_time
        self.stats["compilation_time"] += compilation_time
        self.stats['faults'].update(res)
        if cfg.prob.crossmodule_probability:
            # The ledger is the graph's only source of truth, and it is written
            # here, in the parent, through the atomic save_stats path.
            ledger = self.stats.setdefault('crossmodule', {})
            for pid, record in (published or {}).items():
                # The ledger holds the graph, and a manifest disagreement is a
                # signal about it rather than part of it.
                diff = record.pop('manifest_diff', None)
                ledger[str(pid)] = record
                if diff is not None:
                    self.stats.setdefault('crossmodule_manifest_diff', {})[
                        str(pid)] = diff
                    print('\nProgram {} records dependencies outside its '
                          'closure: {}'.format(pid, diff['unexpected']))
        if not self.options.disable_metrics:
            self.stats['escalations'].update(escalations or {})
        self.stats['time_metrics'].update(time_metrics or {})
        if not self.options.debug:
            self.print_msg()
        self.save_stats()