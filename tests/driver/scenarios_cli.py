import io
import json
import os
import shlex
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


def basic() -> None:
    from tests.driver.support import load_driver

    load_driver()


def validate() -> None:
    from src.args import validate_args
    from tests.driver.support import load_driver

    driver = load_driver()
    validate_args(driver.options)


def requires_correctness_preserving() -> None:
    from src.args import validate_args
    from tests.driver.support import load_driver

    driver = load_driver()
    with patch.object(driver.options,
                      "only_correctness_preserving_transformations", False):
        validate_args(driver.options)


def requires_transformation_source() -> None:
    from src.args import validate_args
    from tests.driver.support import load_driver

    driver = load_driver()
    with patch.object(driver.options, "transformations", None):
        validate_args(driver.options)


def defaults() -> None:
    from src.args import validate_args
    from src.generators.config import cfg
    from tests.driver.support import load_driver

    driver = load_driver()
    workspace = Path.cwd()
    args = driver.options
    stats = driver.reporting.stats
    assert args.language == "kotlin"
    assert args.backend == "native"
    assert args.iterations == 1
    assert args.seconds is None
    assert args.stop_cond == "iterations"
    assert args.transformations == 0
    assert args.batch == 1
    assert args.workers is None
    assert not args.debug
    assert not args.keep_all
    assert not args.keep_successful
    assert not args.disable_metrics
    assert not args.time_metrics
    assert args.only_correctness_preserving_transformations
    assert Path(args.bugs).resolve() == workspace / "bugs"
    assert Path(args.test_directory).resolve() == workspace / "bugs" / "session"
    assert Path(args.temp_directory).resolve() == workspace / "temp"
    assert Path(args.log_file).resolve() == workspace / "run.log"
    assert args.options == {
        "Generator": {"disable_metrics": False},
        "Translator": {"cast_numbers": False},
        "TypeErasure": {"timeout": 600},
        "TypeOverwriting": {"timeout": 600},
    }
    assert cfg.prob.crossmodule_probability == 0
    assert stats["Info"]["stop_cond_value"] == 1
    assert stats["Info"]["language"] == "kotlin"
    assert stats["totals"] == {"passed": 0, "failed": 0, "inconclusive": 0}
    assert "crossmodule" not in stats
    validate_args(args)
    assert not Path(args.bugs).exists()


def configuration() -> None:
    from src.args import validate_args
    from src.generators.config import cfg
    from tests.driver.support import load_driver

    driver = load_driver()
    assert cfg.limits.max_depth == 4
    assert cfg.limits.max_type_params == 2
    assert cfg.limits.inline_default_depth == 3
    assert cfg.limits.max_var_decls == 8
    assert cfg.dis.use_site_variance is False
    assert cfg.dis.use_site_contravariance is False
    assert cfg.prob.bounded_type_parameters == 0.8
    assert cfg.prob.parameterized_functions == 0.8
    assert cfg.prob.reified_type_parameters == 0.8
    assert cfg.prob.crossmodule_probability == 0
    validate_args(driver.options)


def configuration_flags() -> None:
    from src.args import validate_args
    from src.generators.config import cfg
    from tests.driver.support import load_driver

    driver = load_driver()
    args = driver.options
    stats = driver.reporting.stats
    assert cfg.limits.max_depth == 0
    assert cfg.limits.max_type_params == 0
    assert cfg.limits.inline_default_depth == 0
    assert cfg.limits.max_var_decls == 8
    assert cfg.dis.use_site_variance is True
    assert cfg.dis.use_site_contravariance is True
    assert cfg.prob.bounded_type_parameters == 0
    assert cfg.prob.parameterized_functions == 0
    assert cfg.prob.reified_type_parameters == 0
    assert args.keep_successful and not args.keep_all
    assert args.time_metrics and args.disable_metrics and args.dump_ir
    assert args.dry_run and args.log and args.print_stacktrace
    assert args.seconds == 30 and args.iterations == 0
    assert args.stop_cond == "timeout"
    assert args.backend == "js"
    assert args.transformation_types == []
    assert args.options == {
        "Generator": {"disable_metrics": True},
        "Translator": {"cast_numbers": True},
        "TypeErasure": {"timeout": 17},
        "TypeOverwriting": {"timeout": 17},
    }
    assert stats["Info"]["stop_cond"] == "timeout"
    assert stats["Info"]["stop_cond_value"] == 30
    assert stats["Info"]["transformation_types"] == ""
    validate_args(args)


