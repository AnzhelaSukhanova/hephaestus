"""Opt-in integration checks against the internally selected Kotlin distributions."""
import os
from pathlib import Path
import zipfile

import pytest

from src.compilers.kotlin import KOTLIN_COMPARISON_VARIANTS, KotlinCompiler
from src.compilers.results import CompilationRequest
from tests.test_backend_comparison import application


@pytest.fixture(scope='module')
def real_kotlin(tmp_path_factory):
    if os.environ.get('HEPHAESTUS_TEST_REAL_KOTLIN') != '1':
        pytest.skip('set HEPHAESTUS_TEST_REAL_KOTLIN=1 for installed Kotlin distributions')
    from src.compilers.kotlin import _compiler_executable
    missing = []
    for settings in KOTLIN_COMPARISON_VARIANTS.values():
        executable = os.path.expandvars(_compiler_executable(settings))
        library = os.path.expandvars(KotlinCompiler('', settings=settings).stdlib_path())
        for path in (executable, library):
            if not os.path.exists(path):
                missing.append(path)
    if missing:
        pytest.skip('missing internally selected Kotlin tools/libraries: ' + ', '.join(missing))
    app = application(tmp_path_factory.mktemp('real-backend-comparison'))
    app.mode.preflight(app.compilation)
    return app


def compile_source(app, root, module, source, direct=None, closure=()):
    filename = root / module / 'program.kt'
    filename.parent.mkdir(parents=True)
    filename.write_text(source)
    requests = []
    for variant_id, settings in app.compilation.settings_by_variant.items():
        destination = root / module / variant_id
        destination.mkdir()
        paths = [str(root / parent / variant_id / (parent + '.klib'))
                 for parent in closure]
        friends = ([str(root / direct / variant_id / (direct + '.klib'))]
                   if direct else [])
        compiler = app.compilation.make_compiler(
            str(filename), module_name=module,
            output_directory=str(destination), klib_output=True,
            dependency_klibs=paths, friend_klibs=friends, settings=settings)
        requests.append(CompilationRequest(variant_id, compiler, str(destination),
                                           str(destination / (module + '.klib'))))
    return app.compilation.run_variants(requests)


def test_real_valid_and_invalid_sources_on_all_backends(real_kotlin, tmp_path):
    valid = compile_source(real_kotlin, tmp_path, 'valid',
                           'package src.valid\ninternal val answer: Int = 1\n')
    for variant in valid:
        assert variant.command.status and variant.dependency_artifact_valid, (
            variant.variant_id, variant.command.output)
    invalid = compile_source(real_kotlin, tmp_path, 'invalid',
                             'package src.invalid\nval answer: Int = "not an Int"\n')
    for variant in invalid:
        assert not variant.command.status, variant.variant_id
        assert variant.command.errors, (variant.variant_id, variant.command.output)
        assert variant.command.parsing_complete, (variant.variant_id,
                                                   variant.command.unparsed)


def test_real_target_matched_friend_and_transitive_chain(real_kotlin, tmp_path):
    programs = [
        ('d1', 'package src.d1\nfun seed() = 41\n', None, ()),
        ('d2', 'package src.d2\ninternal fun next() = src.d1.seed() + 1\n', 'd1', ('d1',)),
        ('d3', 'package src.d3\nfun answer() = src.d2.next() + src.d1.seed()\n',
         'd2', ('d2', 'd1')),
    ]
    for module, source, direct, closure in programs:
        variants = compile_source(real_kotlin, tmp_path, module, source, direct, closure)
        for variant in variants:
            assert variant.command.status and variant.dependency_artifact_valid, (module,
                variant.variant_id, variant.command.output)
            assert variant.command.command[0].endswith(
                '/kotlinc-' + variant.variant_id)
            manifest = KotlinCompiler.read_manifest_depends(
                variant.expected_dependency_artifact_path)
            assert manifest is not None
            assert not any('-' + variant.variant_id in name for name in manifest)
            if variant.variant_id in ('js', 'wasm'):
                with zipfile.ZipFile(variant.expected_dependency_artifact_path) as archive:
                    metadata = dict(line.split('=', 1)
                                    for line in archive.read('default/manifest').decode().splitlines()
                                    if '=' in line)
                assert metadata['builtins_platform'] == (
                    'WASM' if variant.variant_id == 'wasm' else 'JS')
                if variant.variant_id == 'wasm':
                    assert metadata['wasm_targets'] == 'wasm-js'