import multiprocessing as mp
from pathlib import Path

from src.args import parse_args
from src.modules.worker import RunOptions
from src.tools.kotlin_compilers.layout import Layout


def _layout_lock_worker(root: str, worker_id: int, first_entered,
                        release_first, second_attempting, second_entered) -> None:
    if worker_id == 1:
        second_attempting.set()
    with Layout(root).locked():
        if worker_id == 0:
            first_entered.set()
            if not release_first.wait(timeout=10):
                raise TimeoutError('timed out waiting to release the first layout lock')
        else:
            second_entered.set()


def _coverage_lock_worker(root: str, worker_id: int, first_entered,
                          release_first, second_attempting, second_entered) -> None:
    import src.modules.coverage as coverage_module

    def merge_exec_files(inputs, merged) -> None:
        if worker_id == 0:
            first_entered.set()
            if not release_first.wait(timeout=10):
                raise TimeoutError('timed out waiting to release the first coverage lock')
        else:
            second_entered.set()
        Path(merged).write_bytes(b''.join(Path(path).read_bytes() for path in inputs))

    coverage_module.merge_exec_files = merge_exec_files
    coverage_module.write_jacoco_report = lambda *_args: None
    if worker_id == 1:
        second_attempting.set()

    live_coverage = object.__new__(coverage_module.LiveCoverage)
    live_coverage.options = RunOptions.from_namespace(parse_args(['--language', 'kotlin']))
    live_coverage._refresh_jacoco_report(Path(root))
    if worker_id == 1:
        second_entered.set()


def _assert_process_lock_excludes_second_worker(target, root: Path) -> None:
    context = mp.get_context('spawn')
    first_entered = context.Event()
    release_first = context.Event()
    second_attempting = context.Event()
    second_entered = context.Event()
    processes = [
        context.Process(
            target=target,
            args=(str(root), worker_id, first_entered, release_first,
                  second_attempting, second_entered),
        )
        for worker_id in (0, 1)
    ]
    processes[0].start()
    try:
        assert first_entered.wait(timeout=10)
        processes[1].start()
        assert second_attempting.wait(timeout=10)
        assert not second_entered.wait(timeout=1), 'second worker entered the locked section'
    finally:
        release_first.set()
        for process in processes:
            if process.pid is not None:
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)

    assert [process.exitcode for process in processes] == [0, 0]
    assert second_entered.is_set()


def test_compiler_store_lock_serializes_processes(tmp_path: Path) -> None:
    layout = Layout(tmp_path / 'compilers')
    layout.ensure()
    _assert_process_lock_excludes_second_worker(
        _layout_lock_worker, layout.root
    )


def test_live_jacoco_merge_lock_serializes_processes(tmp_path: Path) -> None:
    output_dir = tmp_path / 'coverage'
    exec_dir = output_dir / 'execs'
    exec_dir.mkdir(parents=True)
    (output_dir / 'classes').mkdir()
    (output_dir / '.merge.lock').touch()
    (exec_dir / '1.exec').write_bytes(b'1')
    (exec_dir / '2.exec').write_bytes(b'2')

    _assert_process_lock_excludes_second_worker(
        _coverage_lock_worker, output_dir
    )

    assert (output_dir / 'jacoco.exec').read_bytes() == b'12'
    assert list(exec_dir.iterdir()) == []
    assert not (output_dir / 'jacoco.exec.new').exists()
