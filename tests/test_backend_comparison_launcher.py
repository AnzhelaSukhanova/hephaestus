import os
from pathlib import Path
import shlex
import subprocess

import pytest

from src.args import parse_args, validate_args


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'run-backend-comparison.sh'
SINGLEMODULE_SCRIPT = ROOT / 'run-singlemodule-backend-comparison.sh'


def launch(cwd, arguments, overrides=None, script=SCRIPT):
    environment = os.environ.copy()
    for key in ('PYTHON', 'NAME', 'ITERATIONS', 'WORKERS', 'GENERATOR_CONFIG',
                'INHIBIT_SLEEP'):
        environment.pop(key, None)
    environment.update(overrides or {})
    return subprocess.run(['bash', str(script), *arguments], cwd=cwd,
                          env=environment, capture_output=True, text=True,
                          timeout=10)


def printed_command(result):
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith('Command: ')
    return shlex.split(result.stdout.removeprefix('Command: '))


@pytest.mark.parametrize('option', ['--help', '-h'])
def test_help_needs_no_python_or_compilers(tmp_path, option):
    result = launch(tmp_path, [option], {'PYTHON': '/missing/python'})
    assert result.returncode == 0, result.stderr
    assert 'Usage: ./run-backend-comparison.sh' in result.stdout
    assert 'JS, Native and Wasm' in result.stdout


def test_default_command_from_another_directory(tmp_path):
    command = printed_command(launch(tmp_path, ['--print-command']))
    assert command[:2] == [str(ROOT / '.venv/bin/python'), str(ROOT / 'hephaestus.py')]
    options = parse_args(command[2:])
    validate_args(options)
    assert options.language == 'kotlin'
    assert options.oracle == 'backend-comparison'
    assert options.only_correctness_preserving_transformations
    assert options.iterations == 1000000
    assert options.workers == 8
    assert options.batch == 1
    assert options.generator_config == str(ROOT / 'config_1.53x.json')
    assert options.disable_reified_type_parameters
    assert options.keep_everything and options.keep_all and options.time_metrics
    assert options.name.startswith('backend-comparison-')
    assert not options.dump_ir and not options.jacoco_always and not options.jacoco_lowerings


def test_environment_overrides_and_quoted_argument_forwarding(tmp_path):
    overrides = {'PYTHON': '/missing/python with spaces', 'ITERATIONS': '0',
                 'WORKERS': '2', 'NAME': 'comparison session',
                 'GENERATOR_CONFIG': 'config with spaces.json'}
    extra = ['--seconds', '60', '--workers', '1', '--name', 'override session',
             '--bugs', str(tmp_path / 'bugs with spaces')]
    command = printed_command(launch(tmp_path, ['--print-command', *extra], overrides))
    assert command[0] == overrides['PYTHON']
    assert command[-len(extra):] == extra
    options = parse_args(command[2:])
    validate_args(options)
    assert options.oracle == 'backend-comparison'
    assert options.iterations == 0 and options.seconds == 60
    assert options.workers == 1
    assert options.name == 'override session'
    assert options.generator_config == overrides['GENERATOR_CONFIG']
    assert options.bugs == str(tmp_path / 'bugs with spaces')


def test_missing_python_fails_before_launch(tmp_path):
    result = launch(tmp_path, [], {'PYTHON': '/missing/python', 'INHIBIT_SLEEP': '0'})
    assert result.returncode == 1
    assert 'Python not found: /missing/python' in result.stderr


def test_singlemodule_help_needs_no_python_or_compilers(tmp_path):
    result = launch(tmp_path, ['--help'], {'PYTHON': '/missing/python'},
                    SINGLEMODULE_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert 'Usage: ./run-singlemodule-backend-comparison.sh' in result.stdout
    assert 'JS, Native and Wasm' in result.stdout


def test_singlemodule_default_command_from_another_directory(tmp_path):
    command = printed_command(
        launch(tmp_path, ['--print-command'], script=SINGLEMODULE_SCRIPT))
    assert command[:2] == [str(ROOT / '.venv/bin/python'), str(ROOT / 'hephaestus.py')]
    options = parse_args(command[2:])
    validate_args(options)
    assert options.language == 'kotlin'
    assert options.oracle == 'backend-comparison'
    assert options.only_correctness_preserving_transformations
    assert options.iterations == 1000000
    assert options.workers == 8
    assert options.batch == 1
    assert options.generator_config == str(ROOT / 'config_1.53x_singlemodule.json')
    assert options.disable_reified_type_parameters
    assert options.keep_everything and options.keep_all and options.time_metrics
    assert options.name.startswith('singlemodule-backend-comparison-')
    assert not options.dump_ir and not options.jacoco_always and not options.jacoco_lowerings


def test_singlemodule_missing_python_fails_before_launch(tmp_path):
    result = launch(tmp_path, [], {'PYTHON': '/missing/python', 'INHIBIT_SLEEP': '0'},
                    SINGLEMODULE_SCRIPT)
    assert result.returncode == 1
    assert 'Python not found: /missing/python' in result.stderr