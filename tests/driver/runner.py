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
    from tests.driver.support import ROOT
    from tests.kotlin_compilers_fakes import fake_master
    fake_tool = '#!' + sys.executable + '\n' + (ROOT / 'tests/driver/fake_compiler.py').read_text()
    # The default Kotlin compiler is master; every worker inherits this store.
    fake_master(workspace / 'compilers', launcher_script=fake_tool)
    # Imports below must remain after argument setup, including with spawn.
    import hephaestus
    options = hephaestus.parse_args(sys.argv[1:])
    assert options.test_directory == str(workspace / 'bugs/session')
    tools = workspace / 'tools'
    tools.mkdir(exist_ok=True)
    for name in ('kotlinc', 'javac', 'groovyc', 'scalac'):
        executable = tools / name
        executable.write_text(fake_tool)
        executable.chmod(0o755)
    os.environ['PATH'] = str(tools) + os.pathsep + os.environ['PATH']
    os.environ['DRIVER_TOOL_LOG'] = str(workspace / 'tool.jsonl')
    module_name, function_name = scenario.rsplit('.', 1)
    function = getattr(importlib.import_module(module_name), function_name)
    function()


if __name__ == '__main__':
    main()