def non_kotlin() -> None:
    from src.args import validate_args
    from src.generators.config import cfg
    from tests.driver.support import load_driver

    driver = load_driver()
    assert driver.options.language != "kotlin"
    assert cfg.prob.crossmodule_probability == 0
    assert "crossmodule" not in driver.reporting.stats
    validate_args(driver.options)


def helpers() -> None:
    from src import utils
    from src.args import pre_process_args
    from tests.driver.support import load_driver

    driver = load_driver()
    test_directory = Path(driver.options.test_directory)
    assert Path(driver.artifacts.get_generator_dir(7)) == (
        test_directory / "generator" / "iter_7")
    assert Path(driver.artifacts.get_transformations_dir(7, 3)) == (
        test_directory / "transformations" / "iter_7" / "3")
    assert not test_directory.exists()
    with patch("src.args.mkdir", wraps=utils.mkdir) as mkdir:
        pre_process_args(driver.options)
        bugs = Path(driver.options.bugs)
        assert bugs.is_dir()
        marker = bugs / "keep.txt"
        marker.write_text("preserved", encoding="utf-8")
        pre_process_args(driver.options)
        mkdir.assert_called_once_with(driver.options.bugs)
        assert marker.read_text(encoding="utf-8") == "preserved"

    def combine(value: int, *, suffix: str) -> str:
        return str(value) + suffix

    with patch.object(time, "perf_counter", side_effect=[10.0, 10.25]) as clock:
        assert driver.compilation.timed(combine, 4, suffix="!") == ("4!", 0.25)
        assert clock.call_count == 2

    failure = ValueError("timed callable failed")

    def fail() -> None:
        raise failure

    with patch.object(time, "perf_counter", return_value=10.0) as clock:
        try:
            driver.compilation.timed(fail)
        except ValueError as error:
            assert error is failure
        else:
            raise AssertionError("timed() swallowed the callable's error")
        clock.assert_called_once_with()


def logging_and_progress() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    args = driver.options
    reporting = driver.reporting
    reporting.stats["totals"].update(passed=3, failed=2, inconclusive=1)
    output = io.StringIO()
    with redirect_stdout(output), patch.object(args, "iterations", 8):
        reporting.print_msg()
    assert output.getvalue() == (
        "\033[2K\033[1GTest Programs Passed 3 / 8 ✔\t\t"
        "Test Programs Failed 2 / 8 ✘  Inconclusive 1 / 8\r")
    output = io.StringIO()
    with redirect_stdout(output), patch.object(args, "iterations", 0):
        reporting.print_msg()
    assert output.getvalue() == (
        "\033[2K\033[1GTest Programs Passed 3 / 6 ✔\t\t"
        "Test Programs Failed 2 / 6 ✘  Inconclusive 1 / 6\r")

    log_file = Path(args.log_file)
    log_file.write_text("existing log\n", encoding="utf-8")
    output = io.StringIO()
    with redirect_stdout(output), patch.object(reporting, "print_msg") as progress:
        reporting.logging("compiler version")
        if args.debug:
            progress.assert_not_called()
        else:
            progress.assert_called_once_with()
    text = output.getvalue()
    stop_value = args.seconds if args.stop_cond == "timeout" else args.iterations
    assert f"{args.stop_cond} ({stop_value})" in text
    assert "compiler version" in text
    for label in ("transformations", "transformation_types", "bugs", "name", "language"):
        assert label in text
    assert ("Warning: To stop the tool" in text) == (
        not args.seconds and not args.iterations)
    assert reporting.stats["Info"]["compiler"] == "compiler version"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == "existing log"
    assert lines[1].endswith(
        f"; {args.name}; {args.bugs}; {args.language}; compiler version")


