"""Controllable executable: logs actual argv/cwd and emits compiler diagnostics."""
import json
import os
from pathlib import Path
import sys
import zipfile


def main() -> int:
    arguments = sys.argv[1:]
    log = os.environ.get('DRIVER_TOOL_LOG')
    if log:
        with open(log, 'a') as output:
            output.write(json.dumps({'arguments': arguments, 'cwd': os.getcwd(),
                                     'java_opts': os.environ.get('JAVA_OPTS')}) + '\n')
    if '-version' in arguments:
        print('hermetic compiler 1.0')
        return 0
    mode = os.environ.get('DRIVER_COMPILER_MODE', 'success')
    target = ('native' if '-produce' in arguments
              else 'jvm' if '-d' in arguments
              else 'wasm' if any('kotlin-stdlib-wasm-' in argument
                                 for argument in arguments) else 'js')
    crash_targets = os.environ.get('DRIVER_CRASH_TARGETS')
    if mode == 'crash' and (crash_targets is None or target in crash_targets.split(',')):
        print('org.jetbrains.kotlin.CompilerException\ncontrolled crash')
        return 2
    sources: list[Path] = []
    for argument in arguments:
        if argument.startswith('-'):
            continue
        path = Path(argument)
        if path.is_dir():
            sources.extend(sorted(path.rglob('*.kt')))
        elif path.suffix in ('.kt', '.java'):
            sources.append(path)
    rejected = [source for source in sources
                if mode == 'reject' or (mode == 'reject-incorrect' and '_incorrect' in str(source))]
    for source in rejected:
        if source.suffix == '.java':
            print(f'{source}:1: error: controlled rejection')
        else:
            print(f'{source}:1:1: error: controlled rejection')
    if rejected:
        return 1
    if '-d' in arguments:
        destination = Path(arguments[arguments.index('-d') + 1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, 'w') as archive:
            archive.writestr('META-INF/MANIFEST.MF', 'Manifest-Version: 1.0\n')
        return 0
    name: str | None = None
    if '-o' in arguments:
        name = arguments[arguments.index('-o') + 1]
    if '-ir-output-name' in arguments:
        output_directory = arguments[arguments.index('-ir-output-dir') + 1]
        name = str(Path(output_directory) / arguments[arguments.index('-ir-output-name') + 1])
    if name:
        destination = Path(name + '.klib')
        destination.parent.mkdir(parents=True, exist_ok=True)
        depends = os.environ.get('DRIVER_MANIFEST_DEPENDS', 'stdlib')
        with zipfile.ZipFile(destination, 'w') as archive:
            archive.writestr('default/manifest', 'depends=' + depends + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())