"""Headless CI entry: ``python -m ci.fuzz <oracle> [--duration 23h] [--workers 4]``.

Builds the Hephaestus options for one oracle and runs a :class:`HeadlessFuzzer`:
nothing prompts, Hephaestus's own output goes to a console log, and the build
log gets a heartbeat line per interval plus a final summary.

A run ends when its time budget runs out (exit 0), on SIGTERM (143), or when it
is still running at budget + grace (watchdog, 124). The last two take the same
path as Ctrl-C, so the scheduler stops its workers and cleans up as usual.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import re
import signal
import sys
import time
from types import FrameType
from typing import TextIO

from hephaestus import Fuzzer
from src.args import configure_generator, parse_args
from src.tools.kotlin_compilers.layout import Layout


ROOT = Path(__file__).resolve().parents[1]

COMMON = ['--language', 'kotlin', '-P', '--batch', '1', '--iterations', '0',
          '--generator-config', str(ROOT / 'config_1.53x.json'),
          '--disable-reified-type-parameters', '--keep-successful', '--time-metrics']
ORACLES: dict[str, list[str]] = {
    'backend-comparison': ['--oracle', 'backend-comparison'],
    'inlining-native': ['--oracle', 'inlining-comparison', '--backend', 'native'],
    'inlining-js': ['--oracle', 'inlining-comparison', '--backend', 'js'],
    'inlining-wasm': ['--oracle', 'inlining-comparison', '--backend', 'wasm'],
}

EXIT_CODES = {'budget': 0, 'sigterm': 143, 'watchdog': 124}
STOP_REASONS = {'budget': 'time budget used up', 'sigterm': 'stopped by SIGTERM',
                'watchdog': 'stopped by the watchdog'}
SIGNALS = {signal.SIGTERM, signal.SIGALRM}
TEAMCITY_ESCAPES = str.maketrans({'|': '||', "'": "|'", '\n': '|n', '\r': '|r',
                                  '[': '|[', ']': '|]'})

DURATION = re.compile(r'(\d+)([smhd])')
UNIT_SECONDS = {'s': 1, 'm': 60, 'h': 60 * 60, 'd': 24 * 60 * 60}


def parse_duration(value: str) -> int:
    """``\\d+[smhd]`` to a positive number of seconds."""
    match = DURATION.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError(
            f'invalid duration {value!r}: use a number with s, m, h or d, e.g. 23h')
    seconds = int(match.group(1)) * UNIT_SECONDS[match.group(2)]
    if seconds <= 0:
        raise argparse.ArgumentTypeError(f'invalid duration {value!r}: must be positive')
    return seconds


def master_version() -> str:
    """The exact version behind the store's ``master`` symlink."""
    return Layout().compiler_dir('master').resolve().name