def _child_script(workspace: Path) -> Path:
    script = workspace / "child script.py"
    script.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "print('child stdout', flush=True)\n"
        "print('child stderr', file=sys.stderr, flush=True)\n"
        "print(json.dumps([os.getcwd(), os.environ.get('JAVA_OPTS'), "
        "sys.argv[2:]]), flush=True)\n"
        "sys.exit(int(sys.argv[1]))\n",
        encoding="utf-8",
    )
    return script


def _python_command(script: Path, *arguments: str) -> list[str]:
    return [shlex.quote(value)
            for value in (sys.executable, str(script), *arguments)]


def _expected_output(cwd: Path, argument: str) -> str:
    return ("child stdout\nchild stderr\n"
            + json.dumps([str(cwd.resolve()), "-Xmx8g", [argument]]) + "\n")


def _command_output(exit_code: int) -> None:
    from src.compilers.results import ProcessResult
    from tests.driver.support import load_driver

    driver = load_driver()
    workspace = Path.cwd()
    script = _child_script(workspace)
    child_cwd = workspace / "child cwd"
    child_cwd.mkdir()
    argument = "spaces, 'quotes', and $literal"
    command = _python_command(script, str(exit_code), argument)
    compilation = driver.compilation
    with patch.dict(os.environ, {"JAVA_OPTS": "-Xmx64m"}):
        assert compilation.run_command(command, cwd=str(child_cwd)) == (
            exit_code == 0, _expected_output(child_cwd, argument))
        assert compilation.run_process(command, cwd=str(child_cwd)) == ProcessResult(
            exit_code, _expected_output(child_cwd, argument))
        assert compilation.run_command(
            command, get_stdout=False, cwd=str(child_cwd)) == (exit_code == 0, "")
        assert compilation.run_command(command) == (
            exit_code == 0, _expected_output(workspace, argument))
        assert os.environ["JAVA_OPTS"] == "-Xmx64m"
    assert Path.cwd() == workspace


def command_success() -> None:
    _command_output(0)


def command_failure() -> None:
    _command_output(7)


def command_empty_output() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    script = Path.cwd() / "empty script.py"
    script.write_text("pass\n", encoding="utf-8")
    assert driver.compilation.run_command(_python_command(script)) == (True, "")


def command_groovy() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    workspace = Path.cwd()
    script = _child_script(workspace)
    bin_dir = workspace / "fake bin"
    bin_dir.mkdir()
    executable = bin_dir / "groovyc"
    executable.write_text(
        "#!/bin/sh\nexec " + " ".join(_python_command(script)) + ' "$@"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    temporary_cwd = Path(driver.options.test_directory) / "tmp"
    argument = "groovy argument"
    with patch.dict(os.environ, {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "JAVA_OPTS": "-Xmx64m",
    }):
        for exit_code in (0, 9):
            assert driver.compilation.run_command([
                "groovyc", str(exit_code), shlex.quote(argument),
            ]) == (exit_code == 0, _expected_output(temporary_cwd, argument))
            assert temporary_cwd.is_dir()
            assert Path.cwd() == workspace
        assert driver.compilation.run_command(
            ["groovyc", "0", shlex.quote(argument)], cwd=str(workspace),
        ) == (True, _expected_output(workspace, argument))
        assert os.environ["JAVA_OPTS"] == "-Xmx64m"
    assert Path.cwd() == workspace


def command_launch_errors() -> None:
    from src.compilers.results import ProcessResult
    from tests.driver.support import load_driver

    driver = load_driver()
    compilation = driver.compilation
    workspace = Path.cwd()
    command = [shlex.quote(sys.executable), "unused-script.py"]
    failure = subprocess.CalledProcessError(4, command, output=b"launch failed")
    launch_error = OSError("controlled launch error")
    for error in (failure, launch_error):
        with patch.object(subprocess, "Popen", side_effect=error) as popen:
            assert compilation.run_command(command) == (False, str(error))
            assert compilation.run_process(command) == ProcessResult(
                None, str(error), str(error))
            assert popen.call_count == 2
    with patch.object(subprocess, "Popen", side_effect=launch_error) as popen:
        assert compilation.run_command(["groovyc", "unused.groovy"]) == (
            False, str(launch_error))
        popen.assert_called_once()
    assert Path(driver.options.test_directory, "tmp").is_dir()
    assert Path.cwd() == workspace