"""Run the hermetic suite and enforce separate statement and branch thresholds."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from coverage import Coverage

from tests.driver.contracts import record
from tests.driver.support import ROOT


def main() -> int:
    output = ROOT / '.driver-coverage'
    output.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='run_', dir=output))
    env = os.environ.copy()
    env['DRIVER_COVERAGE_DIR'] = str(directory)
    result = subprocess.run([sys.executable, '-m', 'pytest', 'tests/driver', '-q',
                             '-o', 'addopts=', '-ra'], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=300)
    (directory / 'pytest.log').write_text(result.stdout + result.stderr)
    print(result.stdout + result.stderr)
    coverage = Coverage(config_file=str(ROOT / 'tests/driver/coverage.ini'),
                        data_file=str(directory / '.coverage'))
    coverage.combine(data_paths=[str(directory)], strict=True, keep=True)
    coverage.save()
    report = directory / 'coverage.json'
    coverage.json_report(outfile=str(report))
    data = record(json.loads(report.read_text()))
    files = record(data['files'])
    driver = record(files['hephaestus.py'])
    assert driver['excluded_lines'] == [], 'Do not hide uncovered behavior'
    summary = record(driver['summary'])
    statements = summary['num_statements']
    covered_statements = summary['covered_lines']
    branches = summary['num_branches']
    covered_branches = summary['covered_branches']
    assert isinstance(statements, int) and isinstance(covered_statements, int)
    assert isinstance(branches, int) and isinstance(covered_branches, int)
    statement_percent = 100 * covered_statements / statements
    branch_percent = 100 * covered_branches / branches
    print(f'Statements: {covered_statements}/{statements} = {statement_percent:.2f}%')
    print(f'Branches: {covered_branches}/{branches} = {branch_percent:.2f}%')
    print('Uncovered lines:', driver['missing_lines'])
    print('Uncovered branches:', driver['missing_branches'])
    print('Evidence:', directory)
    return int(result.returncode != 0 or statement_percent < 90 or branch_percent < 90)


if __name__ == '__main__':
    sys.exit(main())