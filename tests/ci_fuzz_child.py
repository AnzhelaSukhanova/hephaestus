"""Runs ``ci.fuzz`` in a workspace against a fake ``master`` (see ``tests/test_ci_fuzz.py``).

``python -m tests.ci_fuzz_child <workspace> <ci.fuzz arguments...>``

The fake compiler is ``tests/driver/fake_compiler.py``; for anything but a version
probe it first sleeps ``CI_FUZZ_COMPILER_DELAY`` seconds, either always
(``CI_FUZZ_SLOW=always``) or once the run's console log has content, i.e. once
the scheduler has started (``CI_FUZZ_SLOW=running``).
"""
import os
from pathlib import Path
import sys


DELAY = '''import glob, os, sys, time
_slow = os.environ.get('CI_FUZZ_SLOW')
if '-version' not in sys.argv and (_slow == 'always' or _slow == 'running' and any(
        os.path.getsize(path) for path in glob.glob(os.environ['CI_FUZZ_CONSOLE_LOGS']))):
    time.sleep(float(os.environ['CI_FUZZ_COMPILER_DELAY']))
'''


def main() -> int:
    workspace = Path(sys.argv[1]).resolve()
    os.chdir(workspace)
    from tests.driver.support import ROOT
    from tests.kotlin_compilers_fakes import fake_master
    os.environ['CI_FUZZ_CONSOLE_LOGS'] = str(workspace / 'bugs' / '*.console.log')
    fake_master(workspace / 'compilers', launcher_script=(
        '#!' + sys.executable + '\n' + DELAY
        + (ROOT / 'tests/driver/fake_compiler.py').read_text()))
    # Linux, and so the CI container, forks the pool; macOS spawns it by default.
    method = os.environ.get('CI_FUZZ_START_METHOD')
    if method:
        import multiprocessing
        multiprocessing.set_start_method(method)
    from ci import fuzz
    return fuzz.main(sys.argv[2:])


if __name__ == '__main__':
    sys.exit(main())
