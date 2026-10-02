"""Typed compiler-boundary fixtures; publication and import use real managers."""
from copy import deepcopy
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Literal
import zipfile

from src.ir import ast
from src.ir.context import Context
from src.modules.processor import ProgramProcessor
from tests.driver.support import assert_arguments


type KlibLayout = Literal['file', 'directory', 'missing']


def write_klib(path: Path, layout: KlibLayout, manifest: str | None,
               payload: str) -> None:
    if layout == 'missing':
        return
    if layout == 'directory':
        (path / 'default').mkdir(parents=True)
        (path / 'default/payload').write_text(payload)
        if manifest is not None:
            (path / 'default/manifest').write_text(manifest)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('default/payload', payload)
            if manifest is not None:
                archive.writestr('default/manifest', manifest)


def klib_payload(path: Path) -> str:
    if path.is_dir():
        return (path / 'default/payload').read_text()
    with zipfile.ZipFile(path) as archive:
        return archive.read('default/payload').decode()


def assert_command(arguments: list[str], backend: str, source: Path,
                   module: str, dependencies: list[str]) -> None:
    assert_arguments(arguments)
    assert all(Path(path).is_absolute() for path in dependencies)
    expected = [str(source)]
    if backend == 'native':
        expected += ['-produce', 'library', '-o', module,
                     '-nowarn', '-Xklib-ir-inliner=full']
        for dependency in dependencies:
            expected += ['-library', dependency]
        if dependencies:
            expected += ['-friend-modules', dependencies[0]]
    else:
        stdlib = ('$HOME/kotlin/libraries/stdlib/build/libs/kotlin-stdlib-' +
                  ('wasm-' if backend == 'wasm' else '') +
                  'js-2.4.255-SNAPSHOT.klib')
        expected += ['-Xir-produce-klib-file', '-ir-output-dir', '.',
                     '-ir-output-name', module,
                     '-libraries', os.pathsep.join([stdlib, *dependencies]),
                     '-nowarn', '-Xklib-ir-inliner=full']
        if dependencies:
            expected += ['-Xfriend-modules', dependencies[0]]
    assert arguments[1:] == expected


@dataclass
class CompileStep:
    source: Path
    layout: KlibLayout = 'file'
    manifest: str | None = 'depends=stdlib\n'
    status: bool = True
    output: str = ''


@dataclass
class Commands:
    pid: int
    directory: Path
    backend: str
    dependencies: list[str]
    steps: list[CompileStep]
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, arguments: list[str], get_stdout: bool = True,
                 cwd: str | None = None) -> tuple[bool, str]:
        assert len(self.calls) < len(self.steps), arguments
        step = self.steps[len(self.calls)]
        assert get_stdout
        module = f'd{self.pid}'
        assert cwd is not None
        assert cwd == str(self.directory / 'src' / module)
        assert_command(arguments, self.backend, step.source, module, self.dependencies)
        destination = Path(cwd) / (module + '.klib')
        # In particular, a failed NCP batch's file/directory must be removed
        # before the isolated correct-source rebuild starts.
        assert not destination.exists()
        self.calls.append(list(arguments))
        write_klib(destination, step.layout, step.manifest,
                   'batch' if len(self.calls) == 1 else 'isolated')
        return step.status, step.output

    def finished(self) -> None:
        assert len(self.calls) == len(self.steps)


class RecordingProcessor(ProgramProcessor):
    imported: Context | None = None

    def get_program(self, pre_existing_context: Context | None = None
                    ) -> tuple[ast.Program, bool]:
        self.imported = deepcopy(pre_existing_context)
        program, oracle = super().get_program(pre_existing_context)
        integer = program.bt_factory.get_integer_type()
        for name, visibility in (('visible', ast.Visibilities.PUBLIC),
                                 ('hidden', ast.Visibilities.PRIVATE)):
            program.add_declaration(ast.FunctionDeclaration(
                f'{self.target_module}.{name}', [], integer,
                ast.IntegerConstant(1, integer), ast.FunctionDeclaration.FUNCTION,
                visibility=visibility))
        return program, oracle


class FaultProcessor(RecordingProcessor):
    def inject_fault(self, program: ast.Program
                     ) -> tuple[ast.Program, str | None]:
        incorrect = deepcopy(program)
        incorrect.add_declaration(ast.VariableDeclaration(
            f'{self.target_module}.mismatch',
            ast.IntegerConstant(1, program.bt_factory.get_integer_type()),
            var_type=program.bt_factory.get_string_type()))
        return incorrect, 'controlled type mismatch'