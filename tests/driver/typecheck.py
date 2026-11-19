"""Record raw project diagnostics and compare against unchanged adjacent code."""
from collections.abc import Mapping
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tests.driver.contracts import is_sequence, record
from tests.driver.support import ROOT


def check(root: Path, destination: Path) -> Mapping[object, object]:
    result = subprocess.run([sys.executable, '-m', 'pyright', '--pythonpath', sys.executable,
                             '-p', str(root / 'pyrightconfig.json'), '--outputjson'],
                            cwd=root, text=True, capture_output=True, timeout=90)
    destination.write_text(result.stdout)
    assert result.returncode in (0, 1), result.stderr
    return record(json.loads(result.stdout))


def fingerprints(report: Mapping[object, object], root: Path) -> set[str]:
    entries = report['generalDiagnostics']
    assert is_sequence(entries)
    result: set[str] = set()
    for entry in entries:
        fields = record(entry)
        path = fields['file']
        assert isinstance(path, str)
        normalized = {'file': str(Path(path).relative_to(root)),
                      'severity': fields['severity'], 'message': fields['message'],
                      'range': fields['range'], 'rule': fields.get('rule')}
        result.add(json.dumps(normalized, sort_keys=True))
    return result


def main() -> int:
    output = ROOT / '.driver-coverage'
    output.mkdir(exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix='types_', dir=output))
    current = check(ROOT, evidence / 'current.json')
    with tempfile.TemporaryDirectory(prefix='driver_type_baseline_') as directory:
        baseline = Path(directory)
        shutil.copytree(ROOT / 'src', baseline / 'src', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'tests', baseline / 'tests',
                        ignore=shutil.ignore_patterns('__pycache__', 'driver'))
        for name in ('hephaestus.py', 'pickle_investigator.py'):
            shutil.copy2(ROOT / name, baseline / name)
        configuration = dict(record(json.loads((ROOT / 'pyrightconfig.json').read_text())))
        configuration.pop('executionEnvironments')
        (baseline / 'pyrightconfig.json').write_text(json.dumps(configuration))
        previous = check(baseline, evidence / 'baseline.json')
        added = fingerprints(current, ROOT) - fingerprints(previous, baseline)
    (evidence / 'new-diagnostics.json').write_text(json.dumps(sorted(added), indent=2))
    print('Baseline:', previous['summary'])
    print('Current:', current['summary'])
    print('New diagnostics:', len(added))
    for diagnostic in sorted(added):
        print(diagnostic)
    print('Evidence:', evidence)
    return int(bool(added))


if __name__ == '__main__':
    sys.exit(main())