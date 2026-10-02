import importlib
import json
import os
from pathlib import Path
import sys


def main() -> None:
    scenario, directory, *arguments = sys.argv[1:]
    workspace = Path(directory)
    os.chdir(workspace)
    config = workspace / 'baseline.json'
    config.write_text(json.dumps({'prob': {'crossmodule_probability': 0}}))
    sys.argv = ['hephaestus', '--language', 'kotlin', '-P',
                '--generator-config', str(config), '-b', str(workspace / 'bugs'),
                '-n', 'session', '-F', str(workspace / 'run.log'), *arguments]
    # Imports below must remain after argument setup, including with spawn.
    import hephaestus
    import src.compilers.kotlin as kotlin
    import shlex
    from tests.driver.support import ROOT
    kotlin.compiler = (shlex.quote(sys.executable) + ' ' +
                       shlex.quote(str(ROOT / 'tests/driver/fake_compiler.py')))
    tools = workspace / 'tools'
    tools.mkdir(exist_ok=True)
    for name in ('javac', 'groovyc', 'scalac'):
        executable = tools / name
        executable.write_text('#!' + sys.executable + '\n' +
                              (ROOT / 'tests/driver/fake_compiler.py').read_text())
        executable.chmod(0o755)
    os.environ['PATH'] = str(tools) + os.pathsep + os.environ['PATH']
    os.environ['DRIVER_TOOL_LOG'] = str(workspace / 'tool.jsonl')
    module_name, function_name = scenario.rsplit('.', 1)
    function = getattr(importlib.import_module(module_name), function_name)
    function()
    assert hephaestus.cli_args.test_directory == str(workspace / 'bugs/session')


if __name__ == '__main__':
    main()