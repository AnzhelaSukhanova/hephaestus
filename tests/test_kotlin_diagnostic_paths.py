import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import pytest

from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.oracles.compilation import OracleFinding, check_compilation


@pytest.mark.parametrize('backend', ['native', 'js', 'wasm'])
@pytest.mark.parametrize('symlink_sources', [False, True])
def test_module_local_diagnostics_match_absolute_source_paths(
        backend, symlink_sources):
    distribution = ('kotlin-native/dist' if backend == 'native'
                    else 'dist/kotlinc')
    executable = Path.home() / 'kotlin' / distribution / 'bin' / f'kotlinc-{backend}'
    if not executable.is_file():
        pytest.skip(f'Kotlin {backend} compiler is not available')
    settings = KotlinSettings(backend=backend, executable=str(executable))
    if backend != 'native':
        stdlib = Path(os.path.expandvars(KotlinCompiler('src', settings=settings)._stdlib()))
        if not stdlib.is_file():
            pytest.skip(f'Kotlin {backend} standard library is not available')

    # Resolve the base so macOS's /var alias cannot mask relative diagnostics.
    # Avoid pytest's hyphenated temporary paths, unsupported by the existing parser.
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        sources = root / 'sources'
        sources.mkdir()
        if symlink_sources:
            alias = root / 'alias'
            alias.symlink_to(sources, target_is_directory=True)
            sources = alias
        source_root = sources / 'src'
        correct = source_root / 'd1' / 'program.kt'
        incorrect = source_root / 'd1_incorrect' / 'program.kt'
        for source, name in ((correct, 'first'), (incorrect, 'second')):
            source.parent.mkdir(parents=True)
            source.write_text(f'fun {name}(): Int = "wrong type"\n')

        compiler = KotlinCompiler(str(source_root), module_name='d1', settings=settings)
        result = subprocess.run(
            [os.path.expandvars(argument) for argument in compiler.get_compiler_cmd()],
            cwd=correct.parent.resolve(), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=60)
        diagnostics, _ = compiler.analyze_compiler_output(result.stdout)

        assert result.returncode != 0, result.stdout
        assert compiler.crash_msg is None, result.stdout
        assert set(diagnostics) == {str(correct), str(incorrect)}, result.stdout
        assert str(correct) + ':1:' in result.stdout
        assert str(incorrect) + ':1:' in result.stdout
        assert check_compilation(
            {str(correct): True, str(incorrect): False}, diagnostics, None) == (
                OracleFinding('unexpected_rejection', str(correct),
                              tuple(diagnostics[str(correct)])),)
        assert check_compilation(
            {str(correct): False, str(incorrect): False}, diagnostics, None) == ()
