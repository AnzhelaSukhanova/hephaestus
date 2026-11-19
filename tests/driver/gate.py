"""Run the hermetic suite and enforce separate statement and branch thresholds
across all configured coverage sources."""
import configparser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from coverage import Coverage

from tests.driver.contracts import record
from tests.driver.support import ROOT

CONFIG = ROOT / 'tests/driver/coverage.ini'


def configured_sources() -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(CONFIG)
    return parser['run']['source'].split()


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
    coverage = Coverage(config_file=str(CONFIG),
                        data_file=str(directory / '.coverage'))
    coverage.combine(data_paths=[str(directory)], strict=True, keep=True)
    coverage.save()
    report = directory / 'coverage.json'
    coverage.json_report(outfile=str(report))
    data = record(json.loads(report.read_text()))
    files = record(data['files'])
    for source in configured_sources():
        path = source.replace('.', '/')
        assert any(name == path + '.py' or name.startswith(path + '/') for name in files), (
            f'No coverage data for {source}')
    statements = covered_statements = branches = covered_branches = 0
    for name in sorted(files):
        measured = record(files[name])
        assert measured['excluded_lines'] == [], 'Do not hide uncovered behavior'
        summary = record(measured['summary'])
        file_statements = summary['num_statements']
        file_covered_statements = summary['covered_lines']
        file_branches = summary['num_branches']
        file_covered_branches = summary['covered_branches']
        assert isinstance(file_statements, int) and isinstance(file_covered_statements, int)
        assert isinstance(file_branches, int) and isinstance(file_covered_branches, int)
        statements += file_statements
        covered_statements += file_covered_statements
        branches += file_branches
        covered_branches += file_covered_branches
        print(f'{name}: statements {file_covered_statements}/{file_statements}, '
              f'branches {file_covered_branches}/{file_branches}')
        print('  Uncovered lines:', measured['missing_lines'])
        print('  Uncovered branches:', measured['missing_branches'])
    statement_percent = 100 * covered_statements / statements
    branch_percent = 100 * covered_branches / branches
    print(f'Statements: {covered_statements}/{statements} = {statement_percent:.2f}%')
    print(f'Branches: {covered_branches}/{branches} = {branch_percent:.2f}%')
    print('Evidence:', directory)
    return int(result.returncode != 0 or statement_percent < 90 or branch_percent < 90)


if __name__ == '__main__':
    sys.exit(main())