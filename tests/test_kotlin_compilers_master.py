from __future__ import annotations

from src.tools.kotlin_compilers.layout import Layout, master_backend
from tests.kotlin_compilers_fakes import fake_build, fake_master


def test_master_backend_paths_are_absolute_in_the_resolved_build(tmp_path, monkeypatch):
    build = fake_master(tmp_path / 'compilers', monkeypatch)
    assert build.name == '2.5.20-dev-1' and 'master' not in build.parts
    assert master_backend('native') == {
        'launcher': str(build / 'native/bin/kotlinc-native'), 'args': [],
        'stdlib': str(build / 'native/klib/common/stdlib'), 'first_stage_only': True}
    assert master_backend('jvm')['stdlib'] == str(build / 'kotlinc/lib/kotlin-stdlib.jar')
    assert master_backend('js')['stdlib'] == str(build / 'stdlib/kotlin-stdlib-js.klib')
    assert master_backend('wasm') == {
        'launcher': str(build / 'kotlinc/bin/kotlinc-wasm'), 'args': [],
        'stdlib': str(build / 'stdlib/kotlin-stdlib-wasm-js.klib')}


def test_master_backend_passes_descriptor_launcher_args_through(tmp_path, monkeypatch):
    build = fake_master(tmp_path / 'compilers', monkeypatch, wasm_launcher=False)
    entry = master_backend('wasm')
    assert entry['launcher'] == str(build / 'kotlinc/bin/kotlinc-js')
    assert entry['args'] == ['-Xwasm', '-Xwasm-target=wasm-js']


def test_master_is_resolved_once_per_process(tmp_path, monkeypatch):
    first = fake_master(tmp_path / 'compilers', monkeypatch)
    assert master_backend('native')['launcher'].startswith(str(first) + '/')
    layout = Layout(tmp_path / 'compilers')
    fake_build(layout, '2.5.20-dev-2')
    layout.set_symlink('master', '2.5.20-dev-2')
    # Every backend still comes from the build resolved first.
    for backend in ('native', 'jvm', 'js', 'wasm'):
        entry = master_backend(backend)
        assert entry['launcher'].startswith(str(first) + '/')
        assert entry['stdlib'].startswith(str(first) + '/')


def test_separate_store_roots_resolve_their_own_builds(tmp_path, monkeypatch):
    first = fake_master(tmp_path / 'one', monkeypatch)
    assert master_backend('js')['launcher'] == str(first / 'kotlinc/bin/kotlinc-js')
    second_layout = Layout(tmp_path / 'two')
    second_layout.ensure()
    second = fake_build(second_layout, '2.5.20-dev-7').resolve()
    second_layout.set_symlink('master', second.name)
    monkeypatch.setenv('KOTLIN_COMPILERS_HOME', str(second_layout.root))
    # No cache clear: the cache is keyed by the master link of each root.
    assert master_backend('js')['launcher'] == str(second / 'kotlinc/bin/kotlinc-js')
    monkeypatch.setenv('KOTLIN_COMPILERS_HOME', str(tmp_path / 'one'))
    assert master_backend('js')['launcher'] == str(first / 'kotlinc/bin/kotlinc-js')


def test_master_under_home_is_used_without_the_environment_variable(tmp_path, monkeypatch):
    build = fake_master(tmp_path / 'kotlin-compilers', monkeypatch)
    monkeypatch.delenv('KOTLIN_COMPILERS_HOME')
    monkeypatch.setenv('HOME', str(tmp_path))
    assert master_backend('jvm')['launcher'] == str(build / 'kotlinc/bin/kotlinc-jvm')