def run_name(oracle: str, version: str, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return f'kotlin-{version}-{oracle}-{moment:%Y%m%dT%H%M%SZ}'


def hephaestus_arguments(oracle: str, seconds: int, workers: int, name: str) -> list[str]:
    return [*COMMON, *ORACLES[oracle], '--seconds', str(seconds),
            '--workers', str(workers), '--name', name]


def service_message(name: str, message: str | None = None, /, **attributes: object) -> str:
    """A TeamCity service message with escaped values."""
    def escape(text: object) -> str:
        return str(text).translate(TEAMCITY_ESCAPES)
    if message is not None:
        return f"##teamcity[{name} '{escape(message)}']"
    pairs = ' '.join(f"{key}='{escape(text)}'" for key, text in attributes.items())
    return f'##teamcity[{name} {pairs}]'


def format_elapsed(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


class HeadlessFuzzer(Fuzzer):
    """A :class:`Fuzzer` whose ``start()`` runs unattended under CI.

    SIGTERM and the budget + grace watchdog raise ``KeyboardInterrupt`` in the
    main process, which the scheduler already handles like Ctrl-C. Forked
    workers inherit the SIGTERM handler; there it falls back to the default
    action, so ``pool.terminate()`` still ends them.
    """

    def __init__(self, options: argparse.Namespace, *, heartbeat: int, grace: int,
                 out: TextIO) -> None:
        super().__init__(options)
        self.heartbeat = heartbeat
        self.grace = grace
        self.out = out
        self.stop_reason = 'budget'
        self.started = False
        self.elapsed = 0.0
        self.console_log = Path(self.options.bugs) / f'{self.options.name}.console.log'
        self._accepting_stops = False
        self._main_pid = os.getpid()
        self._start_time = 0.0
        self._beats = 0

    def start(self) -> None:
        previous = {signum: signal.getsignal(signum) for signum in SIGNALS}
        saved_stdout = sys.stdout
        self.reporting.save_stats = self._signals_blocked(self.reporting.save_stats)
        self.reporting.logging = self._marks_start(self.reporting.logging)
        self.console_log.parent.mkdir(parents=True, exist_ok=True)
        # Unbuffered: forked workers flush an inherited sys.stdout buffer when they exit.
        console = io.TextIOWrapper(io.FileIO(self.console_log, 'w'), encoding='utf-8',
                                   write_through=True)
        self._main_pid = os.getpid()
        self._start_time = time.monotonic()
        self._beats = 0
        self._write(f'{self.options.name}: fuzzing for {format_elapsed(self.options.seconds)}, '
                    f'console output in {self.console_log}')
        try:
            sys.stdout = console
            signal.signal(signal.SIGTERM, self._on_sigterm)
            signal.signal(signal.SIGALRM, self._on_alarm)
            self._accepting_stops = True
            self._arm_timer()
            try:
                super().start()
            except KeyboardInterrupt:
                if self.stop_reason == 'budget':
                    raise
            finally:
                self._accepting_stops = False
            if self.started:
                # A run stopped before its first batch finished has no stats yet.
                self.reporting.save_stats()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            sys.stdout = saved_stdout
            console.close()
            self.elapsed = time.monotonic() - self._start_time
            run_directory = Path(self.options.test_directory)
            if self.started and run_directory.is_dir():
                self.console_log = self.console_log.replace(run_directory / 'console.log')

    def totals(self) -> dict[str, int]:
        return self.reporting.stats['totals']

    def summary(self) -> str:
        totals = self.totals()
        return (f'{self.options.name}: {STOP_REASONS[self.stop_reason]} after '
                f'{format_elapsed(self.elapsed)}; passed {totals["passed"]}, '
                f'failed {totals["failed"]}, inconclusive {totals["inconclusive"]}')

    def report(self) -> None:
        """The final summary line, plus statistics and status for TeamCity."""
        summary = self.summary()
        self._write(summary)
        if os.environ.get('TEAMCITY_VERSION'):
            for key, value in self.totals().items():
                self._write(service_message('buildStatisticValue',
                                            key=f'hephaestus.{key}', value=value))
            self._write(service_message('buildStatus', text=summary))

    def request_stop(self, reason: str) -> None:
        """Stop like Ctrl-C does; only the first request while running counts."""
        if not self._accepting_stops:
            return
        self._accepting_stops = False
        self.stop_reason = reason
        raise KeyboardInterrupt

    def _on_sigterm(self, signum: int, frame: FrameType | None) -> None:
        if os.getpid() != self._main_pid:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)
            return
        self.request_stop('sigterm')

    def _on_alarm(self, signum: int, frame: FrameType | None) -> None:
        if os.getpid() != self._main_pid:
            return
        elapsed = time.monotonic() - self._start_time
        if elapsed >= (self._beats + 1) * self.heartbeat:
            self._beats = int(elapsed // self.heartbeat)
            self._heartbeat(elapsed)
        if elapsed >= self.options.seconds + self.grace:
            self.request_stop('watchdog')
        self._arm_timer()

    def _arm_timer(self) -> None:
        """One-shot timer for the next heartbeat or the watchdog deadline."""
        next_event = min((self._beats + 1) * self.heartbeat, self.options.seconds + self.grace)
        delay = next_event - (time.monotonic() - self._start_time)
        signal.setitimer(signal.ITIMER_REAL, max(delay, 0.01))

    def _heartbeat(self, elapsed: float) -> None:
        totals = self.totals()
        line = (f'[{format_elapsed(elapsed)}] passed {totals["passed"]}, '
                f'failed {totals["failed"]}, inconclusive {totals["inconclusive"]}')
        self._write(line)
        if os.environ.get('TEAMCITY_VERSION'):
            self._write(service_message('progressMessage', line))

    def _write(self, line: str) -> None:
        self.out.write(line + '\n')
        self.out.flush()

    def _signals_blocked[**P, R](self, method: Callable[P, R]) -> Callable[P, R]:
        """``method`` with SIGTERM and SIGALRM held back, so no stop interrupts a write."""
        def blocked(*args: P.args, **kwargs: P.kwargs) -> R:
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
            try:
                return method(*args, **kwargs)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        return blocked

    def _marks_start[**P, R](self, method: Callable[P, R]) -> Callable[P, R]:
        """``method`` (the scheduler's first report) also records that the run has begun."""
        def marking(*args: P.args, **kwargs: P.kwargs) -> R:
            self.started = True
            return method(*args, **kwargs)
        return marking


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m ci.fuzz',
        description='Run one Hephaestus oracle headless for a fixed time budget.')
    parser.add_argument('oracle', choices=list(ORACLES))
    parser.add_argument('--duration', type=parse_duration, default='23h',
                        help='Time budget, e.g. 23h, 90m or 45s (default: 23h)')
    parser.add_argument('--workers', type=int, default=4,
                        help='Number of Hephaestus workers (default: 4)')
    parser.add_argument('--heartbeat', type=parse_duration, default='5m',
                        help='Interval between progress lines (default: 5m)')
    parser.add_argument('--grace', type=parse_duration, default='15m',
                        help='Time past the budget before the run is stopped (default: 15m)')
    parser.add_argument('--strip-pickles', action='store_true',
                        help='Remove the *.bin pickles under the run directory afterwards')
    return parser


def strip_pickles(run_directory: str) -> int:
    pickles = list(Path(run_directory).rglob('*.bin'))
    for path in pickles:
        path.unlink()
    return len(pickles)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    name = run_name(args.oracle, master_version())
    options = parse_args(hephaestus_arguments(args.oracle, args.duration, args.workers, name))
    configure_generator(options)
    fuzzer = HeadlessFuzzer(options, heartbeat=args.heartbeat, grace=args.grace,
                            out=sys.stdout)
    fuzzer.start()
    fuzzer.report()
    if args.strip_pickles and os.path.isdir(options.test_directory):
        print(f'Removed {strip_pickles(options.test_directory)} pickles', flush=True)
    return EXIT_CODES[fuzzer.stop_reason]


if __name__ == '__main__':
    sys.exit(main())
